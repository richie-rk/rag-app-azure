"""The `docchat-load` command line (`84-testing.md` section 7; `01-repository.md` section 3).

Commands, each run by a job of `load-test.yaml`:

- `tokens`: the token file of the load users (section 6.4);
- `aci-params`: the load generator's deployment parameters (section 7.5 step 5);
- `verdict`: a scenario's criteria (section 7.6);
- `s3-rate`: S3's main-phase rate from its calibration (section 7.5);
- `probe-alerts`: the alert probe's two phases (section 7.5);
- `seed`: the load run's project, index, documents, users and sessions (section 7.4 job 4);
- `build-corpus`: S4's byte-unique corpus (section 1.5);
- `ingest`: one S4 set (section 7.5).

Exit codes: 0 done (a verdict passed or came out `inconclusive`), 1 a failed criterion or a failure
of the work, 2 a refused argument or input. Failures print one JSON line `{"code", "message"}` on
standard error; nothing printed holds a token, a password or a header.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any

from docchat_testing.load import aciparams, corpus, ingest, probe, s3rate, seeding, tokenfile
from docchat_testing.load.azcli import Azure
from docchat_testing.load.common import (
    ADMIN_ALIAS,
    ENVIRONMENT,
    EXIT_OK,
    LoadError,
    UsageError,
    emit,
    emit_error,
    load_questions,
    parse_aliases,
    parse_time,
    read_azd_environment,
    read_json,
    read_tokens,
    setting,
)
from docchat_testing.load.markers import read_log
from docchat_testing.load.verdict import (
    SessionApi,
    Sources,
    Verdict,
    evaluate_s1,
    evaluate_s2,
    evaluate_s3,
    evaluate_s4,
    finish,
    prod_maxima,
)
from docchat_testing.logquery import Rows, query_logs
from docchat_testing.tokens import DeployedTokens, load_passwords

type Handler = Callable[[argparse.Namespace], Coroutine[Any, Any, int]]

WORKSPACE_VARIABLE = "DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID"
ANSWER_TOKENS_RANGE = (80, 4000)


def _answer_tokens(value: str) -> int:
    number = int(value)
    low, high = ANSWER_TOKENS_RANGE
    if not low <= number <= high:
        raise argparse.ArgumentTypeError(f"answer tokens run from {low} to {high}")
    return number


def _rate(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("the rate is a positive integer")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docchat-load", description="The DocChat load test.")
    commands = parser.add_subparsers(dest="command", required=True)

    tokens = commands.add_parser("tokens", help="write the load users' token file")
    tokens.add_argument("--env", required=True, choices=[ENVIRONMENT])
    tokens.add_argument("--aliases", required=True, type=parse_aliases)
    tokens.add_argument("--out", required=True, type=Path)

    params = commands.add_parser("aci-params", help="write the load generator's parameters")
    params.add_argument("--scenario", required=True, choices=aciparams.SCENARIOS)
    params.add_argument("--bootstrap", required=True, type=Path)
    params.add_argument("--script", required=True, type=Path)
    params.add_argument("--lib", required=True, type=Path)
    params.add_argument("--tokens", required=True, type=Path)
    params.add_argument("--out", required=True, type=Path)

    verdict = commands.add_parser("verdict", help="compute a scenario's verdict")
    verdict.add_argument("--scenario", required=True, choices=["s1", "s2", "s3", "s4"])
    verdict.add_argument("--k6-log", type=Path)
    verdict.add_argument("--start", type=parse_time)
    verdict.add_argument("--end", type=parse_time)
    verdict.add_argument("--run-start", required=True, type=parse_time)
    verdict.add_argument("--out", required=True, type=Path)
    verdict.add_argument("--removed-at", type=parse_time, help="S2: T_r")
    verdict.add_argument("--tokens", type=Path, help="S2: the token file, for S2-e")
    verdict.add_argument("--rate", type=Path, help="S3: rate.json of s3-rate")
    verdict.add_argument("--set", type=int, choices=[1, 2], help="S4: the set")
    verdict.add_argument("--results", type=Path, help="S4: the set's ingest results directory")
    verdict.add_argument("--set1-results", type=Path, help="S4 set 2: set 1's results directory")

    rate = commands.add_parser("s3-rate", help="set S3's main-phase rate")
    rate.add_argument("--start", required=True, type=parse_time)
    rate.add_argument("--answer-tokens", required=True, type=_answer_tokens)
    rate.add_argument("--rate", type=_rate)
    rate.add_argument("--out", required=True, type=Path)

    alerts = commands.add_parser("probe-alerts", help="the alert probe")
    alerts.add_argument("--phase", required=True, choices=["send", "check"])
    alerts.add_argument("--tokens", type=Path)
    alerts.add_argument("--out", required=True, type=Path)
    alerts.add_argument("--run-start", type=parse_time)

    seed = commands.add_parser("seed", help="seed the load run")
    seed.add_argument("--tokens", required=True, type=Path)
    seed.add_argument("--applied-at", required=True, type=parse_time)

    build = commands.add_parser("build-corpus", help="write S4's corpus")
    build.add_argument("--variant", required=True, choices=corpus.VARIANTS)
    build.add_argument("--count", required=True, type=int)
    build.add_argument("--out", required=True, type=Path)

    run = commands.add_parser("ingest", help="run one S4 set")
    run.add_argument("--env", required=True, choices=[ENVIRONMENT])
    run.add_argument("--variant", required=True, choices=corpus.VARIANTS)
    run.add_argument("--index", required=True)
    run.add_argument("--corpus", required=True, type=Path)
    run.add_argument("--out", required=True, type=Path)
    return parser


def _azd() -> dict[str, str]:
    return read_azd_environment(ENVIRONMENT)


def _sources(azd: Mapping[str, str]) -> Sources:
    workspace = setting(WORKSPACE_VARIABLE, azd=azd)

    async def run_query(kql: str, timespan: str) -> Rows:
        return await query_logs(workspace, kql, timespan)

    return Sources(query=run_query, azure=Azure())


def _require(arguments: argparse.Namespace, *names: str) -> None:
    missing = [f"--{name.replace('_', '-')}" for name in names if getattr(arguments, name) is None]
    if missing:
        raise UsageError("argument_missing", f"{arguments.scenario} needs {', '.join(missing)}")


async def run_tokens(arguments: argparse.Namespace) -> int:
    tokens = await tokenfile.write_token_file(arguments.env, arguments.aliases, arguments.out)
    emit(f"wrote {len(tokens)} tokens to {arguments.out}")
    return EXIT_OK


async def run_aci_params(arguments: argparse.Namespace) -> int:
    aciparams.write_parameters(
        arguments.out,
        scenario=arguments.scenario,
        bootstrap=arguments.bootstrap,
        script=arguments.script,
        lib=arguments.lib,
        tokens=arguments.tokens,
    )
    emit(f"wrote the {arguments.scenario} parameters to {arguments.out}")
    return EXIT_OK


async def _verdict(arguments: argparse.Namespace, azd: Mapping[str, str]) -> Verdict:
    sources = _sources(azd)
    if arguments.scenario == "s4":
        _require(arguments, "set", "results")
        results = read_json(arguments.results / ingest.RESULT_FILE, "the set's results")
        first = None
        if arguments.set == 2:
            _require(arguments, "set1_results")
            first = read_json(arguments.set1_results / ingest.RESULT_FILE, "set 1's results")
        return await evaluate_s4(
            arguments.set,
            results=results,
            set1_results=first,
            run_start=arguments.run_start,
            sources=sources,
        )
    _require(arguments, "k6_log", "start", "end")
    log = read_log(arguments.k6_log.read_text(encoding="utf-8", errors="replace"))
    if arguments.scenario == "s1":
        return await evaluate_s1(
            log,
            start=arguments.start,
            end=arguments.end,
            run_start=arguments.run_start,
            sources=sources,
            maxima=prod_maxima(),
        )
    if arguments.scenario == "s2":
        _require(arguments, "removed_at", "tokens")
        api = SessionApi(
            api_base_url=setting("API_BASE_URL"),
            tokens=read_tokens(arguments.tokens),
            questions=load_questions(),
        )
        return await evaluate_s2(
            log,
            start=arguments.start,
            end=arguments.end,
            removed_at=arguments.removed_at,
            run_start=arguments.run_start,
            sources=sources,
            api=api,
        )
    _require(arguments, "rate")
    return await evaluate_s3(
        log,
        start=arguments.start,
        end=arguments.end,
        run_start=arguments.run_start,
        sources=sources,
        rate=read_json(arguments.rate, "rate.json"),
        app_name=setting("DOCCHAT_API_APP_NAME", azd=azd),
    )


async def run_verdict(arguments: argparse.Namespace) -> int:
    verdict = await _verdict(arguments, _azd())
    code = finish(verdict, arguments.out)
    emit(f"{verdict.scenario}: {verdict.status}")
    return code


async def run_s3_rate(arguments: argparse.Namespace) -> int:
    azd = _azd()
    rate = await s3rate.measure(
        _sources(azd),
        start=arguments.start,
        answer_tokens=arguments.answer_tokens,
        rate_input=arguments.rate,
        app_name=setting("DOCCHAT_API_APP_NAME", azd=azd),
        cosmos_account=setting("DOCCHAT_COSMOS_ACCOUNT_NAME", azd=azd),
    )
    verdict = s3rate.write_rate(arguments.out, rate)
    if verdict is not None:
        finish(verdict, arguments.out.parent / "verdict.json")
    emit(f"R = {rate['rate']} turns/s ({rate['boundBy']} bound)")
    return EXIT_OK


async def run_probe(arguments: argparse.Namespace) -> int:
    if arguments.phase == "send":
        if arguments.tokens is None:
            raise UsageError("argument_missing", "the send phase needs --tokens")
        window = await probe.send(
            setting("API_BASE_URL"), read_tokens(arguments.tokens), load_questions(), arguments.out
        )
        emit(f"alert probe: {window['done']} of {window['turns']} turns ended with done")
        return EXIT_OK
    if arguments.run_start is None:
        raise UsageError("argument_missing", "the check phase needs --run-start")
    verdict = await probe.check(
        arguments.out, arguments.run_start, _sources(_azd()), probe.chat_deployment()
    )
    code = finish(verdict, arguments.out / "verdict.json")
    emit(f"alert probe: {verdict.status}")
    return code


async def run_seed(arguments: argparse.Namespace) -> int:
    result = await seeding.seed(
        setting("API_BASE_URL"),
        read_tokens(arguments.tokens),
        load_questions(),
        arguments.applied_at,
    )
    emit(f"seeded project {result.project_id} with {result.sessions} sessions")
    return EXIT_OK


async def run_build_corpus(arguments: argparse.Namespace) -> int:
    written = corpus.build_corpus(arguments.variant, arguments.count, arguments.out)
    emit(f"wrote {len(written)} files to {arguments.out}")
    return EXIT_OK


async def run_ingest(
    arguments: argparse.Namespace, environ: Mapping[str, str] | None = None
) -> int:
    env = os.environ if environ is None else environ
    azd = _azd()
    passwords = await load_passwords(
        setting(tokenfile.VAULT_VARIABLE, environ=env), [ADMIN_ALIAS], environment=arguments.env
    )
    held = DeployedTokens(
        passwords, api_client_id=setting(tokenfile.API_CLIENT_VARIABLE, environ=env, azd=azd)
    )
    await held.acquire_all()

    async def token() -> str:
        return await held.token(ADMIN_ALIAS)

    result = await ingest.run_set(
        setting("API_BASE_URL", environ=env),
        token,
        arguments.index,
        corpus.corpus_files(arguments.corpus, arguments.variant),
        arguments.out,
    )
    states = ", ".join(str(run["state"]) for run in result["runs"])
    emit(f"set ended: {states}")
    return EXIT_OK


HANDLERS: dict[str, Handler] = {
    "tokens": run_tokens,
    "aci-params": run_aci_params,
    "verdict": run_verdict,
    "s3-rate": run_s3_rate,
    "probe-alerts": run_probe,
    "seed": run_seed,
    "build-corpus": run_build_corpus,
    "ingest": run_ingest,
}


def main(
    argv: Sequence[str] | None = None, *, handlers: Mapping[str, Handler] | None = None
) -> int:
    arguments = build_parser().parse_args(argv)
    handler = (handlers or HANDLERS)[arguments.command]
    try:
        return asyncio.run(handler(arguments))
    except LoadError as error:
        emit_error(error)
        return error.exit_code
    except Exception as error:  # noqa: BLE001 - every failure ends as one JSON line and exit 1
        message = str(error).splitlines()[0][:200] if str(error) else ""
        emit_error(LoadError("internal_error", f"{type(error).__name__}: {message}"))
        return 1
