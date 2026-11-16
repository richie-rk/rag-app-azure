"""Unit tests of the bootstrap's ci-runner step (83-infrastructure.md section 11.3; T-460)."""

from __future__ import annotations

import importlib
import json
from typing import TYPE_CHECKING, Any

from bootstrap_harness import ORGANIZATION, REPOSITORY, SUBSCRIPTION, config_data

if TYPE_CHECKING:
    from bootstrap_harness import Harness

bootstrap = importlib.import_module("bootstrap")

ORG = f"/orgs/{ORGANIZATION}"
SETTINGS_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-docchat-shared"
    "/providers/GitHub.Network/networkSettings/ghns-docchat-ci"
)
GITHUB_ID = "B5F4A0C1D2E3"
REPOSITORY_ID = 42
CONFIGURATION_ID = "NC0000001"
GROUP_ID = 7
RUNNER_ID = 9


def run(harness: Harness) -> int:
    code: int = bootstrap.main(["--step", "ci-runner"], harness.runtime())
    return code


def private_networking(harness: Harness, location: str = "eastus2") -> None:
    harness.write_config(config_data(ciPrivateNetworking=True, ciNetworkLocation=location))


def settings_lookup() -> tuple[str, ...]:
    return (
        "az",
        "resource",
        "list",
        "--resource-group",
        "rg-docchat-shared",
        "--subscription",
        SUBSCRIPTION,
        "--resource-type",
        "GitHub.Network/networkSettings",
        "--name",
        "ghns-docchat-ci",
        "--query",
        "[].id",
        "--output",
        "tsv",
    )


def settings_deployment(harness: Harness) -> tuple[str, ...]:
    return (
        "az",
        "deployment",
        "group",
        "create",
        "--name",
        "docchat-bootstrap-ci-network-settings",
        "--resource-group",
        "rg-docchat-shared",
        "--subscription",
        SUBSCRIPTION,
        "--parameters",
        harness.template("ci-network-settings.bicepparam"),
        "--output",
        "none",
    )


def post(path: str) -> tuple[str, ...]:
    return ("gh", "api", "--method", "POST", path, "--input", "-")


def record_offer(harness: Harness, *, sizes: list[str], images: list[str]) -> None:
    harness.runner.answer_json(
        ("gh", "api", f"{ORG}/actions/hosted-runners/machine-sizes?per_page=100"),
        {"total_count": len(sizes), "machine_specs": [{"id": size} for size in sizes]},
    )
    harness.runner.answer_json(
        ("gh", "api", f"{ORG}/actions/hosted-runners/images/github-owned?per_page=100"),
        {
            "total_count": len(images),
            "images": [{"id": image, "source": "github"} for image in images],
        },
    )


def record_preparation(harness: Harness, *, settings_exist: bool) -> None:
    record_offer(harness, sizes=["2-core", "4-core"], images=["ubuntu-24.04"])
    harness.runner.answer_json(
        (
            "gh",
            "api",
            "graphql",
            "-f",
            "query=query($login: String!) { organization(login: $login) { databaseId } }",
            "-F",
            f"login={ORGANIZATION}",
        ),
        {"data": {"organization": {"databaseId": 123456}}},
    )
    if not settings_exist:
        harness.runner.answer(settings_lookup(), "")
        harness.runner.answer(settings_deployment(harness))
    harness.runner.answer(settings_lookup(), f"{SETTINGS_ID}\n")
    harness.runner.answer(
        (
            "az",
            "resource",
            "show",
            "--ids",
            SETTINGS_ID,
            "--api-version",
            "2024-04-02",
            "--query",
            "tags.GitHubId",
            "--output",
            "tsv",
        ),
        f"{GITHUB_ID}\n",
    )
    harness.runner.answer_json(
        ("gh", "api", f"/repos/{ORGANIZATION}/{REPOSITORY}"),
        {"id": REPOSITORY_ID, "private": False, "visibility": "public"},
    )


def record_objects(harness: Harness, *, exist: bool) -> None:
    lists = (
        (
            f"{ORG}/settings/network-configurations",
            "network_configurations",
            "docchat-ci",
            CONFIGURATION_ID,
        ),
        (f"{ORG}/actions/runner-groups", "runner_groups", "docchat-ci-private", GROUP_ID),
        (f"{ORG}/actions/hosted-runners", "runners", "docchat-ci-private", RUNNER_ID),
    )
    for path, key, name, object_id in lists:
        items = [{"id": "other", "name": "other"}] + (
            [{"id": object_id, "name": name}] if exist else []
        )
        harness.runner.answer_json(("gh", "api", f"{path}?per_page=100"), {key: items})
        if not exist:
            harness.runner.answer_json(post(path), {"id": object_id, "name": name})


def record_read_back(harness: Harness, **runner_changes: Any) -> None:
    harness.runner.answer_json(
        ("gh", "api", f"{ORG}/settings/network-configurations/{CONFIGURATION_ID}"),
        {"id": CONFIGURATION_ID, "compute_service": "actions", "network_settings_ids": [GITHUB_ID]},
    )
    harness.runner.answer_json(
        ("gh", "api", f"{ORG}/actions/runner-groups/{GROUP_ID}"),
        {
            "id": GROUP_ID,
            "visibility": "selected",
            "allows_public_repositories": True,
            "restricted_to_workflows": False,
            "network_configuration_id": CONFIGURATION_ID,
        },
    )
    harness.runner.answer_json(
        ("gh", "api", f"{ORG}/actions/runner-groups/{GROUP_ID}/repositories?per_page=100"),
        {"total_count": 1, "repositories": [{"id": REPOSITORY_ID}]},
    )
    runner = {
        "id": RUNNER_ID,
        "runner_group_id": GROUP_ID,
        "image_details": {"id": "ubuntu-24.04"},
        "machine_size_details": {"id": "2-core"},
        "maximum_runners": 10,
        "public_ip_enabled": False,
    }
    harness.runner.answer_json(
        ("gh", "api", f"{ORG}/actions/hosted-runners/{RUNNER_ID}"), runner | runner_changes
    )


def test_t460_ci_runner_is_skipped_and_makes_no_call_by_default(harness: Harness) -> None:
    assert run(harness) == 0

    assert harness.reports() == [{"step": "ci-runner", "status": "skipped"}]
    assert harness.runner.calls == []


def test_t460_ci_runner_stops_with_ci_region_unsupported_before_any_call(harness: Harness) -> None:
    private_networking(harness, location="northeurope")

    assert run(harness) == 2

    assert harness.refusal()["code"] == "ci_region_unsupported"
    assert harness.runner.calls == []


def test_t460_ci_runner_stops_when_no_2_core_size_is_offered(harness: Harness) -> None:
    private_networking(harness)
    record_offer(harness, sizes=["4-core"], images=["ubuntu-24.04"])

    assert run(harness) == 2

    assert harness.refusal()["code"] == "ci_private_networking_unavailable"
    assert harness.runner.called("az") == []
    assert harness.runner.called("gh", "api", "--method", "POST") == []


def test_t460_ci_runner_stops_when_no_ubuntu_24_04_image_is_offered(harness: Harness) -> None:
    private_networking(harness)
    record_offer(harness, sizes=["2-core"], images=["ubuntu-22.04"])

    assert run(harness) == 2

    assert harness.refusal()["code"] == "ci_private_networking_unavailable"


def test_t460_ci_runner_stops_when_the_organization_is_refused_the_list(harness: Harness) -> None:
    private_networking(harness)
    harness.runner.answer(
        ("gh", "api", f"{ORG}/actions/hosted-runners/machine-sizes?per_page=100"),
        returncode=1,
        stderr="gh: Not Found (HTTP 404)\n",
    )

    assert run(harness) == 2

    assert harness.refusal()["code"] == "ci_private_networking_unavailable"


def test_t460_ci_runner_stops_when_a_create_call_answers_403(harness: Harness) -> None:
    private_networking(harness)
    record_preparation(harness, settings_exist=True)
    path = f"{ORG}/settings/network-configurations"
    harness.runner.answer_json(
        ("gh", "api", f"{path}?per_page=100"), {"network_configurations": []}
    )
    harness.runner.answer(post(path), returncode=1, stderr="gh: Forbidden (HTTP 403)\n")

    assert run(harness) == 2

    refusal = harness.refusal()
    assert refusal["code"] == "ci_private_networking_unavailable"
    assert "403" in refusal["detail"]


def test_t460_ci_runner_creates_the_settings_and_the_three_objects_when_absent(
    harness: Harness,
) -> None:
    private_networking(harness)
    record_preparation(harness, settings_exist=False)
    record_objects(harness, exist=False)
    record_read_back(harness)

    assert run(harness) == 0

    (deployment,) = harness.runner.called("az", "deployment")
    assert deployment.env == {"DOCCHAT_BOOTSTRAP_GITHUB_DATABASE_ID": "123456"}
    bodies = {
        call.args[4]: json.loads(call.stdin or "{}")
        for call in harness.runner.called("gh", "api", "--method", "POST")
    }
    assert bodies[f"{ORG}/settings/network-configurations"] == {
        "name": "docchat-ci",
        "compute_service": "actions",
        "network_settings_ids": [GITHUB_ID],
    }
    assert bodies[f"{ORG}/actions/runner-groups"] == {
        "name": "docchat-ci-private",
        "visibility": "selected",
        "selected_repository_ids": [REPOSITORY_ID],
        "allows_public_repositories": True,
        "restricted_to_workflows": False,
        "network_configuration_id": CONFIGURATION_ID,
    }
    assert bodies[f"{ORG}/actions/hosted-runners"] == {
        "name": "docchat-ci-private",
        "image": {"id": "ubuntu-24.04", "source": "github"},
        "size": "2-core",
        "runner_group_id": GROUP_ID,
        "maximum_runners": 10,
        "enable_static_ip": False,
    }
    assert harness.reports() == [{"step": "ci-runner", "status": "succeeded"}]


def test_t460_ci_runner_creates_nothing_that_exists(harness: Harness) -> None:
    private_networking(harness)
    record_preparation(harness, settings_exist=True)
    record_objects(harness, exist=True)
    record_read_back(harness)

    assert run(harness) == 0

    assert harness.runner.called("az", "deployment") == []
    assert harness.runner.called("gh", "api", "--method", "POST") == []


def test_t460_ci_runner_stops_with_ci_runner_mismatch_on_a_differing_read_back(
    harness: Harness,
) -> None:
    private_networking(harness)
    record_preparation(harness, settings_exist=True)
    record_objects(harness, exist=True)
    record_read_back(harness, maximum_runners=20, public_ip_enabled=True)

    assert run(harness) == 2

    refusal = harness.refusal()
    assert refusal["code"] == "ci_runner_mismatch"
    assert "maximum_runners" in refusal["detail"]
    assert "public_ip_enabled" in refusal["detail"]


def test_ci_runner_stops_with_ci_runner_mismatch_without_a_github_id_tag(harness: Harness) -> None:
    private_networking(harness)
    record_offer(harness, sizes=["2-core"], images=["ubuntu-24.04"])
    harness.runner.answer_json(
        (
            "gh",
            "api",
            "graphql",
            "-f",
            "query=query($login: String!) { organization(login: $login) { databaseId } }",
            "-F",
            f"login={ORGANIZATION}",
        ),
        {"data": {"organization": {"databaseId": 123456}}},
    )
    harness.runner.answer(settings_lookup(), f"{SETTINGS_ID}\n")
    harness.runner.answer(
        (
            "az",
            "resource",
            "show",
            "--ids",
            SETTINGS_ID,
            "--api-version",
            "2024-04-02",
            "--query",
            "tags.GitHubId",
            "--output",
            "tsv",
        ),
        "\n",
    )

    assert run(harness) == 2

    assert harness.refusal()["code"] == "ci_runner_mismatch"
