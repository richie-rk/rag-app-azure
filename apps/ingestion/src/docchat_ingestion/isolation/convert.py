"""The Office conversion (`14-ingestion.md` section 15.11; ADR 0152): a DOCX or PPTX source becomes
a PDF in the `convert` child, under every isolation measure of the parsing children, and from then
on its version is processed exactly as a PDF source.

The parent writes the LibreOffice profile `lo-profile/user/registrymodifications.xcu` before the
hand-over, owned by root with mode 0644, with exactly `PROFILE`; the child runs `soffice` with
the command line of `soffice_command` as a subprocess with an argument list, never a shell, and
renames `out/source.pdf` to `converted.pdf`. The export options live in the profile, because the
LibreOffice 7.4 documentation gives no syntax for PDF export options on the command line; they
reduce what the converted PDF holds and aren't the control, since Render builds every page anew
from the rendition object schema whatever the converter wrote.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Final

from docchat_ingestion.isolation.results import (
    CONVERTED,
    DOCX,
    PPTX,
    SOURCE,
    ConvertFailed,
    Ok,
)

LIBREOFFICE_PACKAGE_VERSION: Final = "4:7.4.7-1+deb12u14"
"""The pinned `libreoffice-core-nogui` version; the image self-check fails the build when the
installed package differs."""

CONVERT_TIMEOUT_SECONDS: Final = 900

SOFFICE: Final = "/usr/lib/libreoffice/program/soffice"
LIBREOFFICE_DIRECTORY: Final = Path("/usr/lib/libreoffice")
UPDATE_CHECK_LIBRARY: Final = "libupdchklo.so"

PROFILE_DIRECTORY: Final = "lo-profile"
PROFILE_USER_DIRECTORY: Final = "lo-profile/user"
PROFILE_FILE: Final = "lo-profile/user/registrymodifications.xcu"
OUTPUT_DIRECTORY: Final = "out"
OUTPUT_FILE: Final = "out/source.pdf"

_SCRIPTING: Final = "/org.openoffice.Office.Common/Security/Scripting"
_PDF_EXPORT: Final = "/org.openoffice.Office.Common/Filter/PDF/Export"
_PROFILE_ITEMS: Final = (
    (_SCRIPTING, "DisableMacrosExecution", "true"),
    (_SCRIPTING, "MacroSecurityLevel", "3"),
    (_SCRIPTING, "OfficeBasic", "0"),
    (_SCRIPTING, "ExecutePlugins", "false"),
    (_SCRIPTING, "BlockUntrustedRefererLinks", "true"),
    ("/org.openoffice.Office.Writer/Content/Update", "Link", "2"),
    ("/org.openoffice.Office.Writer/Content/Update", "Field", "false"),
    ("/org.openoffice.Office.Writer/Filter/Import/VBA", "Load", "false"),
    ("/org.openoffice.Office.Writer/Filter/Import/VBA", "Executable", "false"),
    ("/org.openoffice.Office.Impress/Filter/Import/VBA", "Load", "false"),
    ("/org.openoffice.Office.Recovery/RecoveryInfo", "Enabled", "false"),
    ("/org.openoffice.Office.Recovery/AutoSave", "Enabled", "false"),
    ("/org.openoffice.Office.Common/Save/Document", "AutoSave", "false"),
    (_PDF_EXPORT, "ExportFormFields", "false"),
    (_PDF_EXPORT, "ExportNotes", "false"),
    (_PDF_EXPORT, "ExportNotesPages", "false"),
    (_PDF_EXPORT, "ExportHiddenSlides", "false"),
    (_PDF_EXPORT, "ExportPlaceholders", "false"),
    (_PDF_EXPORT, "ExportBookmarks", "false"),
    (_PDF_EXPORT, "ExportBookmarksToPDFDestination", "false"),
    (_PDF_EXPORT, "ConvertOOoTargetToPDFTarget", "false"),
    (_PDF_EXPORT, "ExportLinksRelativeFsys", "false"),
    (_PDF_EXPORT, "IsAddStream", "false"),
    (_PDF_EXPORT, "UseTaggedPDF", "false"),
)


def _profile() -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry"'
            ' xmlns:xs="http://www.w3.org/2001/XMLSchema"'
            ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        ),
    ]
    lines.extend(
        f'<item oor:path="{path}"><prop oor:name="{name}" oor:op="fuse">'
        f"<value>{value}</value></prop></item>"
        for path, name, value in _PROFILE_ITEMS
    )
    lines.append("</oor:items>")
    return ("\n".join(lines) + "\n").encode("utf-8")


PROFILE: Final = _profile()
"""The exact bytes of `registrymodifications.xcu` (section 15.11 item 3), in the format that
LibreOffice's configuration manager writes for that file."""

CONVERTER_VERSION: Final = (
    "lo-" + LIBREOFFICE_PACKAGE_VERSION + "/" + hashlib.sha256(PROFILE).hexdigest()[:12]
)
"""Recorded with each converted PDF; a re-ingest reuses a stored converted PDF only under the
same value (section 6.2 item 8)."""

FILTERS: Final = {
    DOCX: ("MS Word 2007 XML", "writer_pdf_Export"),
    PPTX: ("Impress MS PowerPoint 2007 XML", "impress_pdf_Export"),
}
"""The import filter named for each type, so LibreOffice never chooses one by content, and its
PDF export filter."""


def soffice_command(scratch: Path, content_type: str, soffice: str = SOFFICE) -> list[str]:
    """The conversion's argument list (section 15.11 item 8); no filter options are passed."""
    import_filter, export_filter = FILTERS[content_type]
    return [
        soffice,
        "--headless",
        "--norestore",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        f"-env:UserInstallation=file://{scratch}/{PROFILE_DIRECTORY}",
        f"--infilter={import_filter}",
        "--convert-to",
        f"pdf:{export_filter}",
        "--outdir",
        str(scratch / OUTPUT_DIRECTORY),
        SOURCE,
    ]


def convert_in_child(
    scratch: Path, content_type: str, soffice: str | None = None
) -> Ok | ConvertFailed:
    """The `convert` command, inside the child: run LibreOffice on `source.bin` in the scratch
    directory and rename its output to `converted.pdf`. The parent's wall clock and memory
    limits bound the run; no `out/source.pdf` is `convert_failed`."""
    if content_type not in FILTERS:
        return ConvertFailed(ok=False, code="convert_failed")
    subprocess.run(  # noqa: S603 - an argument list built from constants and the scratch path
        soffice_command(scratch, content_type, soffice or SOFFICE),
        cwd=scratch,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    output = scratch / OUTPUT_FILE
    if output.is_symlink() or not output.is_file():
        return ConvertFailed(ok=False, code="convert_failed")
    output.replace(scratch / CONVERTED)
    return Ok(ok=True)


def update_check_present(directory: Path = LIBREOFFICE_DIRECTORY) -> bool:
    """Whether a `libupdchklo.so` exists under the LibreOffice directory; the core package holds no
    online-update component, and the image self-check asserts it."""
    return any(path.name == UPDATE_CHECK_LIBRARY for path in directory.rglob(UPDATE_CHECK_LIBRARY))
