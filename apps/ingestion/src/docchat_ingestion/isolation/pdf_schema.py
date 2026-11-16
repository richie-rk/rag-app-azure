"""The rendition object schema (`14-ingestion.md` sections 14.2, 14.7 and 14.8, and section 15.6
item 14; ADR 0145): the schema, the copier `copy_page`, the checker `check_file`, the steps of
the `render`, `assemble` and `verify-render` commands, and `RENDITION_RULES_VERSION`.

Each of the three children imports this module in its own process; no other module decides what a
rendition holds. A rendition holds only what draws its pages: every page file is built anew in a
fresh `pikepdf.Pdf` from the keys this schema lists, so no annotation, link, action or catalogue
entry of the source reaches it, and `verify-render` checks every file once more, against the same
schema and against two nets that read every object by its keys, names and `/S` alone.

Every object has a kind, given by the position it was reached from; where one position admits
several kinds, a key that ISO 32000-1 requires of each of them decides (`Choice`). Keys are visited
in sorted order, so the copy and the walk are the same every time, and an indirect object is
copied or checked once per kind.
"""

from __future__ import annotations

import io
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import pikepdf
from pikepdf import Array, Dictionary, Name, Pdf, Stream, String

from docchat_ingestion.isolation.results import (
    DOCUMENT,
    DOCUMENT_VERIFIED,
    SOURCE,
    AssembleOk,
    ChildRequest,
    Ok,
    RenderFailed,
    RenderOk,
    RenditionFailed,
    page_name,
    verified_name,
)

if TYPE_CHECKING:
    from pikepdf import Object

RENDITION_RULES_VERSION: Final = 1
"""Raised by 1 with every change to this module and with every change of the pinned `pikepdf` or
of the qpdf it bundles; the rendition rules version unit test holds the table of values."""

DEPTH_LIMIT: Final = 64
PAGE_CONTAINER_CAP: Final = 100_000
DOCUMENT_CONTAINER_CAP: Final = 1_000_000
PAGE_TREE_DEPTH_LIMIT: Final = 16
FINISHED_PAGES_POLL_SECONDS: Final = 0.1

type Mode = Literal["pages", "document"]

NET_NAMES: Final = frozenset(
    {
        "/JS",
        "/JavaScript",
        "/URI",
        "/Launch",
        "/GoToR",
        "/GoToE",
        "/EmbeddedFile",
        "/EmbeddedFiles",
        "/AF",
        "/EF",
        "/RF",
        "/FS",
        "/Filespec",
        "/RichMedia",
        "/XFA",
        "/PresSteps",
        "/NA",
        "/PA",
        "/AA",
        "/OpenAction",
        "/Outlines",
        "/Ref",
        "/OPI",
        "/URL",
        "/Annots",
    }
)
"""The 25 names of the net (invariant 5): no key and no name of any object, and none in a
dictionary written inline in a content stream."""

ACTION_TYPES: Final = frozenset(
    {
        "/GoTo",
        "/GoToR",
        "/GoToE",
        "/Launch",
        "/Thread",
        "/URI",
        "/Sound",
        "/Movie",
        "/Hide",
        "/Named",
        "/SubmitForm",
        "/ResetForm",
        "/ImportData",
        "/JavaScript",
        "/SetOCGState",
        "/Rendition",
        "/Trans",
        "/GoTo3DView",
    }
)
"""The 18 action types of ISO 32000-1 Table 198 (invariant 6): no dictionary whose `/S` is one."""

STREAM_FILTERS: Final = frozenset(
    {
        "/ASCIIHexDecode",
        "/ASCII85Decode",
        "/LZWDecode",
        "/FlateDecode",
        "/RunLengthDecode",
        "/CCITTFaxDecode",
        "/JBIG2Decode",
        "/DCTDecode",
        "/JPXDecode",
    }
)
"""The standard filters of Table 6 other than `/Crypt`."""

CONTENT_OPERATORS: Final = "BDC DP BI ID EI"


class SchemaError(Exception):
    """A page or file the schema refuses: `render_failed` in `render`, a refusal in
    `verify-render`."""


class ContainerCapError(SchemaError):
    """More containers than the cap: `render_too_large` in `render`, `too_large` in
    `assemble`."""


# Value specifications. A spec describes the form a value must have in its position.


@dataclass(frozen=True)
class Scalar:
    form: Literal["number", "integer", "boolean", "string", "name"]
    names: frozenset[str] | None = None
    integers: frozenset[int] | None = None
    multiple_of: int | None = None
    positive: bool = False


@dataclass(frozen=True)
class TypeKey:
    """`/Type`, which holds only the kind's own value; another value is left out in a copy."""

    value: str


@dataclass(frozen=True)
class ArrayOf:
    item: Spec
    length: int | None = None


@dataclass(frozen=True)
class Positions:
    """An array whose elements each have their own spec; the last `optional` may be absent."""

    items: tuple[Spec, ...]
    optional: int = 0


@dataclass(frozen=True)
class KindRef:
    """→ K: an object of kind K, direct or indirect."""

    name: str


@dataclass(frozen=True)
class DictOf:
    """A dictionary whose every value has the spec `item`."""

    item: Spec


@dataclass(frozen=True)
class OneOf:
    """Several forms, chosen by the value's own form (a name, an array, a dictionary)."""

    options: tuple[Spec, ...]


@dataclass(frozen=True)
class Named:
    """A spec defined by name, for the one recursive form (`/VE`)."""

    name: str


@dataclass(frozen=True)
class ColourSpaceSpec:
    """A colour space: a device name, or an array chosen by its family name."""


type Spec = (
    Scalar | TypeKey | ArrayOf | Positions | KindRef | DictOf | OneOf | Named | ColourSpaceSpec
)


@dataclass(frozen=True)
class KindDef:
    name: str
    stream: bool
    keys: Mapping[str, Spec]


@dataclass(frozen=True)
class Choice:
    """A position whose kind a required key decides; `omit_unknown` leaves out (in a copy) what
    no variant names, where leaving it out changes nothing the page draws."""

    name: str
    key: str
    variants: Mapping[str | int, str]
    omit_unknown: bool = False


NUMBER: Final = Scalar("number")
INTEGER: Final = Scalar("integer")
BOOLEAN: Final = Scalar("boolean")
STRING: Final = Scalar("string")
NAME: Final = Scalar("name")
NUMBERS: Final = ArrayOf(NUMBER)
NUMBERS_2: Final = ArrayOf(NUMBER, 2)
NUMBERS_4: Final = ArrayOf(NUMBER, 4)
NUMBERS_6: Final = ArrayOf(NUMBER, 6)
NUMBER_OR_NUMBERS: Final = OneOf((NUMBER, NUMBERS))
NAMES: Final = ArrayOf(NAME)
NAME_OR_NAMES: Final = OneOf((NAME, NAMES))
COLOUR_SPACE: Final = ColourSpaceSpec()
FUNCTION: Final = OneOf((KindRef("function"), ArrayOf(KindRef("function"))))


def _names(*values: str) -> Scalar:
    return Scalar("name", names=frozenset(values))


_SHADING_COMMON: Final[dict[str, Spec]] = {
    "/ShadingType": INTEGER,
    "/ColorSpace": COLOUR_SPACE,
    "/Background": NUMBERS,
    "/BBox": NUMBERS_4,
    "/AntiAlias": BOOLEAN,
}
_FUNCTION_COMMON: Final[dict[str, Spec]] = {
    "/FunctionType": INTEGER,
    "/Domain": NUMBER_OR_NUMBERS,
    "/Range": NUMBER_OR_NUMBERS,
}
_SIMPLE_FONT_NUMBERS: Final[dict[str, Spec]] = {
    "/FirstChar": INTEGER,
    "/LastChar": INTEGER,
    "/Widths": NUMBERS,
    "/FontDescriptor": KindRef("font descriptor"),
    "/ToUnicode": KindRef("cmap"),
}
_WIDTHS: Final = ArrayOf(OneOf((NUMBER, NUMBERS)))
_FONT_DESCRIPTOR_NUMBERS: Final = (
    "/FontWeight",
    "/Flags",
    "/ItalicAngle",
    "/Ascent",
    "/Descent",
    "/Leading",
    "/CapHeight",
    "/XHeight",
    "/StemV",
    "/StemH",
    "/AvgWidth",
    "/MaxWidth",
    "/MissingWidth",
)
_INTEGER_OR_BOOLEAN_PARMS: Final[dict[str, dict[str, Spec]]] = {
    "/LZWDecode": {
        "/Predictor": INTEGER,
        "/Colors": INTEGER,
        "/BitsPerComponent": INTEGER,
        "/Columns": INTEGER,
        "/EarlyChange": INTEGER,
    },
    "/FlateDecode": {
        "/Predictor": INTEGER,
        "/Colors": INTEGER,
        "/BitsPerComponent": INTEGER,
        "/Columns": INTEGER,
    },
    "/CCITTFaxDecode": {
        "/K": INTEGER,
        "/EndOfLine": BOOLEAN,
        "/EncodedByteAlign": BOOLEAN,
        "/Columns": INTEGER,
        "/Rows": INTEGER,
        "/EndOfBlock": BOOLEAN,
        "/BlackIs1": BOOLEAN,
        "/DamagedRowsBeforeError": INTEGER,
    },
    "/JBIG2Decode": {"/JBIG2Globals": KindRef("plain stream")},
    "/DCTDecode": {"/ColorTransform": INTEGER},
}

KINDS: Final[dict[str, KindDef | Choice]] = {
    "resources": KindDef(
        "resources",
        stream=False,
        keys={
            "/ExtGState": DictOf(KindRef("graphics state")),
            "/ColorSpace": DictOf(COLOUR_SPACE),
            "/Pattern": DictOf(KindRef("pattern")),
            "/Shading": DictOf(KindRef("shading")),
            "/XObject": DictOf(KindRef("xobject")),
            "/Font": DictOf(KindRef("font")),
            "/Properties": DictOf(KindRef("properties entry")),
            "/ProcSet": NAMES,
        },
    ),
    "page content stream": KindDef("page content stream", stream=True, keys={}),
    "glyph procedure": KindDef("glyph procedure", stream=True, keys={}),
    "plain stream": KindDef("plain stream", stream=True, keys={}),
    "graphics state": KindDef(
        "graphics state",
        stream=False,
        keys={
            "/Type": TypeKey("/ExtGState"),
            "/LW": NUMBER,
            "/LC": NUMBER,
            "/LJ": NUMBER,
            "/ML": NUMBER,
            "/OPM": NUMBER,
            "/FL": NUMBER,
            "/SM": NUMBER,
            "/CA": NUMBER,
            "/ca": NUMBER,
            "/D": Positions((NUMBERS, NUMBER)),
            "/RI": NAME,
            "/OP": BOOLEAN,
            "/op": BOOLEAN,
            "/SA": BOOLEAN,
            "/AIS": BOOLEAN,
            "/TK": BOOLEAN,
            "/BM": NAME_OR_NAMES,
            "/SMask": OneOf((_names("/None"), KindRef("soft mask"))),
            "/Font": Positions((KindRef("font"), NUMBER)),
        },
    ),
    "soft mask": KindDef(
        "soft mask",
        stream=False,
        keys={
            "/Type": TypeKey("/Mask"),
            "/S": _names("/Alpha", "/Luminosity"),
            "/G": KindRef("form"),
            "/BC": NUMBERS,
            "/TR": OneOf((KindRef("function"), ArrayOf(KindRef("function")), _names("/Identity"))),
        },
    ),
    "group": KindDef(
        "group",
        stream=False,
        keys={
            "/Type": TypeKey("/Group"),
            "/S": _names("/Transparency"),
            "/CS": COLOUR_SPACE,
            "/I": BOOLEAN,
            "/K": BOOLEAN,
        },
    ),
    "cie dict": KindDef(
        "cie dict",
        stream=False,
        keys={
            "/WhitePoint": NUMBER_OR_NUMBERS,
            "/BlackPoint": NUMBER_OR_NUMBERS,
            "/Gamma": NUMBER_OR_NUMBERS,
            "/Matrix": NUMBER_OR_NUMBERS,
            "/Range": NUMBER_OR_NUMBERS,
        },
    ),
    "icc profile": KindDef(
        "icc profile",
        stream=True,
        keys={"/N": INTEGER, "/Alternate": COLOUR_SPACE, "/Range": NUMBERS},
    ),
    "devicen attributes": KindDef(
        "devicen attributes",
        stream=False,
        keys={
            "/Subtype": _names("/DeviceN", "/NChannel"),
            "/Colorants": DictOf(COLOUR_SPACE),
            "/Process": KindRef("devicen process"),
        },
    ),
    "devicen process": KindDef(
        "devicen process",
        stream=False,
        keys={"/ColorSpace": COLOUR_SPACE, "/Components": NAMES},
    ),
    "function": Choice(
        "function",
        "/FunctionType",
        {
            0: "sampled function",
            2: "exponential function",
            3: "stitching function",
            4: "calculator function",
        },
    ),
    "sampled function": KindDef(
        "sampled function",
        stream=True,
        keys={
            **_FUNCTION_COMMON,
            "/Size": NUMBER_OR_NUMBERS,
            "/BitsPerSample": NUMBER_OR_NUMBERS,
            "/Order": NUMBER_OR_NUMBERS,
            "/Encode": NUMBER_OR_NUMBERS,
            "/Decode": NUMBER_OR_NUMBERS,
        },
    ),
    "exponential function": KindDef(
        "exponential function",
        stream=False,
        keys={
            **_FUNCTION_COMMON,
            "/C0": NUMBER_OR_NUMBERS,
            "/C1": NUMBER_OR_NUMBERS,
            "/N": NUMBER_OR_NUMBERS,
        },
    ),
    "stitching function": KindDef(
        "stitching function",
        stream=False,
        keys={
            **_FUNCTION_COMMON,
            "/Functions": ArrayOf(KindRef("function")),
            "/Bounds": NUMBER_OR_NUMBERS,
            "/Encode": NUMBER_OR_NUMBERS,
        },
    ),
    "calculator function": KindDef("calculator function", stream=True, keys=_FUNCTION_COMMON),
    "pattern": Choice("pattern", "/PatternType", {1: "tiling pattern", 2: "shading pattern"}),
    "tiling pattern": KindDef(
        "tiling pattern",
        stream=True,
        keys={
            "/Type": TypeKey("/Pattern"),
            "/PatternType": INTEGER,
            "/PaintType": INTEGER,
            "/TilingType": INTEGER,
            "/BBox": NUMBERS_4,
            "/XStep": NUMBER,
            "/YStep": NUMBER,
            "/Matrix": NUMBERS_6,
            "/Resources": KindRef("resources"),
        },
    ),
    "shading pattern": KindDef(
        "shading pattern",
        stream=False,
        keys={
            "/Type": TypeKey("/Pattern"),
            "/PatternType": INTEGER,
            "/Shading": KindRef("shading"),
            "/Matrix": NUMBERS_6,
            "/ExtGState": KindRef("graphics state"),
        },
    ),
    "shading": Choice(
        "shading",
        "/ShadingType",
        {
            1: "function shading",
            2: "axial shading",
            3: "axial shading",
            4: "free-form mesh shading",
            5: "lattice mesh shading",
            6: "free-form mesh shading",
            7: "free-form mesh shading",
        },
    ),
    "function shading": KindDef(
        "function shading",
        stream=False,
        keys={**_SHADING_COMMON, "/Domain": NUMBERS, "/Matrix": NUMBERS_6, "/Function": FUNCTION},
    ),
    "axial shading": KindDef(
        "axial shading",
        stream=False,
        keys={
            **_SHADING_COMMON,
            "/Coords": NUMBERS,
            "/Domain": NUMBERS,
            "/Function": FUNCTION,
            "/Extend": ArrayOf(BOOLEAN),
        },
    ),
    "free-form mesh shading": KindDef(
        "free-form mesh shading",
        stream=True,
        keys={
            **_SHADING_COMMON,
            "/BitsPerCoordinate": INTEGER,
            "/BitsPerComponent": INTEGER,
            "/BitsPerFlag": INTEGER,
            "/Decode": NUMBERS,
            "/Function": FUNCTION,
        },
    ),
    "lattice mesh shading": KindDef(
        "lattice mesh shading",
        stream=True,
        keys={
            **_SHADING_COMMON,
            "/BitsPerCoordinate": INTEGER,
            "/BitsPerComponent": INTEGER,
            "/VerticesPerRow": INTEGER,
            "/Decode": NUMBERS,
            "/Function": FUNCTION,
        },
    ),
    "xobject": Choice("xobject", "/Subtype", {"/Image": "image", "/Form": "form"}),
    "image": KindDef(
        "image",
        stream=True,
        keys={
            "/Type": TypeKey("/XObject"),
            "/Subtype": _names("/Image"),
            "/Width": INTEGER,
            "/Height": INTEGER,
            "/BitsPerComponent": INTEGER,
            "/SMaskInData": INTEGER,
            "/ColorSpace": COLOUR_SPACE,
            "/Intent": NAME,
            "/ImageMask": BOOLEAN,
            "/Interpolate": BOOLEAN,
            "/Mask": OneOf((KindRef("image"), ArrayOf(INTEGER))),
            "/Decode": NUMBERS,
            "/Matte": NUMBERS,
            "/SMask": KindRef("image"),
        },
    ),
    "form": KindDef(
        "form",
        stream=True,
        keys={
            "/Type": TypeKey("/XObject"),
            "/Subtype": _names("/Form"),
            "/FormType": Scalar("integer", integers=frozenset({1})),
            "/BBox": NUMBERS_4,
            "/Matrix": NUMBERS_6,
            "/Resources": KindRef("resources"),
            "/Group": KindRef("group"),
        },
    ),
    "font": Choice(
        "font",
        "/Subtype",
        {
            "/Type1": "simple font",
            "/MMType1": "simple font",
            "/TrueType": "simple font",
            "/Type3": "type 3 font",
            "/Type0": "type 0 font",
        },
    ),
    "simple font": KindDef(
        "simple font",
        stream=False,
        keys={
            "/Type": TypeKey("/Font"),
            "/Subtype": NAME,
            "/BaseFont": NAME,
            "/Encoding": OneOf((NAME, KindRef("encoding"))),
            **_SIMPLE_FONT_NUMBERS,
        },
    ),
    "type 3 font": KindDef(
        "type 3 font",
        stream=False,
        keys={
            "/Type": TypeKey("/Font"),
            "/Subtype": NAME,
            "/FontBBox": NUMBERS_4,
            "/FontMatrix": NUMBERS_6,
            "/CharProcs": DictOf(KindRef("glyph procedure")),
            "/Encoding": KindRef("encoding"),
            "/Resources": KindRef("resources"),
            **_SIMPLE_FONT_NUMBERS,
        },
    ),
    "type 0 font": KindDef(
        "type 0 font",
        stream=False,
        keys={
            "/Type": TypeKey("/Font"),
            "/Subtype": NAME,
            "/BaseFont": NAME,
            "/Encoding": OneOf((NAME, KindRef("cmap"))),
            "/DescendantFonts": ArrayOf(KindRef("cid font"), 1),
            "/ToUnicode": KindRef("cmap"),
        },
    ),
    "cid font": KindDef(
        "cid font",
        stream=False,
        keys={
            "/Type": TypeKey("/Font"),
            "/Subtype": _names("/CIDFontType0", "/CIDFontType2"),
            "/BaseFont": NAME,
            "/CIDSystemInfo": KindRef("cid system info"),
            "/FontDescriptor": KindRef("font descriptor"),
            "/DW": NUMBER,
            "/DW2": NUMBERS_2,
            "/W": _WIDTHS,
            "/W2": _WIDTHS,
            "/CIDToGIDMap": OneOf((_names("/Identity"), KindRef("plain stream"))),
        },
    ),
    "cid system info": KindDef(
        "cid system info",
        stream=False,
        keys={"/Registry": STRING, "/Ordering": STRING, "/Supplement": INTEGER},
    ),
    "cmap": KindDef(
        "cmap",
        stream=True,
        keys={
            "/Type": TypeKey("/CMap"),
            "/CMapName": NAME,
            "/CIDSystemInfo": KindRef("cid system info"),
            "/WMode": INTEGER,
            "/UseCMap": OneOf((NAME, KindRef("cmap"))),
        },
    ),
    "encoding": KindDef(
        "encoding",
        stream=False,
        keys={
            "/Type": TypeKey("/Encoding"),
            "/BaseEncoding": NAME,
            "/Differences": ArrayOf(OneOf((INTEGER, NAME))),
        },
    ),
    "font descriptor": KindDef(
        "font descriptor",
        stream=False,
        keys={
            "/Type": TypeKey("/FontDescriptor"),
            "/FontName": NAME,
            "/FontStretch": NAME,
            "/FontFamily": STRING,
            "/CharSet": STRING,
            **dict.fromkeys(_FONT_DESCRIPTOR_NUMBERS, NUMBER),
            "/FontBBox": NUMBERS_4,
            "/FontFile": KindRef("font file"),
            "/FontFile2": KindRef("font file"),
            "/FontFile3": KindRef("font file"),
        },
    ),
    "font file": KindDef(
        "font file",
        stream=True,
        keys={"/Length1": INTEGER, "/Length2": INTEGER, "/Length3": INTEGER, "/Subtype": NAME},
    ),
    "properties entry": Choice(
        "properties entry",
        "/Type",
        {"/OCG": "optional-content group", "/OCMD": "optional-content membership"},
        omit_unknown=True,
    ),
    "optional-content group": KindDef(
        "optional-content group",
        stream=False,
        keys={"/Type": TypeKey("/OCG"), "/Name": STRING, "/Intent": NAME_OR_NAMES},
    ),
    "optional-content membership": KindDef(
        "optional-content membership",
        stream=False,
        keys={
            "/Type": TypeKey("/OCMD"),
            "/OCGs": OneOf(
                (KindRef("optional-content group"), ArrayOf(KindRef("optional-content group")))
            ),
            "/P": NAME,
            "/VE": Named("visibility expression"),
        },
    ),
    **{
        f"decode parameters {filter_name}": KindDef(
            f"decode parameters {filter_name}",
            stream=False,
            keys=_INTEGER_OR_BOOLEAN_PARMS.get(filter_name, {}),
        )
        for filter_name in STREAM_FILTERS
    },
}
"""Every kind of section 14.8 item 1, by name."""

NAMED_SPECS: Final[dict[str, Spec]] = {
    "visibility expression": ArrayOf(
        OneOf((NAME, KindRef("optional-content group"), Named("visibility expression")))
    ),
}

PAGE_KEYS: Final[dict[str, Spec]] = {
    "/MediaBox": NUMBERS_4,
    "/CropBox": NUMBERS_4,
    "/Rotate": Scalar("integer", multiple_of=90),
    "/UserUnit": Scalar("number", positive=True),
    "/Resources": KindRef("resources"),
    "/Contents": OneOf((KindRef("page content stream"), ArrayOf(KindRef("page content stream")))),
    "/Group": KindRef("group"),
}
"""The page allowlist of section 14.2 item 4 beside `/Type` and `/Parent` (ISO 32000-1 Table 30)."""

PAGE_TREE_KEYS: Final = ("/Type", "/Parent")
STREAM_KEYS: Final = frozenset({"/Length", "/Filter", "/DecodeParms"})
TRAILER_KEYS: Final = frozenset({"/Size", "/Root", "/ID"})
CATALOGUE_KEYS: Final = frozenset({"/Type", "/Pages"})
PAGE_TREE_NODE_KEYS: Final = frozenset({"/Type", "/Kids", "/Count"})
_PARSED_KINDS: Final = frozenset({"form", "tiling pattern", "glyph procedure"})
_COLOUR_SPACE_NAMES: Final = frozenset({"/DeviceGray", "/DeviceRGB", "/DeviceCMYK", "/Pattern"})
_FAMILY: Final = NAME
COLOUR_SPACE_FAMILIES: Final[dict[str, Positions]] = {
    "/CalGray": Positions((_FAMILY, KindRef("cie dict"))),
    "/CalRGB": Positions((_FAMILY, KindRef("cie dict"))),
    "/Lab": Positions((_FAMILY, KindRef("cie dict"))),
    "/ICCBased": Positions((_FAMILY, KindRef("icc profile"))),
    "/Indexed": Positions(
        (_FAMILY, COLOUR_SPACE, INTEGER, OneOf((STRING, KindRef("plain stream"))))
    ),
    "/Pattern": Positions((_FAMILY, COLOUR_SPACE)),
    "/Separation": Positions((_FAMILY, NAME, COLOUR_SPACE, FUNCTION)),
    "/DeviceN": Positions(
        (_FAMILY, NAMES, COLOUR_SPACE, FUNCTION, KindRef("devicen attributes")), optional=1
    ),
}
_EOF: Final = b"%%EOF"
_LINE_ENDINGS: Final = (b"", b"\r\n", b"\n", b"\r")
_FINISHED_PAGE: Final = re.compile(r"[0-9]{1,4}\.pdf")


def _form(value: object) -> str:  # noqa: PLR0911 - one return per form
    """The form of a value as pikepdf hands it over: scalars arrive as Python values."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, Decimal | float):
        return "real"
    if isinstance(value, Stream):
        return "stream"
    if isinstance(value, Dictionary):
        return "dictionary"
    if isinstance(value, Array):
        return "array"
    if isinstance(value, Name):
        return "name"
    if isinstance(value, String):
        return "string"
    return "other"


def _resolve(spec: Spec) -> Spec:
    while isinstance(spec, Named):
        spec = NAMED_SPECS[spec.name]
    return spec


def _forms(spec: Spec) -> frozenset[str]:  # noqa: PLR0911 - one return per spec
    """The forms a spec accepts, which `OneOf` chooses by."""
    spec = _resolve(spec)
    if isinstance(spec, Scalar):
        if spec.form == "number":
            return frozenset({"integer", "real"})
        return frozenset({spec.form})
    if isinstance(spec, TypeKey):
        return frozenset({"name"})
    if isinstance(spec, ArrayOf | Positions):
        return frozenset({"array"})
    if isinstance(spec, DictOf):
        return frozenset({"dictionary"})
    if isinstance(spec, ColourSpaceSpec):
        return frozenset({"name", "array"})
    if isinstance(spec, OneOf):
        return frozenset().union(*(_forms(option) for option in spec.options))
    definition = KINDS[spec.name]
    if isinstance(definition, Choice):
        return frozenset({"dictionary", "stream"})
    return frozenset({"stream" if definition.stream else "dictionary"})


def _option(spec: OneOf, value: object) -> Spec:
    form = _form(value)
    for option in spec.options:
        if form in _forms(option):
            return option
    raise SchemaError("a value has another form than its position takes")


def _check_scalar(value: object, spec: Scalar) -> None:
    form = _form(value)
    accepted = _forms(spec)
    if form not in accepted:
        raise SchemaError("a value has another form than its position takes")
    if spec.names is not None and str(value) not in spec.names:
        raise SchemaError("a name outside its position's values")
    if spec.integers is not None and value not in spec.integers:
        raise SchemaError("an integer outside its position's values")
    if spec.multiple_of is not None and int(value) % spec.multiple_of != 0:  # type: ignore[call-overload]
        raise SchemaError("an integer that isn't a multiple of its step")
    if spec.positive and not Decimal(str(value)) > 0:
        raise SchemaError("a number that isn't positive")


def _scalar_copy(value: object, spec: Scalar) -> object:
    _check_scalar(value, spec)
    if isinstance(value, Name):
        return Name(str(value))
    if isinstance(value, String):
        return String(bytes(value))
    return value


def _discriminator(value: object) -> str | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Name):
        return str(value)
    return None


def _filters(stream: Stream) -> list[str]:
    """The stream's filter names, refusing a filter outside Table 6 or `/Crypt`."""
    value = stream.get("/Filter")
    if value is None:
        return []
    names = (
        [value] if isinstance(value, Name) else list(value) if isinstance(value, Array) else None
    )
    if names is None or not all(isinstance(name, Name) for name in names):
        raise SchemaError("a /Filter that isn't a name or an array of names")
    filters = [str(name) for name in names]
    if any(name not in STREAM_FILTERS for name in filters):
        raise SchemaError("a filter outside the standard filters")
    return filters


@dataclass
class _Budget:
    """The count of distinct containers against a cap."""

    cap: int
    count: int = 0

    def add(self) -> None:
        self.count += 1
        if self.count > self.cap:
            raise ContainerCapError("more containers than the cap")


def _concrete(value: object, name: str, *, omit: bool) -> KindDef | None:
    """The kind of `value` at a position of kind `name`: a `Choice` is decided by its required
    key. With `omit`, a choice that leaves out what no variant names returns `None`."""
    definition = KINDS[name]
    if isinstance(definition, KindDef):
        return definition
    leave_out = omit and definition.omit_unknown
    if not isinstance(value, Dictionary | Stream):
        if leave_out:
            return None
        raise SchemaError("a value that should be a dictionary or a stream")
    concrete = definition.variants.get(_discriminator(value.get(definition.key)))  # type: ignore[arg-type]
    if concrete is None:
        if leave_out:
            return None
        raise SchemaError("an object of a kind the schema doesn't define")
    resolved = KINDS[concrete]
    if not isinstance(resolved, KindDef):  # pragma: no cover - the table names kinds only
        raise SchemaError("an unresolved kind")
    return resolved


def _objgen(value: Object) -> tuple[int, int]:
    number, generation = value.objgen
    return (number, generation)


def _is_indirect(value: object) -> bool:
    return isinstance(value, Dictionary | Array | Stream | Name | String) and bool(
        value.is_indirect
    )


_OMIT: Final = object()


@dataclass
class _Copier:
    """Builds each value it sets in `out` from the schema and copies nothing else."""

    out: Pdf
    budget: _Budget
    memo: dict[tuple[int, int, object], object] = field(default_factory=dict)

    def _enter(self, depth: int) -> None:
        if depth > DEPTH_LIMIT:
            raise SchemaError("a path deeper than the depth limit")
        self.budget.add()

    def copy(self, value: object, spec: Spec, depth: int) -> object:  # noqa: PLR0911
        spec = _resolve(spec)
        if isinstance(spec, OneOf):
            return self.copy(value, _option(spec, value), depth)
        if isinstance(spec, Scalar):
            return _scalar_copy(value, spec)
        if isinstance(spec, TypeKey):
            # A `/Type` with another value is left out by the caller; here it is the kind's own.
            return Name(spec.value)
        if isinstance(spec, KindRef):
            return self._kind(value, spec.name, depth)
        if isinstance(spec, ColourSpaceSpec):
            return self._colour_space(value, depth)
        if isinstance(spec, ArrayOf | Positions):
            return self._array(value, spec, depth)
        if isinstance(spec, DictOf):
            return self._dict_of(value, spec, depth)
        raise SchemaError("a spec the schema doesn't define")  # pragma: no cover

    def _memo_key(self, value: object, label: object) -> tuple[int, int, object] | None:
        if isinstance(value, Dictionary | Array | Stream) and value.is_indirect:
            number, generation = _objgen(value)
            return (number, generation, label)
        return None

    def _colour_space(self, value: object, depth: int) -> object:
        if isinstance(value, Name):
            if str(value) not in _COLOUR_SPACE_NAMES:
                raise SchemaError("a colour space name outside the device spaces")
            return Name(str(value))
        if not isinstance(value, Array) or len(value) == 0 or not isinstance(value[0], Name):
            raise SchemaError("a colour space that isn't a name or a family array")
        shape = COLOUR_SPACE_FAMILIES.get(str(value[0]))
        if shape is None:
            raise SchemaError("a colour space family outside the schema")
        return self._array(value, shape, depth, label="colour space")

    def _array(
        self, value: object, spec: ArrayOf | Positions, depth: int, label: object = None
    ) -> object:
        if not isinstance(value, Array):
            raise SchemaError("a value that should be an array")
        key = self._memo_key(value, spec if label is None else label)
        if key is not None and key in self.memo:
            return self.memo[key]
        self._enter(depth)
        items = list(value)
        if isinstance(spec, ArrayOf):
            if spec.length is not None and len(items) != spec.length:
                raise SchemaError("an array of another length than its position takes")
            specs: Sequence[Spec] = [spec.item] * len(items)
        else:
            if not len(spec.items) - spec.optional <= len(items) <= len(spec.items):
                raise SchemaError("an array of another length than its position takes")
            specs = spec.items[: len(items)]
        new = Array()
        if key is not None:
            new = self.out.make_indirect(new)
            self.memo[key] = new
        for item, item_spec in zip(items, specs, strict=True):
            if item is None:
                raise SchemaError("a null where the position takes a value")
            new.append(self.copy(item, item_spec, depth + 1))
        return new

    def _dict_of(self, value: object, spec: DictOf, depth: int) -> object:
        if not isinstance(value, Dictionary) or isinstance(value, Stream):
            raise SchemaError("a value that should be a dictionary")
        key = self._memo_key(value, spec)
        if key is not None and key in self.memo:
            return self.memo[key]
        self._enter(depth)
        new = Dictionary()
        if key is not None:
            new = self.out.make_indirect(new)
            self.memo[key] = new
        for name in sorted(value.keys()):
            entry = value.get(name)
            if entry is None:
                continue
            copied = self.copy(entry, spec.item, depth + 1)
            if copied is not _OMIT:
                new[name] = copied
        return new

    def _kind(self, value: object, name: str, depth: int) -> object:
        definition = _concrete(value, name, omit=True)
        if definition is None:
            return _OMIT
        if definition.stream != isinstance(value, Stream) or not isinstance(
            value, Dictionary | Stream
        ):
            raise SchemaError("an object of another form than its kind")
        key = self._memo_key(value, definition.name)
        if key is not None and key in self.memo:
            return self.memo[key]
        self._enter(depth)
        new: Dictionary | Stream
        if isinstance(value, Stream):
            new = Stream(self.out, value.read_raw_bytes())
        else:
            new = Dictionary()
            if key is not None:
                new = self.out.make_indirect(new)
        if key is not None:
            self.memo[key] = new
        filters = _filters(value) if isinstance(value, Stream) else []
        for item in sorted(value.keys()):
            entry = value.get(item)
            if entry is None:
                continue
            copied = self._entry(definition, item, entry, filters, depth + 1)
            if copied is not _OMIT:
                new[item] = copied
        return new

    def _entry(
        self, definition: KindDef, item: str, entry: object, filters: list[str], depth: int
    ) -> object:
        """The copy of one key's value, or `_OMIT` for a key the kind doesn't list and for a
        `/Type` of another value."""
        if definition.stream and item in STREAM_KEYS:
            if item == "/Filter":
                return self._filter_value(entry, depth)
            if item == "/DecodeParms":
                return self._decode_parms(entry, filters, depth)
            return _OMIT
        spec = definition.keys.get(item)
        if spec is None:
            return _OMIT
        if isinstance(spec, TypeKey):
            matches = isinstance(entry, Name) and str(entry) == spec.value
            return Name(spec.value) if matches else _OMIT
        return self.copy(entry, spec, depth)

    def _filter_value(self, value: object, depth: int) -> object:
        if isinstance(value, Name):
            return Name(str(value))
        return self._array(value, NAMES, depth)

    def _decode_parms(self, value: object, filters: list[str], depth: int) -> object:
        if isinstance(value, Dictionary) and not isinstance(value, Stream):
            if len(filters) != 1:
                raise SchemaError("one /DecodeParms dictionary for several filters")
            return self._kind(value, f"decode parameters {filters[0]}", depth)
        if not isinstance(value, Array) or len(value) != len(filters):
            raise SchemaError("a /DecodeParms of another form than its filters")
        key = self._memo_key(value, "decode parameters")
        if key is not None and key in self.memo:
            return self.memo[key]
        self._enter(depth)
        new = Array()
        if key is not None:
            new = self.out.make_indirect(new)
            self.memo[key] = new
        entries: list[object] = list(value)
        for entry, filter_name in zip(entries, filters, strict=True):
            if entry is None:
                new.append(None)
            elif isinstance(entry, Dictionary) and not isinstance(entry, Stream):
                new.append(self._kind(entry, f"decode parameters {filter_name}", depth + 1))
            else:
                raise SchemaError("a /DecodeParms entry that isn't a dictionary or null")
        return new


def _page_object(page: pikepdf.Page | Dictionary) -> Dictionary:
    return page.obj if isinstance(page, pikepdf.Page) else page


def copy_page(
    source: pikepdf.Page | Dictionary,
    out: Pdf,
    page: pikepdf.Page | Dictionary,
    budget: _Budget | None = None,
) -> None:
    """Set on the blank `page` of `out` exactly the page keys of section 14.2 item 4, each value
    built anew in `out` by the schema. The memo lives for this one call, so pages of different
    files never share a copied font or image; `budget` counts containers across a whole file
    (`assemble`), a fresh page budget by default."""
    source_page = _page_object(source)
    target = _page_object(page)
    for key in list(target.keys()):
        if key not in PAGE_TREE_KEYS:
            del target[key]
    copier = _Copier(out, budget if budget is not None else _Budget(PAGE_CONTAINER_CAP))
    copier.budget.add()
    for key in sorted(source_page.keys()):
        spec = PAGE_KEYS.get(key)
        value = source_page.get(key)
        if spec is None or value is None:
            continue
        target[key] = copier.copy(value, spec, 1)


@dataclass
class _Checker:
    """Walks a file from its trailer with the schema, then reads every object with the nets."""

    pdf: Pdf
    budget: _Budget
    reached: set[tuple[int, int]] = field(default_factory=set)
    seen: set[tuple[int, int, object]] = field(default_factory=set)
    content: list[tuple[Object, int]] = field(default_factory=list)

    def _reach(self, value: object) -> None:
        if _is_indirect(value):
            self.reached.add(_objgen(value))  # type: ignore[arg-type]

    def _visit(self, value: object, label: object, depth: int) -> bool:
        """Count a container once per kind; `False` when it was already checked."""
        if depth > DEPTH_LIMIT:
            raise SchemaError("a path deeper than the depth limit")
        if isinstance(value, Dictionary | Array | Stream) and value.is_indirect:
            number, generation = _objgen(value)
            self.reached.add((number, generation))
            if (number, generation, label) in self.seen:
                return False
            self.seen.add((number, generation, label))
        self.budget.add()
        return True

    def check(self, value: object, spec: Spec, depth: int) -> None:
        spec = _resolve(spec)
        if isinstance(spec, OneOf):
            self.check(value, _option(spec, value), depth)
        elif isinstance(spec, Scalar):
            _check_scalar(value, spec)
            self._reach(value)
        elif isinstance(spec, TypeKey):
            self._reach(value)
            if not isinstance(value, Name) or str(value) != spec.value:
                raise SchemaError("a /Type with another value than its kind's")
        elif isinstance(spec, KindRef):
            self._kind(value, spec.name, depth)
        elif isinstance(spec, ColourSpaceSpec):
            self._colour_space(value, depth)
        elif isinstance(spec, ArrayOf | Positions):
            self._array(value, spec, depth, spec)
        elif isinstance(spec, DictOf):
            self._dict_of(value, spec, depth)

    def _colour_space(self, value: object, depth: int) -> None:
        if isinstance(value, Name):
            if str(value) not in _COLOUR_SPACE_NAMES:
                raise SchemaError("a colour space name outside the device spaces")
            return
        if not isinstance(value, Array) or len(value) == 0 or not isinstance(value[0], Name):
            raise SchemaError("a colour space that isn't a name or a family array")
        shape = COLOUR_SPACE_FAMILIES.get(str(value[0]))
        if shape is None:
            raise SchemaError("a colour space family outside the schema")
        self._array(value, shape, depth, "colour space")

    def _array(self, value: object, spec: ArrayOf | Positions, depth: int, label: object) -> None:
        if not isinstance(value, Array):
            raise SchemaError("a value that should be an array")
        if not self._visit(value, label, depth):
            return
        items = list(value)
        if isinstance(spec, ArrayOf):
            if spec.length is not None and len(items) != spec.length:
                raise SchemaError("an array of another length than its position takes")
            specs: Sequence[Spec] = [spec.item] * len(items)
        else:
            if not len(spec.items) - spec.optional <= len(items) <= len(spec.items):
                raise SchemaError("an array of another length than its position takes")
            specs = spec.items[: len(items)]
        for item, item_spec in zip(items, specs, strict=True):
            if item is None:
                raise SchemaError("a null where the position takes a value")
            self.check(item, item_spec, depth + 1)

    def _dict_of(self, value: object, spec: DictOf, depth: int) -> None:
        if not isinstance(value, Dictionary) or isinstance(value, Stream):
            raise SchemaError("a value that should be a dictionary")
        if not self._visit(value, spec, depth):
            return
        for name in sorted(value.keys()):
            entry = value.get(name)
            if entry is not None:
                self.check(entry, spec.item, depth + 1)

    def _kind(self, value: object, name: str, depth: int) -> None:
        definition = _concrete(value, name, omit=False)
        if definition is None:  # pragma: no cover - `omit=False` raises instead
            raise SchemaError("an object of a kind the schema doesn't define")
        if definition.stream != isinstance(value, Stream) or not isinstance(
            value, Dictionary | Stream
        ):
            raise SchemaError("an object of another form than its kind")
        if not self._visit(value, definition.name, depth):
            return
        filters = _filters(value) if isinstance(value, Stream) else []
        for item in sorted(value.keys()):
            self._entry(definition, item, value.get(item), filters, depth + 1)
        if definition.name in _PARSED_KINDS:
            self.content.append((value, depth))

    def _entry(
        self, definition: KindDef, item: str, entry: object, filters: list[str], depth: int
    ) -> None:
        """Check one key: it must be one its kind lists, with a value of the listed form."""
        if definition.stream and item in STREAM_KEYS:
            if item == "/Filter" and isinstance(entry, Array):
                self._array(entry, NAMES, depth, NAMES)
            elif item == "/DecodeParms" and entry is not None:
                self._decode_parms(entry, filters, depth)
            return
        spec = definition.keys.get(item)
        if spec is None:
            raise SchemaError("a key its kind doesn't list")
        if entry is not None:
            self.check(entry, spec, depth)

    def _decode_parms(self, value: object, filters: list[str], depth: int) -> None:
        if isinstance(value, Dictionary) and not isinstance(value, Stream):
            if len(filters) != 1:
                raise SchemaError("one /DecodeParms dictionary for several filters")
            self._kind(value, f"decode parameters {filters[0]}", depth)
            return
        if not isinstance(value, Array) or len(value) != len(filters):
            raise SchemaError("a /DecodeParms of another form than its filters")
        if not self._visit(value, "decode parameters", depth):
            return
        entries: list[object] = list(value)
        for entry, filter_name in zip(entries, filters, strict=True):
            if entry is None:
                continue
            if not isinstance(entry, Dictionary) or isinstance(entry, Stream):
                raise SchemaError("a /DecodeParms entry that isn't a dictionary or null")
            self._kind(entry, f"decode parameters {filter_name}", depth + 1)

    def page(self, page: Dictionary) -> None:
        """Check one page dictionary: its keys, then every object reached from it."""
        self.budget.add()
        for key in sorted(page.keys()):
            if key in PAGE_TREE_KEYS:
                continue
            spec = PAGE_KEYS.get(key)
            if spec is None:
                raise SchemaError("a page key outside the page allowlist")
            value = page.get(key)
            if value is not None:
                self.check(value, spec, 1)
        contents = page.get("/Contents")
        if contents is not None:
            self.content.append((page, 2 if isinstance(contents, Array) else 1))


def _exact_keys(value: Object, keys: frozenset[str]) -> None:
    if set(value.keys()) != keys:
        raise SchemaError("a structural dictionary with keys other than its own")


@dataclass
class _PageTree:
    """Invariant 2's page tree: exact keys, each `/Kids` element's `/Parent` naming the node that
    lists it, each `/Count` equal to the pages below, at most 16 nodes deep."""

    checker: _Checker
    pages: list[Dictionary] = field(default_factory=list)
    nodes: set[tuple[int, int]] = field(default_factory=set)

    def _mark(self, value: Dictionary) -> None:
        if _objgen(value) in self.nodes:
            raise SchemaError("a page tree object reached twice")
        self.nodes.add(_objgen(value))
        self.checker.reached.add(_objgen(value))

    def node(self, node: object, parent: Dictionary | None, level: int) -> int:
        if level > PAGE_TREE_DEPTH_LIMIT:
            raise SchemaError("a page tree deeper than its limit")
        if not isinstance(node, Dictionary) or isinstance(node, Stream) or not node.is_indirect:
            raise SchemaError("a page tree node that isn't an indirect dictionary")
        self._mark(node)
        _exact_keys(node, PAGE_TREE_NODE_KEYS | ({"/Parent"} if parent is not None else set()))
        if node.get("/Type") != Name.Pages:
            raise SchemaError("a page tree node whose /Type isn't /Pages")
        if parent is not None and not _same(node.get("/Parent"), parent):
            raise SchemaError("a page tree node whose /Parent isn't its node")
        kids = node.get("/Kids")
        if not isinstance(kids, Array):
            raise SchemaError("a /Kids that isn't an array")
        self.checker._reach(kids)
        count = sum(self.kid(kid, node, level) for kid in list(kids))
        declared: object = node.get("/Count")
        if isinstance(declared, bool) or declared != count:
            raise SchemaError("a /Count other than the pages below")
        return count

    def kid(self, kid: object, node: Dictionary, level: int) -> int:
        if not isinstance(kid, Dictionary) or isinstance(kid, Stream) or not kid.is_indirect:
            raise SchemaError("a /Kids element that isn't an indirect dictionary")
        kind = kid.get("/Type")
        if kind == Name.Pages:
            return self.node(kid, node, level + 1)
        if kind != Name.Page:
            raise SchemaError("a /Kids element that is neither a node nor a page")
        if not _same(kid.get("/Parent"), node):
            raise SchemaError("a page whose /Parent isn't the node that lists it")
        self._mark(kid)
        self.pages.append(kid)
        return 1


def _same(value: object, node: Dictionary) -> bool:
    return isinstance(value, Dictionary) and value.is_indirect and _objgen(value) == _objgen(node)


def _walk_inline(value: object, depth: int) -> None:
    """Invariant 5 inside a content stream: a dictionary or array written inline, and everything
    it holds, carries no name of the net and stays within the depth limit."""
    if depth > DEPTH_LIMIT:
        raise SchemaError("an inline dictionary nested past the depth limit")
    if isinstance(value, Dictionary):
        for key, item in value.items():
            if key in NET_NAMES:
                raise SchemaError("a name of the net in an inline dictionary")
            _walk_inline(item, depth + 1)
    elif isinstance(value, Array):
        for item in list(value):
            _walk_inline(item, depth + 1)
    elif isinstance(value, Name) and str(value) in NET_NAMES:
        raise SchemaError("a name of the net in an inline dictionary")


def _parse_content(pdf: Pdf, holder: Object, depth: int) -> None:
    """Parse one content stream (a page's `/Contents` as one stream through the page) for its
    marked-content property lists and inline images, refusing a parse that raises or warns."""
    warnings = len(pdf.get_warnings())
    try:
        instructions = pikepdf.parse_content_stream(holder, CONTENT_OPERATORS)
    except Exception as failure:
        raise SchemaError("a content stream that doesn't parse") from failure
    if len(pdf.get_warnings()) != warnings:
        raise SchemaError("a content stream that parses with a warning")
    for instruction in instructions:
        if isinstance(instruction, pikepdf.ContentStreamInlineImage):
            _walk_inline(instruction.iimage.obj, depth + 1)
        elif str(instruction.operator) in ("BDC", "DP"):
            for operand in instruction.operands:
                if isinstance(operand, Dictionary | Array):
                    _walk_inline(operand, depth + 1)


def _net_objects(pdf: Pdf) -> Iterator[object]:
    """Every object of the file and every direct value inside it, stream dictionaries included;
    an indirect value is read when its own turn in `pdf.objects` comes."""
    stack: list[object] = [pdf.trailer, *pdf.objects]
    while stack:
        value = stack.pop()
        yield value
        children: list[object] = []
        if isinstance(value, Dictionary | Stream):
            children = [item for _key, item in value.items()]
        elif isinstance(value, Array):
            children = list(value)
        stack.extend(child for child in children if not _is_indirect(child))


def _nets(pdf: Pdf) -> None:
    """Invariants 5 and 6 over every object of the file, independent of the schema."""
    for value in _net_objects(pdf):
        if isinstance(value, Dictionary | Stream):
            if any(key in NET_NAMES for key, _item in value.items()):
                raise SchemaError("a key of the net")
            action = value.get("/S")
            if isinstance(action, Name) and str(action) in ACTION_TYPES:
                raise SchemaError("a dictionary whose /S is an action type")
        elif isinstance(value, Name) and str(value) in NET_NAMES:
            raise SchemaError("a name of the net")


def check_file(pdf: Pdf, *, mode: Mode, page_count: int) -> None:
    """The invariants of section 15.6 item 14.2, numbers 1 to 6, on an open file; the byte cap of
    invariant 7 and the open checks are `check_opened`'s and the caller's."""
    trailer = pdf.trailer
    _exact_keys(trailer, TRAILER_KEYS)
    root = trailer.get("/Root")
    if not isinstance(root, Dictionary) or isinstance(root, Stream) or not root.is_indirect:
        raise SchemaError("a /Root that isn't an indirect dictionary")
    _exact_keys(root, CATALOGUE_KEYS)
    if root.get("/Type") != Name.Catalog:
        raise SchemaError("a catalogue whose /Type isn't /Catalog")
    cap = PAGE_CONTAINER_CAP if mode == "pages" else DOCUMENT_CONTAINER_CAP
    checker = _Checker(pdf, _Budget(cap))
    checker.reached.add(_objgen(root))
    tree = _PageTree(checker)
    tree.node(root.get("/Pages"), None, 1)
    pages = tree.pages
    expected = 1 if mode == "pages" else page_count
    if len(pages) != expected:
        raise SchemaError("a file with another page count")
    for page in pages:
        checker.page(page)
    for holder, depth in checker.content:
        _parse_content(pdf, holder, depth)
    for obj in pdf.objects:
        # Integers and nulls arrive as Python values and carry nothing a reader acts on.
        if _is_indirect(obj) and _objgen(obj) not in checker.reached:
            raise SchemaError("an indirect object no position reaches")
    _nets(pdf)


def check_layout(data: bytes) -> None:
    """At most one line ending after the file's last `%%EOF`."""
    end = data.rfind(_EOF)
    if end < 0 or data[end + len(_EOF) :] not in _LINE_ENDINGS:
        raise SchemaError("bytes after the last %%EOF")


def check_opened(pdf: Pdf, *, mode: Mode, page_count: int) -> None:
    """The checks of an opened file: no warning after the open, no incremental update (`/Prev`)
    and no hybrid reference (`/XRefStm`) in the trailer, the invariants, and no warning after the
    walk."""
    if pdf.get_warnings():
        raise SchemaError("a file that opens with a warning")
    if "/Prev" in pdf.trailer or "/XRefStm" in pdf.trailer:
        raise SchemaError("an incremental update or a hybrid reference")
    check_file(pdf, mode=mode, page_count=page_count)
    if pdf.get_warnings():
        raise SchemaError("a file that warns while it is read")


def _save(pdf: Pdf, path: Path) -> None:
    """qpdf writes only the objects reachable from the trailer, as one cross-reference table
    and no object stream."""
    pdf.save(
        path,
        linearize=False,
        object_stream_mode=pikepdf.ObjectStreamMode.disable,
        compress_streams=True,
    )


def _open_strict(source: Path | io.BytesIO) -> Pdf:
    return pikepdf.open(source, attempt_recovery=False, inherit_page_attributes=False)


def check_path(path: Path, *, mode: Mode, page_count: int, byte_cap: int) -> None:
    """Every check of one file on disk, as `render`'s self-check and `verify-render` run it."""
    size = path.stat().st_size
    if size > byte_cap:
        raise SchemaError("a file above its byte cap")
    check_layout(path.read_bytes())
    try:
        pdf = _open_strict(path)
    except Exception as failure:
        raise SchemaError("a file that doesn't open") from failure
    with pdf:
        check_opened(pdf, mode=mode, page_count=page_count)


def verify_file(
    source: Path, verified: Path, *, mode: Mode, page_count: int, byte_cap: int
) -> None:
    """`verify-render` for one file: check it, write it once more from the object graph that was
    checked, and check the written file the same way."""
    if source.stat().st_size > byte_cap:
        raise SchemaError("a file above its byte cap")
    check_layout(source.read_bytes())
    try:
        pdf = _open_strict(source)
    except Exception as failure:
        raise SchemaError("a file that doesn't open") from failure
    with pdf:
        check_opened(pdf, mode=mode, page_count=page_count)
        _save(pdf, verified)
    check_path(verified, mode=mode, page_count=page_count, byte_cap=byte_cap)


def _finished_pages(directory: Path) -> int:
    return sum(1 for path in directory.iterdir() if _FINISHED_PAGE.fullmatch(path.name))


def _new_page(out: Pdf) -> pikepdf.Page:
    page = out.add_blank_page()
    for key in list(page.obj.keys()):
        if key not in PAGE_TREE_KEYS:
            del page.obj[key]
    return page


def render_pages(scratch: Path, request: ChildRequest) -> RenderOk | RenderFailed:
    """The `render` command (section 14.2): one page file at a time, each built anew from the
    schema, self-checked, and renamed to `{n}.pdf` only when it passes; the child waits while
    `renderMaxPagesOnDisk` finished page files are in its directory."""
    limits = request.limits
    pdf = pikepdf.open(scratch / SOURCE, attempt_recovery=True, inherit_page_attributes=True)
    with pdf:
        pdf.flatten_annotations(mode="screen")
        total = 0
        number = 0
        for number, source_page in enumerate(pdf.pages, start=1):
            while _finished_pages(scratch) >= limits.render_max_pages_on_disk:
                time.sleep(FINISHED_PAGES_POLL_SECONDS)
            out = Pdf.new()
            try:
                copy_page(source_page, out, _new_page(out))
            except ContainerCapError:
                return RenderFailed(ok=False, code="render_too_large")
            except SchemaError:
                return RenderFailed(ok=False, code="render_failed")
            part = scratch / f"{page_name(number)}.part"
            _save(out, part)
            size = part.stat().st_size
            total += size
            if size > limits.render_max_page_bytes or total > limits.render_max_total_bytes:
                return RenderFailed(ok=False, code="render_too_large")
            try:
                check_path(part, mode="pages", page_count=1, byte_cap=limits.render_max_page_bytes)
            except SchemaError:
                return RenderFailed(ok=False, code="render_failed")
            part.replace(scratch / page_name(number))
    if number == 0:
        return RenderFailed(ok=False, code="render_failed")
    return RenderOk(ok=True, pageCount=number)


def _page_count(request: ChildRequest) -> int:
    value = request.parameters.get("pageCount")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SchemaError("a request without the version's page count")
    return value


def assemble_document(scratch: Path, request: ChildRequest) -> AssembleOk | RenditionFailed:
    """The `assemble` command (section 14.7 item 1): the verified pages joined into one file by
    the same copier, a new memo for each page file, containers counted across the whole file."""
    try:
        page_count = _page_count(request)
    except SchemaError:
        return RenditionFailed(ok=False, code="render_failed")
    out = Pdf.new()
    budget = _Budget(DOCUMENT_CONTAINER_CAP)
    try:
        for number in range(1, page_count + 1):
            data = (scratch / page_name(number)).read_bytes()
            with _open_strict(io.BytesIO(data)) as source:
                if len(source.pages) != 1:
                    return RenditionFailed(ok=False, code="render_failed")
                copy_page(source.pages[0], out, _new_page(out), budget)
    except ContainerCapError:
        return AssembleOk(ok=True, pageCount=page_count, documentPdf="too_large")
    except SchemaError:
        return RenditionFailed(ok=False, code="render_failed")
    _save(out, scratch / DOCUMENT)
    return AssembleOk(ok=True, pageCount=page_count, documentPdf="built")


def verify_render(scratch: Path, request: ChildRequest) -> Ok | RenditionFailed:
    """The `verify-render` command (section 15.6 item 14.2) in its request's mode."""
    limits = request.limits
    try:
        if request.mode == "pages" and request.pages is not None:
            for number in range(request.pages.first, request.pages.last + 1):
                verify_file(
                    scratch / page_name(number),
                    scratch / verified_name(number),
                    mode="pages",
                    page_count=1,
                    byte_cap=limits.render_max_page_bytes,
                )
        elif request.mode == "document":
            verify_file(
                scratch / DOCUMENT,
                scratch / DOCUMENT_VERIFIED,
                mode="document",
                page_count=_page_count(request),
                byte_cap=limits.render_max_document_bytes,
            )
        else:
            return RenditionFailed(ok=False, code="render_failed")
    except (SchemaError, OSError):
        return RenditionFailed(ok=False, code="render_failed")
    return Ok(ok=True)
