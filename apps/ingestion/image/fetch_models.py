"""Build-time download of the pinned models (`14-ingestion.md` section 15.3).

`python -I apps/ingestion/image/fetch_models.py apps/ingestion/models.lock.json /opt/docchat/models`
downloads every file the lock pins at its revision, checks each file's SHA-256, writes the Hugging
Face cache layout under `<target>/hf/hub` (`blobs/`, `snapshots/<revision>/`, and `refs/main`
naming the pinned commit), writes the layout model's initialisation parameters to
`<target>/layout/params.json` from the installed library's layout label map, and fails the build
on any mismatch. Nothing is fetched at run time; the image sets `HF_HUB_OFFLINE=1`.

The lock pins each model by repository, revision (commit hash) and the SHA-256 of every file:

    {
      "models": [
        {
          "repository": "unstructuredio/yolo_x_layout",
          "revision": "<40 hexadecimal characters>",
          "files": [{"path": "yolox_l0.05.onnx", "sha256": "<64 hexadecimal characters>"}]
        }
      ]
    }

Standard library only, so the build stage needs nothing installed to run it.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import sys
import urllib.request  # noqa: TID251 - a build-time script with the standard library only
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Final

HUB: Final = "https://huggingface.co"
TIMEOUT_SECONDS: Final = 60
CHUNK: Final = 1 << 20
LAYOUT_REPOSITORY: Final = "unstructuredio/yolo_x_layout"
LAYOUT_FILE: Final = "yolox_l0.05.onnx"
LAYOUT_LABEL_MODULE: Final = "unstructured_inference.models.yolox"
LAYOUT_LABEL_MAP: Final = "YOLOX_LABEL_MAP"
_REPOSITORY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
_REVISION: Final = re.compile(r"[0-9a-f]{40}")
_SHA256: Final = re.compile(r"[0-9a-f]{64}")

type Opener = Callable[[str], IO[bytes]]


class LockError(Exception):
    """The lock file doesn't have the documented form."""


class MismatchError(Exception):
    """A downloaded file's SHA-256 differs from the lock."""


@dataclass(frozen=True)
class PinnedFile:
    path: str
    sha256: str


@dataclass(frozen=True)
class PinnedModel:
    repository: str
    revision: str
    files: tuple[PinnedFile, ...]

    @property
    def cache_directory(self) -> str:
        """The cache's directory name: `models--<owner>--<name>`."""
        return "models--" + self.repository.replace("/", "--")


def _safe_path(text: object) -> str:
    if not isinstance(text, str) or not text:
        raise LockError("a file path is missing")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise LockError("a file path leaves its snapshot")
    return str(path)


def read_lock(path: Path) -> list[PinnedModel]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as failure:
        raise LockError("the lock can't be read") from failure
    if not isinstance(document, dict) or set(document) != {"models"}:
        raise LockError("the lock holds exactly one key, models")
    models: list[PinnedModel] = []
    for entry in document["models"]:
        if not isinstance(entry, dict) or set(entry) != {"repository", "revision", "files"}:
            raise LockError("a model holds exactly repository, revision and files")
        repository, revision = entry["repository"], entry["revision"]
        if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
            raise LockError("a repository isn't owner/name")
        if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
            raise LockError("a revision isn't a commit hash")
        files: list[PinnedFile] = []
        for item in entry["files"]:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise LockError("a file holds exactly path and sha256")
            digest = item["sha256"]
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                raise LockError("a SHA-256 isn't 64 hexadecimal characters")
            files.append(PinnedFile(_safe_path(item["path"]), digest))
        if not files:
            raise LockError("a model pins no file")
        models.append(PinnedModel(repository, revision, tuple(files)))
    return models


def file_url(model: PinnedModel, pinned: PinnedFile) -> str:
    return f"{HUB}/{model.repository}/resolve/{model.revision}/{pinned.path}"


def _open_url(url: str) -> IO[bytes]:
    # The standard library's client: the build stage installs nothing to run this script.
    headers = {"User-Agent": "docchat-image-build"}
    request = urllib.request.Request(url, headers=headers)  # noqa: S310, TID251
    response: IO[bytes] = urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)  # noqa: S310, TID251
    return response


def fetch_file(model: PinnedModel, pinned: PinnedFile, hub: Path, opener: Opener) -> Path:
    """Download one file into the model's blobs, check its hash, and link it into the pinned
    revision's snapshot; returns the snapshot path."""
    root = hub / model.cache_directory
    blob = root / "blobs" / pinned.sha256
    blob.parent.mkdir(parents=True, exist_ok=True)
    partial = blob.with_name(blob.name + ".incomplete")
    hasher = hashlib.sha256()
    with opener(file_url(model, pinned)) as response, partial.open("wb") as output:
        while chunk := response.read(CHUNK):
            hasher.update(chunk)
            output.write(chunk)
    if hasher.hexdigest() != pinned.sha256:
        partial.unlink()
        raise MismatchError(f"{model.repository}/{pinned.path} doesn't match its SHA-256")
    partial.replace(blob)
    snapshot = root / "snapshots" / model.revision / pinned.path
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.is_symlink() or snapshot.exists():
        snapshot.unlink()
    snapshot.symlink_to(os.path.relpath(blob, snapshot.parent))
    refs = root / "refs"
    refs.mkdir(exist_ok=True)
    (refs / "main").write_text(model.revision, encoding="ascii")
    return snapshot


def layout_label_map() -> dict[str, str]:
    """The installed layout library's label map, keyed by class index as a string."""
    module = importlib.import_module(LAYOUT_LABEL_MODULE)
    labels = getattr(module, LAYOUT_LABEL_MAP)
    return {str(index): str(label) for index, label in dict(labels).items()}


def write_layout_params(target: Path, model_path: Path, labels: dict[str, str]) -> Path:
    """`params.json`, which `UNSTRUCTURED_DEFAULT_MODEL_INITIALIZE_PARAMS_JSON_PATH` names."""
    params = target / "layout" / "params.json"
    params.parent.mkdir(parents=True, exist_ok=True)
    params.write_text(
        json.dumps({"model_path": str(model_path), "label_map": labels}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return params


def fetch_models(
    lock: Path,
    target: Path,
    *,
    opener: Opener = _open_url,
    labels: Callable[[], dict[str, str]] = layout_label_map,
) -> Path:
    """Fetch every pinned file and write the layout parameters; returns `params.json`."""
    models = read_lock(lock)
    hub = target / "hf" / "hub"
    layout: Path | None = None
    for model in models:
        for pinned in model.files:
            snapshot = fetch_file(model, pinned, hub, opener)
            if model.repository == LAYOUT_REPOSITORY and pinned.path == LAYOUT_FILE:
                layout = snapshot
    if layout is None:
        raise LockError(f"the lock pins no {LAYOUT_REPOSITORY} {LAYOUT_FILE}")
    return write_layout_params(target, layout, labels())


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:  # noqa: PLR2004 - the lock and the target
        print("usage: fetch_models.py <models.lock.json> <target>", file=sys.stderr)  # noqa: T201
        return 2
    lock, target = Path(arguments[0]), Path(arguments[1])
    try:
        fetch_models(lock, target)
    except (LockError, MismatchError, OSError) as failure:
        print(f"fetch_models: {failure}", file=sys.stderr)  # noqa: T201
        shutil.rmtree(target / "hf", ignore_errors=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
