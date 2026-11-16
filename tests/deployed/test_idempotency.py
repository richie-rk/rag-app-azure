"""The deployed idempotency suite T-175 (`84-testing.md` sections 5.5 and 6.6; `14-ingestion.md`
acceptance criterion 23), `full` suite in `dev`, through the admin API as `admin` in the index
`automated-tests-idempotency`, mapped to `Automated tests`.

Each case ends with one served result: the document's current version is the one expected,
`ingested`, and every other version of it `superseded` (or the document stays deleted). The cases
the admin API can drive are here: an identical re-upload (test 2), two concurrent uploads of one
document (3), a delete during Embed (4), an update while a rebuild runs (5), several pending
versions (8) and a delete followed by a re-upload (9). Tests 1, 6 and 7 deliver a queue message
or restart the ingestion app, which only the `dev` deploy identity can do, in a job of its own
that `deployed-tests.yaml` doesn't have; they run on the orchestration harness (T-166, T-171,
T-172), and the restart is exercised deployed by the crash-resume test T-587. Chunk sets aren't
visible through the API, so "no duplicate chunk set" rests on the harness tests too.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Final

import pytest

from docchat_testing.adminapi import (
    FINISHED_ITEM_STATES,
    FINISHED_RUN_STATES,
    AdminApi,
    AdminApiError,
    UploadFile,
    problem_code,
    stage_reached,
)
from docchat_testing.seed import (
    IDEMPOTENCY_INDEX_NAME,
    MappedIndex,
    byte_unique,
    delete_documents,
    ensure_mapped_index,
    fixture_bytes,
    generated_pdf,
    upload_file,
)

pytestmark = [pytest.mark.deployed, pytest.mark.full]

RUN_WAIT_SECONDS: Final = 600
REBUILD_WAIT_SECONDS: Final = 840
EMBED_WAIT_SECONDS: Final = 600
DELETE_PAGES: Final = 120
ANALYZERS: Final = ("standard.lucene", "en.lucene")
HTTP_CONFLICT: Final = 409


@pytest.fixture
async def index(admin_api: AdminApi) -> MappedIndex:
    return await ensure_mapped_index(admin_api, IDEMPOTENCY_INDEX_NAME)


def variant(fixture: str, name: str, label: str) -> UploadFile:
    """A byte-unique copy of a fixture document under another name."""
    return upload_file(name, byte_unique(fixture_bytes(fixture), fixture, label))


async def document(api: AdminApi, index: MappedIndex, document_id: str) -> dict[str, Any]:
    detail = await api.get_document(index.index_id, document_id)
    assert detail is not None, "the document isn't live"
    return detail


async def settled(api: AdminApi, index: MappedIndex, document_id: str) -> dict[str, Any]:
    """Poll the document until nothing of it is in progress."""

    async def done() -> dict[str, Any] | None:
        detail = await document(api, index, document_id)
        return None if detail["displayState"] == "in_progress" else detail

    return await api.wait_until(done, within=RUN_WAIT_SECONDS, what="the document settling")


def assert_one_served(detail: dict[str, Any], current: UploadFile) -> None:
    """The current version holds `current`'s bytes and is `ingested`; every other version that
    was active is `superseded`."""
    served = detail["currentVersion"]
    assert served is not None, "no version is served"
    assert (served["sha256"], served["state"]) == (current.sha256, "ingested")
    others = [v for v in detail["versions"] if v["versionId"] != served["versionId"]]
    assert all(v["state"] in {"superseded", "failed"} for v in others), others
    assert [v["state"] for v in detail["versions"]].count("ingested") == 1


async def cleanup(api: AdminApi, index: MappedIndex, document_ids: Sequence[str]) -> None:
    await delete_documents(api, index.index_id, list(set(document_ids)), within=RUN_WAIT_SECONDS)


async def test_t175_case_2_an_identical_reupload_is_refused_and_not_ingested_again(
    admin_api: AdminApi, index: MappedIndex, run_label: str
) -> None:
    api = admin_api
    file = variant("tables.pdf", f"idem-{run_label}-reupload.pdf", f"idem-{run_label}-2")
    reservation = await api.upload(index.index_id, file, index.project_id)
    try:
        run_id = (await api.submit(index.index_id, [reservation.version_id]))["runId"]
        await api.wait_for_run(str(run_id), within=RUN_WAIT_SECONDS)
        [preflight] = await api.preflight(index.index_id, [file])
        assert preflight["status"] == "unchanged"
        refused = await api.reserve_response(index.index_id, file, index.project_id)
        assert refused.status_code == HTTP_CONFLICT
        assert problem_code(refused) == "content_unchanged"
        detail = await document(api, index, reservation.document_id)
        assert len(detail["versions"]) == 1
        assert_one_served(detail, file)
    finally:
        await cleanup(api, index, [reservation.document_id])


async def test_t175_case_3_two_concurrent_uploads_of_one_document_serve_the_newest(
    admin_api: AdminApi, index: MappedIndex, run_label: str
) -> None:
    api = admin_api
    name = f"idem-{run_label}-concurrent.docx"
    files = [variant("headings.docx", name, f"idem-{run_label}-3{part}") for part in "ab"]
    reservations = await asyncio.gather(
        *(
            api.reserve(index.index_id, file, index.project_id, confirm_update=True)
            for file in files
        )
    )
    document_ids = [reservation.document_id for reservation in reservations]
    try:
        assert len(set(document_ids)) == 1, "two documents for one name"
        await asyncio.gather(
            *(
                api.upload_content(index.index_id, reservation.version_id, file.data)
                for reservation, file in zip(reservations, files, strict=True)
            )
        )
        await asyncio.gather(
            *(api.submit(index.index_id, [reservation.version_id]) for reservation in reservations)
        )
        detail = await settled(api, index, document_ids[0])
        newest = max(reservations, key=lambda reservation: reservation.version_number)
        assert_one_served(detail, files[reservations.index(newest)])
        live = [d for d in await api.list_documents(index.index_id) if d["name"] == name]
        assert len(live) == 1
    finally:
        await cleanup(api, index, document_ids)


async def test_t175_case_4_a_document_deleted_during_embed_stays_deleted(
    admin_api: AdminApi, index: MappedIndex, run_label: str
) -> None:
    api = admin_api
    name = f"idem-{run_label}-delete.pdf"
    file = await asyncio.to_thread(
        lambda: upload_file(name, generated_pdf(DELETE_PAGES, label=f"idem-{run_label}-4"))
    )
    reservation = await api.upload(index.index_id, file, index.project_id)
    try:
        run_id = str((await api.submit(index.index_id, [reservation.version_id]))["runId"])

        async def at_embed() -> bool | None:
            detail = await api.get_run(run_id)
            [item] = [i for i in detail["items"] if i["operation"] == "ingest"]
            if (
                item["state"] in FINISHED_ITEM_STATES
                or detail["run"]["state"] in FINISHED_RUN_STATES
            ):
                pytest.fail("the item ended before it was seen at embed", pytrace=False)
            return True if stage_reached(item["stage"], "embed") else None

        await api.wait_until(at_embed, within=EMBED_WAIT_SECONDS, what="the item reaching embed")
        delete_run = await api.delete_document(index.index_id, reservation.document_id)
        assert delete_run is not None
        await api.wait_for_run(delete_run, within=RUN_WAIT_SECONDS)
        await api.wait_for_run(run_id, within=RUN_WAIT_SECONDS)
        assert await api.get_document(index.index_id, reservation.document_id) is None
        names = [d["name"] for d in await api.list_documents(index.index_id)]
        assert name not in names
    finally:
        await cleanup(api, index, [reservation.document_id])


async def test_t175_case_5_an_update_during_a_rebuild_is_what_readers_see_after(
    admin_api: AdminApi, index: MappedIndex, run_label: str
) -> None:
    api = admin_api
    name = f"idem-{run_label}-rebuild.pdf"
    first = variant("tables.pdf", name, f"idem-{run_label}-5a")
    update = variant("tables.pdf", name, f"idem-{run_label}-5b")
    reservation = await api.upload(index.index_id, first, index.project_id)
    try:
        run_id = (await api.submit(index.index_id, [reservation.version_id]))["runId"]
        await api.wait_for_run(str(run_id), within=RUN_WAIT_SECONDS)
        detail = await api.wait_for_index(
            index.index_id, {"ready", "reingest_required"}, within=RUN_WAIT_SECONDS
        )
        # A new analyzer can't be served by the active physical index, so the re-ingest
        # rebuilds into a new one.
        analyzer = ANALYZERS[1 - ANALYZERS.index(detail["languageAnalyzer"])]
        detail = await api.update_index(
            index.index_id, str(detail["rowVersion"]), {"languageAnalyzer": analyzer}
        )
        rebuild = await api.start_reingest(index.index_id, str(detail["rowVersion"]))
        assert rebuild["mode"] == "new_physical_index"
        second = await api.upload(index.index_id, update, index.project_id, confirm_update=True)
        submitted = await api.submit(index.index_id, [second.version_id])
        await api.wait_for_run(str(rebuild["runId"]), within=REBUILD_WAIT_SECONDS)
        await api.wait_for_run(str(submitted["runId"]), within=RUN_WAIT_SECONDS)
        await api.wait_for_index(index.index_id, {"ready"}, within=RUN_WAIT_SECONDS)
        assert_one_served(await settled(api, index, reservation.document_id), update)
    finally:
        await cleanup(api, index, [reservation.document_id])


async def test_t175_case_8_of_several_pending_versions_only_the_newest_is_ingested(
    admin_api: AdminApi, index: MappedIndex, run_label: str
) -> None:
    api = admin_api
    name = f"idem-{run_label}-pending.pdf"
    files = [variant("tables.pdf", name, f"idem-{run_label}-8{n}") for n in range(3)]
    versions: list[str] = []
    document_ids: list[str] = []
    try:
        for file in files:
            reservation = await api.upload(
                index.index_id, file, index.project_id, confirm_update=bool(versions)
            )
            versions.append(reservation.version_id)
            document_ids.append(reservation.document_id)
        assert len(set(document_ids)) == 1
        run_id = (await api.submit(index.index_id, versions))["runId"]
        await api.wait_for_run(str(run_id), within=RUN_WAIT_SECONDS)
        detail = await settled(api, index, document_ids[0])
        assert_one_served(detail, files[-1])
        states = {v["versionId"]: v["state"] for v in detail["versions"]}
        assert [states[version] for version in versions[:-1]] == ["superseded", "superseded"]
    finally:
        await cleanup(api, index, document_ids)


async def test_t175_case_9_a_delete_then_a_reupload_keeps_the_document_id(
    admin_api: AdminApi, index: MappedIndex, run_label: str
) -> None:
    api = admin_api
    name = f"idem-{run_label}-again.pdf"
    first = variant("tables.pdf", name, f"idem-{run_label}-9a")
    again = variant("tables.pdf", name, f"idem-{run_label}-9b")
    reservation = await api.upload(index.index_id, first, index.project_id)
    document_ids = [reservation.document_id]
    try:
        run_id = (await api.submit(index.index_id, [reservation.version_id]))["runId"]
        await api.wait_for_run(str(run_id), within=RUN_WAIT_SECONDS)
        await delete_documents(api, index.index_id, document_ids, within=RUN_WAIT_SECONDS)
        try:
            second = await api.upload(index.index_id, again, index.project_id)
        except AdminApiError as error:
            pytest.fail(f"the re-upload was refused: {error}", pytrace=False)
        document_ids.append(second.document_id)
        assert second.document_id == reservation.document_id
        run_id = (await api.submit(index.index_id, [second.version_id]))["runId"]
        await api.wait_for_run(str(run_id), within=RUN_WAIT_SECONDS)
        assert_one_served(await settled(api, index, second.document_id), again)
    finally:
        await cleanup(api, index, document_ids)
