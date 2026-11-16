from __future__ import annotations

import pytest

from docchat_core.settings.hosting import AZURE_HOST_VARIABLES, running_in_azure


def test_the_platform_variables() -> None:
    assert AZURE_HOST_VARIABLES == ("WEBSITE_SITE_NAME", "WEBSITE_INSTANCE_ID")


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, False),
        ({"WEBSITE_SITE_NAME": "app-docchat-api-dev"}, True),
        ({"WEBSITE_INSTANCE_ID": "a1b2"}, True),
        ({"WEBSITE_INSTANCE_ID": ""}, True),
        ({"WEBSITE_HOSTNAME": "x"}, False),
    ],
)
def test_running_in_azure(environ: dict[str, str], expected: bool) -> None:
    assert running_in_azure(environ) is expected


def test_running_in_azure_reads_the_process_environment(settings_env: pytest.MonkeyPatch) -> None:
    assert running_in_azure() is False

    settings_env.setenv("WEBSITE_SITE_NAME", "func-docchat-ingest-dev")

    assert running_in_azure() is True
