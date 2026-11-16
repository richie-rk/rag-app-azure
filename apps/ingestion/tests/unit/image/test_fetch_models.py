"""`apps/ingestion/image/fetch_models.py`: the build-time download of the pinned models
(`14-ingestion.md` section 15.3). The hub is a stand-in opener; the layout library's label map is
a stand-in too, so the tests need no download."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import IO

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "image" / "fetch_models.py"
REVISION = "0123456789abcdef0123456789abcdef01234567"
FILES = {
    "unstructuredio/yolo_x_layout": {"yolox_l0.05.onnx": b"onnx model"},
    "microsoft/table-transformer-structure-recognition": {
        "config.json": b"{}",
        "model.safetensors": b"weights",
    },
}


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fetch_models", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fetch_models"] = module
    spec.loader.exec_module(module)
    return module


fetch = load()


def lock_document(files: dict[str, dict[str, bytes]] = FILES) -> dict[str, object]:
    return {
        "models": [
            {
                "repository": repository,
                "revision": REVISION,
                "files": [
                    {"path": path, "sha256": hashlib.sha256(data).hexdigest()}
                    for path, data in contents.items()
                ],
            }
            for repository, contents in files.items()
        ]
    }


def write_lock(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "models.lock.json"
    path.write_text(json.dumps(document))
    return path


def opener(served: dict[str, bytes]) -> fetch.Opener:  # type: ignore[name-defined]
    def open_url(url: str) -> IO[bytes]:
        return io.BytesIO(served[url])

    return open_url


def served(files: dict[str, dict[str, bytes]] = FILES) -> dict[str, bytes]:
    return {
        f"https://huggingface.co/{repository}/resolve/{REVISION}/{path}": data
        for repository, contents in files.items()
        for path, data in contents.items()
    }


def test_every_file_lands_in_the_cache_layout_with_its_revision(tmp_path: Path) -> None:
    lock = write_lock(tmp_path, lock_document())
    target = tmp_path / "models"

    params = fetch.fetch_models(
        lock, target, opener=opener(served()), labels=lambda: {"0": "Caption"}
    )

    hub = target / "hf" / "hub"
    table = hub / "models--microsoft--table-transformer-structure-recognition"
    assert (table / "refs" / "main").read_text() == REVISION
    assert (table / "snapshots" / REVISION / "model.safetensors").read_bytes() == b"weights"
    assert (table / "snapshots" / REVISION / "model.safetensors").is_symlink()
    blob = table / "blobs" / hashlib.sha256(b"weights").hexdigest()
    assert blob.read_bytes() == b"weights"
    layout = (
        hub / "models--unstructuredio--yolo_x_layout" / "snapshots" / REVISION / "yolox_l0.05.onnx"
    )
    assert json.loads(params.read_text()) == {
        "model_path": str(layout),
        "label_map": {"0": "Caption"},
    }
    assert params == target / "layout" / "params.json"


def test_a_file_that_doesnt_match_its_hash_fails_the_build(tmp_path: Path) -> None:
    lock = write_lock(tmp_path, lock_document())
    corrupted = served()
    corrupted[next(iter(corrupted))] = b"substituted"

    with pytest.raises(fetch.MismatchError):
        fetch.fetch_models(lock, tmp_path / "models", opener=opener(corrupted), labels=dict)
    assert not list((tmp_path / "models").rglob("*.incomplete"))


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"models": [], "extra": 1},
        {"models": [{"repository": "no-owner", "revision": REVISION, "files": []}]},
        {"models": [{"repository": "a/b", "revision": "main", "files": []}]},
        {"models": [{"repository": "a/b", "revision": REVISION, "files": []}]},
        {
            "models": [
                {
                    "repository": "a/b",
                    "revision": REVISION,
                    "files": [{"path": "../x", "sha256": "0" * 64}],
                }
            ]
        },
        {
            "models": [
                {
                    "repository": "a/b",
                    "revision": REVISION,
                    "files": [{"path": "/x", "sha256": "0" * 64}],
                }
            ]
        },
        {
            "models": [
                {
                    "repository": "a/b",
                    "revision": REVISION,
                    "files": [{"path": "x", "sha256": "short"}],
                }
            ]
        },
        {"models": [{"repository": "a/b", "revision": REVISION, "files": [{"path": "x"}]}]},
        {"models": [{"repository": "a/b", "revision": REVISION}]},
        {
            "models": [
                {
                    "repository": "a/b",
                    "revision": REVISION,
                    "files": [{"path": "", "sha256": "0" * 64}],
                }
            ]
        },
    ],
)
def test_a_lock_outside_the_documented_form_is_refused(tmp_path: Path, document: object) -> None:
    with pytest.raises(fetch.LockError):
        fetch.read_lock(write_lock(tmp_path, document))


def test_an_unreadable_lock_and_a_lock_without_the_layout_model_are_refused(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("{")
    files = {"microsoft/table-transformer-structure-recognition": {"config.json": b"{}"}}
    lock = write_lock(tmp_path, lock_document(files))

    with pytest.raises(fetch.LockError):
        fetch.read_lock(tmp_path / "broken.json")
    with pytest.raises(fetch.LockError):
        fetch.fetch_models(lock, tmp_path / "models", opener=opener(served(files)), labels=dict)


def test_the_label_map_comes_from_the_installed_layout_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = SimpleNamespace(YOLOX_LABEL_MAP={0: "Caption", 1: "Footnote"})
    monkeypatch.setattr(fetch.importlib, "import_module", lambda name: module)

    assert fetch.layout_label_map() == {"0": "Caption", "1": "Footnote"}


def test_main_reports_usage_and_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert fetch.main([]) == 2
    assert fetch.main([str(tmp_path / "absent.json"), str(tmp_path / "models")]) == 1
    assert "fetch_models:" in capsys.readouterr().err


def test_the_hub_opener_requests_the_pinned_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, float]] = []

    def urlopen(request: object, timeout: float) -> IO[bytes]:
        seen.append((request.full_url, timeout))  # type: ignore[attr-defined]
        return io.BytesIO(b"x")

    monkeypatch.setattr(fetch.urllib.request, "urlopen", urlopen)

    assert fetch._open_url("https://huggingface.co/a/b/resolve/x/y").read() == b"x"
    assert seen == [("https://huggingface.co/a/b/resolve/x/y", 60)]


def test_the_committed_lock_pins_the_section_15_3_models() -> None:
    """The captured `apps/ingestion/models.lock.json` reads cleanly and pins the layout model's
    ONNX file, the table-structure model and its ResNet-18 backbone, each at a commit."""
    models = fetch.read_lock(SCRIPT.parents[1] / "models.lock.json")
    pinned = {model.repository: {file.path for file in model.files} for model in models}

    assert pinned == {
        "unstructuredio/yolo_x_layout": {"yolox_l0.05.onnx"},
        "microsoft/table-transformer-structure-recognition": {
            "config.json",
            "model.safetensors",
            "preprocessor_config.json",
        },
        "timm/resnet18.a1_in1k": {"model.safetensors"},
    }
