# ADR 0128: mypy strict

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 01-repository.md, 84-testing.md
- **Related ADRs:** 0006, 0011, 0013, 0129, 0132

## Context

Every Python module is type-checked in strict mode, before each commit and in CI; the two strict checkers that fit this code base are mypy and Pyright. Two properties of the code base weigh on the choice. Every boundary is a Pydantic v2 model with `extra="forbid"` (ADR 0013), so the checker must understand how Pydantic builds `__init__`, including aliases. And everything runs on an event loop (ADR 0006), where a coroutine called without `await` is a silent bug. Every Python tool is also installed from the one hash-checked `uv.lock` (ADR 0011), so a tool that fetches parts of itself at run time would sit outside that guarantee.

Vendor facts (checked 2026-10-21 unless stated):

- mypy 2.4.0 is the current release (PyPI, checked 2026-10-15). `follow_untyped_imports` "makes mypy analyze imports from installed packages even if missing a py.typed marker or stubs", and per-module settings go in `[[tool.mypy.overrides]]` blocks of `pyproject.toml`. Its optional error codes include `ignore-without-code` (a `# type: ignore` without a code), `redundant-expr`, `truthy-bool` (a value with no `__bool__` or `__len__` in a boolean context), `possibly-undefined`, `unused-awaitable` (an awaitable result never used, "often from a missing `await`") and `explicit-override` (an override without `@override`).
- Pydantic's mypy plugin is enabled with `plugins = ['pydantic.mypy']` and configured in `[tool.pydantic-mypy]`: `init_typed` uses the declared field types in the generated `__init__` (otherwise `Any`); `init_forbid_extra` removes the `**kwargs: Any` the plugin otherwise adds; `warn_required_dynamic_aliases` reports a dynamic alias or alias generator used while `validate_by_name` is `False`, which would otherwise make every `__init__` argument optional.
- The `pyright` package on PyPI (1.1.414) is a wrapper: it looks for Node.js on the `PATH`, fetches Node with nodeenv when it is absent (or takes it from the `nodejs` extra), then installs the `pyright` npm package and runs it with Node.

## Decision

1. **mypy, pinned in the lock.** `mypy==2.4.0` is an exact pin in the root `dev` dependency group, installed with every other tool by `uv sync --locked --all-packages --all-groups`. Pyright isn't used.
2. **Configuration**, in the root `pyproject.toml`:

   ```toml
   [tool.mypy]
   python_version = "3.13"
   strict = true
   warn_unreachable = true
   enable_error_code = ["ignore-without-code", "redundant-expr", "truthy-bool", "possibly-undefined", "unused-awaitable", "explicit-override"]
   plugins = ["pydantic.mypy"]

   [tool.pydantic-mypy]
   init_forbid_extra = true
   init_typed = true
   warn_required_dynamic_aliases = true
   ```

3. **What is checked.** `[tool.mypy] files` lists exactly every workspace member's `src/` and `tests/` trees, `tests/integration`, `tests/deployed`, `tests/infra`, `tests/load/aci`, `scripts/ci` and `db/migrations`, so test code (the compiled-template tests included), CI tools and migrations are held to the same rules as the apps.
4. **One place for untyped packages.** Third-party packages that ship no type information get `follow_untyped_imports = true` in a single `[[tool.mypy.overrides]]` block whose `module` list is exactly the set the stage-0 run reports as `import-untyped`: known at design time, `unstructured_inference.*` (its wheel has no `py.typed`) and any stub-less `pyodbc` submodule the run reports. A CI check fails when that list names a first-party package (`docchat_*`). No first-party module is exempt from any check, and every `# type: ignore` names its error code.
5. **Where it runs.** The pre-commit hook `mypy` (`repo: local`, `entry: uv run --frozen mypy`, `language: system`, `pass_filenames: false`, `types: [python]`) runs it before each commit, and `ci.yaml`'s `lint` job runs `pre-commit run --all-files`, a required status check on `main`.

## Alternatives considered

- **Pyright in strict mode.** Rejected: its PyPI package fetches the type checker from npm, and Node.js too unless an extra supplies it, outside the hash-checked lockfile; it adds a JavaScript runtime to the Python toolchain; and it runs no plugins, so it has no counterpart to the Pydantic plugin's checks on forbidden extras and dynamic aliases.
- **Both checkers.** Rejected: two checkers that disagree on edge cases cost review time and find no class of bug in this code base that one strict checker misses.
- **mypy without `strict`, tightened module by module.** Rejected: strict typing is required from the first commit, and a new repository has no untyped code waiting to be tightened.
- **Per-module `ignore_errors` for hard cases.** Rejected: an exemption hides every future error in that module; a coded `# type: ignore` covers one line.

## Consequences

- Constructing a request, response, queue message, Cosmos document or settings model with an unknown or wrongly typed field fails the type check before any test runs.
- `unused-awaitable` turns a coroutine called without `await` into a check failure, beside Ruff's `ASYNC` rules, the loop guard and the loop-lag metric (ADRs 0129 and 0132).
- A new dependency without type information must be added to the overrides block by name; nothing silently becomes `Any`.
- mypy updates arrive through Dependabot's `dev-tools` group for `uv`; a release that reports new errors gets them corrected in the same pull request before it merges.
- Tests (84-testing.md): the `lint` job fails on any mypy error; a CI check fails when the overrides block names a first-party package.

## References

- mypy optional error codes: https://mypy.readthedocs.io/en/stable/error_code_list2.html (checked 2026-10-21)
- mypy configuration file: https://mypy.readthedocs.io/en/stable/config_file.html (checked 2026-10-21)
- Pydantic mypy plugin: https://pydantic.dev/docs/validation/latest/integrations/dev-tools/mypy/ (checked 2026-10-21)
- mypy on PyPI: https://pypi.org/project/mypy/ (checked 2026-10-15)
- pyright on PyPI: https://pypi.org/project/pyright/ (checked 2026-10-21)
