"""`docchat_ingestion.isolation.pdf_schema`: the rendition object schema, the copier, the checker
and the `render`, `assemble` and `verify-render` steps (`14-ingestion.md` sections 14.2, 14.7,
14.8 and 15.6 item 14; T-600, T-614, T-620, T-621 and T-622 run in-process here).

Source pages are built in the test with `pikepdf`, one construct each, as the rendition schema
fixtures of `84-testing.md` section 1.5 describe them; stub page files, the forms a compromised
`render` or `assemble` child could write, are built the same way and given to `verify_file`.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import re
import time
import warnings
import zlib
from collections.abc import Callable
from pathlib import Path

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name, Pdf, Stream, String

from docchat_ingestion.isolation import convert, pdf_schema, results
from docchat_ingestion.isolation.pdf_schema import (
    RENDITION_RULES_VERSION,
    SchemaError,
    check_layout,
    copy_page,
    verify_file,
)
from docchat_ingestion.isolation.results import (
    DOCX,
    PDF,
    PPTX,
    AssembleOk,
    ChildRequest,
    Limits,
    Ok,
    RenderFailed,
    RenderOk,
    RenditionFailed,
)

type Setup = Callable[[Pdf, pikepdf.Page], None]

RULES: dict[int, tuple[str, str, str]] = {
    1: ("3fc2cd537028bce00bef43a8c51da11d07733db39ccaec2cb63b2a99c7e36594", "10.17.0", "12.4.2"),
}
"""One entry per `RENDITION_RULES_VERSION`: (SHA-256 of pdf_schema.py, pikepdf, bundled qpdf)."""

PAGE_BYTES = 20_971_520


def font(pdf: Pdf, base: str = "Helvetica") -> Dictionary:
    return pdf.make_indirect(
        Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name("/" + base))
    )


def source_pdf(setup: Setup | None = None, *, pages: int = 1) -> bytes:
    pdf = Pdf.new()
    for number in range(1, pages + 1):
        page = pdf.add_blank_page(page_size=(200, 200))
        page.obj.Resources = Dictionary(Font=Dictionary(F1=font(pdf)))
        page.obj.Contents = Stream(pdf, f"BT /F1 12 Tf 10 100 Td (Page {number}) Tj ET".encode())
        if setup is not None:
            setup(pdf, page)
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def copied(data: bytes, budget: int | None = None) -> bytes:
    """The page file `render` writes for page 1 of `data`, without its self-check."""
    with pikepdf.open(io.BytesIO(data), attempt_recovery=True, inherit_page_attributes=True) as pdf:
        pdf.flatten_annotations(mode="screen")
        out = Pdf.new()
        page = out.add_blank_page()
        cap = pdf_schema._Budget(budget) if budget is not None else None
        copy_page(pdf.pages[0], out, page, cap)
        buffer = io.BytesIO()
        out.save(
            buffer,
            linearize=False,
            object_stream_mode=pikepdf.ObjectStreamMode.disable,
            compress_streams=True,
        )
    return buffer.getvalue()


def checked(
    tmp_path: Path, data: bytes, *, mode: pdf_schema.Mode = "pages", pages: int = 1
) -> None:
    path = tmp_path / "check.pdf"
    path.write_bytes(data)
    pdf_schema.check_path(path, mode=mode, page_count=pages, byte_cap=104_857_600)


def verified(
    tmp_path: Path, data: bytes, *, mode: pdf_schema.Mode = "pages", pages: int = 1
) -> bytes:
    source = tmp_path / "1.pdf"
    source.write_bytes(data)
    verify_file(
        source, tmp_path / "1.verified.pdf", mode=mode, page_count=pages, byte_cap=104_857_600
    )
    return (tmp_path / "1.verified.pdf").read_bytes()


def refused(
    tmp_path: Path, data: bytes, *, mode: pdf_schema.Mode = "pages", pages: int = 1
) -> None:
    with pytest.raises(SchemaError):
        verified(tmp_path, data, mode=mode, pages=pages)


def objects_of(data: bytes) -> bytes:
    with pikepdf.open(io.BytesIO(data)) as pdf:
        return b"\n".join(repr(obj).encode() for obj in pdf.objects)


def limits() -> Limits:
    return Limits(max_pages=2000, render_max_total_bytes=524_288_000)


def child_request(command: str, **fields: object) -> ChildRequest:
    value: dict[str, object] = {
        "command": command,
        "contentType": PDF,
        "sourcePath": None,
        "pages": None,
        "mode": None,
        "parameters": {},
        "limits": limits(),
    }
    value.update(fields)
    return ChildRequest.model_validate(value)


# --- The rules version (T-621) -----------------------------------------------------------------


def test_t621_the_rules_version_names_the_module_and_the_libraries() -> None:
    module = Path(pdf_schema.__file__).read_bytes()
    qpdf = getattr(pikepdf, "__libqpdf_version__", None)

    assert isinstance(qpdf, str)
    assert max(RULES) == RENDITION_RULES_VERSION
    assert RULES[RENDITION_RULES_VERSION] == (
        hashlib.sha256(module).hexdigest(),
        importlib.metadata.version("pikepdf"),
        qpdf,
    )


def test_t621_no_other_module_names_a_pdf_key_of_the_schema() -> None:
    keys = set(pdf_schema.PAGE_KEYS) | pdf_schema.NET_NAMES | pdf_schema.ACTION_TYPES
    for kind in pdf_schema.KINDS.values():
        if isinstance(kind, pdf_schema.KindDef):
            keys |= set(kind.keys)
    package = Path(pdf_schema.__file__).parents[1]
    for path in package.rglob("*.py"):
        if path.name == "pdf_schema.py":
            continue
        text = path.read_text(encoding="utf-8")
        named = {key for key in keys if f'"{key}"' in text}
        assert named == set(), f"{path.name} names {sorted(named)}"


# --- Building each page (section 14.2) --------------------------------------------------------


def test_t600_the_copy_keeps_the_page_allowlist_and_nothing_else(tmp_path: Path) -> None:
    def setup(pdf: Pdf, page: pikepdf.Page) -> None:
        page.obj.CropBox = [0, 0, 150, 150]
        page.obj.Rotate = 90
        page.obj.UserUnit = 2
        page.obj.Group = Dictionary(Type=Name.Group, S=Name.Transparency, CS=Name.DeviceRGB)
        for key in ("/BleedBox", "/TrimBox", "/ArtBox"):
            page.obj[key] = [0, 0, 100, 100]
        page.obj.Tabs = Name.S
        page.obj.StructParents = 3
        page.obj.Dur = 5
        page.obj.Thumb = Stream(pdf, b"thumb")
        page.obj.Trans = Dictionary(S=Name.Wipe)
        page.obj.Metadata = Stream(pdf, b"<x/>")
        page.obj.PieceInfo = Dictionary()
        page.obj.B = Array([])
        pdf.Root.OpenAction = Dictionary(S=Name.JavaScript, JS=String("app.alert(1)"))
        pdf.Root.PageMode = Name.UseOutlines

    data = copied(source_pdf(setup))

    with pikepdf.open(io.BytesIO(data)) as out:
        assert set(out.trailer.keys()) == {"/Size", "/Root", "/ID"}
        assert set(out.Root.keys()) == {"/Type", "/Pages"}
        assert set(out.Root.Pages.keys()) == {"/Type", "/Kids", "/Count"}
        page = out.pages[0].obj
        assert set(page.keys()) == {
            "/Type",
            "/Parent",
            "/MediaBox",
            "/CropBox",
            "/Rotate",
            "/UserUnit",
            "/Resources",
            "/Contents",
            "/Group",
        }
        assert page.Rotate == 90
    checked(tmp_path, data)


@pytest.mark.parametrize(
    ("name", "setup"),
    [
        (
            "schema-presteps-gotor",
            lambda pdf, page: page.obj.__setitem__(
                "/PresSteps", Dictionary(NA=Dictionary(S=Name.GoToR, F=String("other.pdf")))
            ),
        ),
        (
            "schema-gotoe-unlisted",
            lambda pdf, page: page.obj.__setitem__("/X1", Dictionary(S=Name.GoToE, F=String("x"))),
        ),
        (
            "schema-page-af",
            lambda pdf, page: page.obj.__setitem__(
                "/AF", Array([Dictionary(F=String("a.bin"), EF=Dictionary(F=Stream(pdf, b"x")))])
            ),
        ),
        (
            "schema-resources-unknown",
            lambda pdf, page: page.obj.Resources.__setitem__(
                "/X1", Dictionary(S=Name.Launch, F=String("calc.exe"))
            ),
        ),
        (
            "schema-font-unlisted",
            lambda pdf, page: page.obj.Resources.Font.F1.__setitem__(
                "/X1", Dictionary(S=Name.URI, URI=String("https://example.net"))
            ),
        ),
    ],
)
def test_t600_constructs_the_schema_leaves_out_never_reach_a_page(
    tmp_path: Path, name: str, setup: Setup
) -> None:
    data = copied(source_pdf(setup))

    for net in (b"/GoToR", b"/GoToE", b"/AF", b"/EF", b"/Launch", b"/URI", b"/PresSteps", b"/X1"):
        assert net not in objects_of(data), name
    checked(tmp_path, data)


def image(pdf: Pdf, **extra: object) -> Stream:
    stream = Stream(pdf, zlib.compress(b"\xff\x00\x00" * 4))
    stream.Type = Name.XObject
    stream.Subtype = Name.Image
    stream.Width = 2
    stream.Height = 2
    stream.BitsPerComponent = 8
    stream.ColorSpace = Name.DeviceRGB
    stream.Filter = Name.FlateDecode
    for key, value in extra.items():
        stream["/" + key] = value
    return stream


def test_t600_xobject_keys_outside_the_schema_are_left_out(tmp_path: Path) -> None:
    def setup(pdf: Pdf, page: pikepdf.Page) -> None:
        form = Stream(pdf, b"0 0 m 10 10 l S")
        form.Type = Name.XObject
        form.Subtype = Name.Form
        form.BBox = [0, 0, 10, 10]
        form.Ref = Dictionary(F=String("other.pdf"), Page=0)
        form.Metadata = Stream(pdf, b"<x/>")
        page.obj.Resources.XObject = Dictionary(
            Fm1=form,
            Im1=image(
                pdf,
                Alternates=Array([]),
                OPI=Dictionary(),
                F=String("x.bin"),
                FFilter=Name.FlateDecode,
                FDecodeParms=Dictionary(),
                DL=4,
            ),
        )
        page.obj.Contents = Stream(pdf, b"q /Fm1 Do Q q 20 0 0 20 0 0 cm /Im1 Do Q")

    data = copied(source_pdf(setup))

    text = objects_of(data)
    for key in (
        b"/Ref",
        b"/OPI",
        b"/Alternates",
        b"/FFilter",
        b"/FDecodeParms",
        b"/DL",
        b"/Metadata",
    ):
        assert key not in text
    assert b"/F " not in text.replace(b"/Filter", b"")
    checked(tmp_path, data)


def test_t600_annotations_are_flattened_and_never_kept(tmp_path: Path) -> None:
    def setup(pdf: Pdf, page: pikepdf.Page) -> None:
        appearance = Stream(pdf, b"1 0 0 rg 0 0 50 50 re f")
        appearance.Type = Name.XObject
        appearance.Subtype = Name.Form
        appearance.BBox = [0, 0, 50, 50]
        square = Dictionary(
            Type=Name.Annot,
            Subtype=Name.Square,
            Rect=[10, 10, 60, 60],
            AP=Dictionary(N=appearance),
        )
        link = Dictionary(
            Type=Name.Annot,
            Subtype=Name.Link,
            Rect=[0, 0, 10, 10],
            Dest=Array([page.obj, Name.Fit]),
        )
        page.obj.Annots = pdf.make_indirect(
            Array([pdf.make_indirect(square), pdf.make_indirect(link)])
        )

    data = copied(source_pdf(setup))

    with pikepdf.open(io.BytesIO(data)) as out:
        page = out.pages[0].obj
        assert "/Annots" not in page
        # The square's appearance is drawn by the page now, as a form of its resources.
        assert len(page.Resources.XObject.keys()) == 1
    checked(tmp_path, data)


@pytest.mark.parametrize(
    "setup",
    [
        lambda pdf, page: page.obj.Resources.__setitem__("/Font", Array([])),
        lambda pdf, page: page.obj.Resources.Font.__setitem__("/F1", Name.Helvetica),
        lambda pdf, page: page.obj.Resources.__setitem__(
            "/XObject", Dictionary(P1=image(pdf, Subtype=Name.PS))
        ),
        lambda pdf, page: page.obj.Resources.__setitem__(
            "/XObject", Dictionary(I1=image(pdf, Width=Name.Two))
        ),
        lambda pdf, page: page.obj.Resources.Font.F1.__setitem__("/Subtype", Name.OpenType),
        lambda pdf, page: page.obj.Resources.__setitem__(
            "/XObject", Dictionary(I1=image(pdf, Filter=Name.BrotliDecode))
        ),
        lambda pdf, page: page.obj.__setitem__("/Rotate", 45),
        lambda pdf, page: page.obj.__setitem__("/UserUnit", -1),
        lambda pdf, page: page.obj.__setitem__(
            "/Group", Dictionary(Type=Name.Group, S=Name.Knockout)
        ),
        lambda pdf, page: page.obj.Resources.__setitem__(
            "/ColorSpace", Dictionary(C1=Array([Name.Unknown, Dictionary()]))
        ),
        lambda pdf, page: page.obj.Resources.__setitem__("/ColorSpace", Dictionary(C1=Name.CalRGB)),
        lambda pdf, page: page.obj.Resources.__setitem__(
            "/Shading", Dictionary(S1=Dictionary(ShadingType=9, ColorSpace=Name.DeviceRGB))
        ),
        lambda pdf, page: page.obj.Resources.__setitem__(
            "/ExtGState", Dictionary(G1=Dictionary(SMask=Name.Alpha))
        ),
    ],
)
def test_t600_a_listed_key_of_the_wrong_form_or_an_undefined_kind_fails(setup: Setup) -> None:
    with pytest.raises(SchemaError):
        copied(source_pdf(setup))


def nested_forms(levels: int) -> Setup:
    """Form XObjects nested `levels` deep: each level adds 3 containers below the page."""

    def setup(pdf: Pdf, page: pikepdf.Page) -> None:
        inner: Stream | None = None
        for _level in range(levels):
            form = Stream(pdf, b"/Fm Do" if inner is not None else b"0 0 m 1 1 l S")
            form.Type = Name.XObject
            form.Subtype = Name.Form
            form.BBox = [0, 0, 1, 1]
            if inner is not None:
                form.Resources = Dictionary(XObject=Dictionary(Fm=inner))
            inner = form
        page.obj.Resources.XObject = Dictionary(Fm=inner)

    return setup


def test_t622_a_path_past_the_depth_limit_fails_and_one_at_it_passes(tmp_path: Path) -> None:
    # resources (1), XObject (2), form (3), then 3 a level: 21 forms reach 63, 22 reach 66.
    checked(tmp_path, copied(source_pdf(nested_forms(21))))
    with pytest.raises(SchemaError):
        copied(source_pdf(nested_forms(22)))


def test_t622_a_page_above_the_container_cap_is_too_large() -> None:
    with pytest.raises(pdf_schema.ContainerCapError):
        copied(source_pdf(), budget=3)


def test_a_reference_cycle_ends_at_the_memo(tmp_path: Path) -> None:
    def setup(pdf: Pdf, page: pikepdf.Page) -> None:
        form = Stream(pdf, b"0 0 m 1 1 l S")
        form.Type = Name.XObject
        form.Subtype = Name.Form
        form.BBox = [0, 0, 1, 1]
        form.Resources = Dictionary(XObject=Dictionary(Self=form))
        page.obj.Resources.XObject = Dictionary(Fm=form, Again=form)

    data = copied(source_pdf(setup))

    with pikepdf.open(io.BytesIO(data)) as out:
        xobjects = out.pages[0].obj.Resources.XObject
        assert (
            xobjects.Fm.objgen == xobjects.Again.objgen == xobjects.Fm.Resources.XObject.Self.objgen
        )
    checked(tmp_path, data)


def test_every_kind_of_the_schema_copies_and_checks(tmp_path: Path) -> None:  # noqa: PLR0915
    def setup(pdf: Pdf, page: pikepdf.Page) -> None:  # noqa: PLR0915 - one object of each kind
        exponential = Dictionary(FunctionType=2, Domain=[0, 1], C0=[0], C1=[1], N=1)
        sampled = Stream(pdf, b"\x00\xff")
        sampled.FunctionType = 0
        sampled.Domain = [0, 1]
        sampled.Range = [0, 1]
        sampled.Size = [2]
        sampled.BitsPerSample = 8
        calculator = Stream(pdf, b"{ }")
        calculator.FunctionType = 4
        calculator.Domain = [0, 1]
        calculator.Range = [0, 1]
        stitching = Dictionary(
            FunctionType=3,
            Domain=[0, 1],
            Functions=[exponential, sampled],
            Bounds=[0.5],
            Encode=[0, 1, 0, 1],
        )
        icc = Stream(pdf, b"icc")
        icc.N = 3
        icc.Alternate = Name.DeviceRGB
        lookup = Stream(pdf, b"\x00\x00\x00\xff\xff\xff")
        attributes = Dictionary(
            Subtype=Name.NChannel,
            Colorants=Dictionary(
                Spot=Array([Name.Separation, Name.Spot, Name.DeviceCMYK, exponential])
            ),
            Process=Dictionary(ColorSpace=Name.DeviceCMYK, Components=[Name.Cyan]),
        )
        axial = Dictionary(
            ShadingType=2,
            ColorSpace=Name.DeviceRGB,
            Coords=[0, 0, 1, 1],
            Function=stitching,
            Extend=[True, False],
            AntiAlias=False,
        )
        mesh = Stream(pdf, b"\x00" * 16)
        mesh.ShadingType = 4
        mesh.ColorSpace = Name.DeviceGray
        mesh.BitsPerCoordinate = 8
        mesh.BitsPerComponent = 8
        mesh.BitsPerFlag = 8
        mesh.Decode = [0, 1, 0, 1, 0, 1]
        mesh.Filter = Array([Name.FlateDecode])
        mesh.DecodeParms = Array([Dictionary(Predictor=12, Columns=2, Unknown=1)])
        mesh.write(zlib.compress(b"\x00" * 16), filter=Name.FlateDecode)
        tiling = Stream(pdf, b"/Span << /MCID 0 >> BDC 0 0 m 1 1 l S EMC")
        tiling.Type = Name.Pattern
        tiling.PatternType = 1
        tiling.PaintType = 1
        tiling.TilingType = 1
        tiling.BBox = [0, 0, 1, 1]
        tiling.XStep = 1
        tiling.YStep = 1
        tiling.Resources = Dictionary(ProcSet=[Name.PDF])
        soft = Stream(pdf, b"0 0 m 1 1 l S")
        soft.Type = Name.XObject
        soft.Subtype = Name.Form
        soft.FormType = 1
        soft.BBox = [0, 0, 1, 1]
        soft.Group = Dictionary(S=Name.Transparency, I=True, K=False)
        glyph = Stream(pdf, b"0 0 d0 0 0 m 1 1 l S")
        to_unicode = Stream(pdf, b"cmap")
        to_unicode.Type = Name.CMap
        to_unicode.CMapName = Name.Test
        to_unicode.CIDSystemInfo = Dictionary(
            Registry=String("Adobe"), Ordering=String("Identity"), Supplement=0
        )
        font_file = Stream(pdf, b"font")
        font_file.Length1 = 4
        font_file.Metadata = Stream(pdf, b"<x/>")
        descriptor = Dictionary(
            Type=Name.FontDescriptor,
            FontName=Name.Test,
            Flags=32,
            FontBBox=[0, 0, 1, 1],
            ItalicAngle=0,
            Ascent=1,
            Descent=0,
            CapHeight=1,
            StemV=1,
            FontFile2=font_file,
            CIDSet=Stream(pdf, b"x"),
        )
        gid_map = Stream(pdf, b"\x00\x01")
        cid_font = Dictionary(
            Type=Name.Font,
            Subtype=Name.CIDFontType2,
            BaseFont=Name.Test,
            CIDSystemInfo=Dictionary(
                Registry=String("Adobe"), Ordering=String("Identity"), Supplement=0
            ),
            FontDescriptor=descriptor,
            DW=1000,
            W=Array([1, Array([500, 600])]),
            CIDToGIDMap=gid_map,
        )
        resources = page.obj.Resources
        resources.Font.F2 = Dictionary(
            Type=Name.Font,
            Subtype=Name.Type0,
            BaseFont=Name.Test,
            Encoding=Name("/Identity-H"),
            DescendantFonts=[cid_font],
            ToUnicode=to_unicode,
        )
        resources.Font.F3 = Dictionary(
            Type=Name.Font,
            Subtype=Name.Type3,
            FontBBox=[0, 0, 1, 1],
            FontMatrix=[1, 0, 0, 1, 0, 0],
            CharProcs=Dictionary(a=glyph),
            FirstChar=97,
            LastChar=97,
            Widths=[1],
            Encoding=Dictionary(Type=Name.Encoding, Differences=[97, Name.a]),
            Resources=Dictionary(),
        )
        resources.ColorSpace = Dictionary(
            C1=Array([Name.ICCBased, icc]),
            C2=Array([Name.Indexed, Name.DeviceRGB, 1, lookup]),
            C3=Array([Name.Indexed, Name.DeviceRGB, 1, String(b"\x00\x00\x00\xff\xff\xff")]),
            C4=Array([Name.DeviceN, [Name.Spot], Name.DeviceCMYK, calculator, attributes]),
            C5=Array(
                [Name.Lab, Dictionary(WhitePoint=[0.95, 1, 1.09], Range=[-100, 100, -100, 100])]
            ),
            C6=Array([Name.Pattern, Name.DeviceRGB]),
        )
        resources.Pattern = Dictionary(
            P1=tiling,
            P2=Dictionary(
                Type=Name.Pattern, PatternType=2, Shading=axial, ExtGState=Dictionary(CA=0.5)
            ),
        )
        resources.Shading = Dictionary(S1=axial, S2=mesh)
        resources.ExtGState = Dictionary(
            G1=Dictionary(
                Type=Name.ExtGState,
                LW=1,
                D=Array([Array([3, 2]), 0]),
                BM=[Name.Multiply, Name.Normal],
                SMask=Dictionary(Type=Name.Mask, S=Name.Luminosity, G=soft, TR=Name.Identity),
                Font=Array([resources.Font.F1, 12]),
                TR2=Name.Default,
                HT=Name.Default,
            ),
            G2=Dictionary(SMask=Name("/None")),
        )
        resources.Properties = Dictionary(
            oc1=Dictionary(Type=Name.OCG, Name=String("Layer"), Usage=Dictionary()),
            oc2=Dictionary(
                Type=Name.OCMD,
                OCGs=[Dictionary(Type=Name.OCG, Name=String("L2"))],
                P=Name.AnyOn,
                VE=Array([Name.Not, Array([Name.And])]),
            ),
            mc=Dictionary(MCID=1),
            oddity=Name.Other,
        )

    data = copied(source_pdf(setup))

    text = objects_of(data)
    for left_out in (
        b"/TR2",
        b"/HT",
        b"/Usage",
        b"/CIDSet",
        b"/Unknown",
        b"/Metadata",
        b"/oddity",
        b"/mc",
    ):
        assert left_out not in text
    checked(tmp_path, data)


@pytest.mark.parametrize(
    "mesh_setup",
    [
        lambda stream: stream.__setitem__("/DecodeParms", Dictionary(Predictor=12)),
        lambda stream: stream.__setitem__(
            "/DecodeParms", Array([Dictionary(), Dictionary(), Dictionary()])
        ),
        lambda stream: stream.__setitem__("/DecodeParms", Array([Name.Bad])),
        lambda stream: stream.__setitem__("/DecodeParms", Name.Bad),
        lambda stream: stream.__setitem__("/Filter", Array([Name.FlateDecode, String("x")])),
    ],
)
def test_decode_parameters_of_another_form_than_the_filters_fail(
    mesh_setup: Callable[[Stream], None],
) -> None:
    def setup(pdf: Pdf, page: pikepdf.Page) -> None:
        stream = image(pdf)
        stream.Filter = Array([Name.FlateDecode, Name.ASCIIHexDecode])
        mesh_setup(stream)
        page.obj.Resources.XObject = Dictionary(I1=stream)

    with pytest.raises(SchemaError):
        copied(source_pdf(setup))


# --- verify-render: stub pages (section 15.6 item 14.2) ----------------------------------------


def stub(change: Callable[[Pdf], None] | None = None, *, pages: int = 1) -> bytes:
    pdf = Pdf.open(io.BytesIO(copied(source_pdf())))
    for _extra in range(pages - 1):
        pdf.pages.append(pdf.pages[0])
    if change is not None:
        change(pdf)
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        # pikepdf warns about a widget no /AcroForm reaches, which is what this stub forges.
        warnings.simplefilter("ignore")
        pdf.save(buffer, linearize=False, object_stream_mode=pikepdf.ObjectStreamMode.disable)
    return buffer.getvalue()


def with_object(data: bytes, body: bytes) -> bytes:
    """`data` with one more indirect object that the cross-reference table names and no other
    object references; qpdf drops unreferenced objects when it writes, so the stub is edited."""
    number = int(re.findall(rb"/Size (\d+)", data)[-1])
    xref_at = data.rindex(b"\nxref\n") + 1
    obj = b"%d 0 obj\n" % number + body + b"\nendobj\n"
    head = data[:xref_at] + obj
    table = data[xref_at:].replace(b"xref\n0 %d\n" % number, b"xref\n0 %d\n" % (number + 1), 1)
    trailer_at = table.index(b"trailer")
    table = table[:trailer_at] + b"%010d 00000 n \n" % xref_at + table[trailer_at:]
    table = table.replace(b"/Size %d" % number, b"/Size %d" % (number + 1), 1)
    table = re.sub(rb"startxref\n\d+", b"startxref\n%d" % len(head), table)
    return head + table


def test_t600_a_render_page_passes_and_the_verified_file_is_written_anew(tmp_path: Path) -> None:
    data = copied(source_pdf())

    check_layout(data)
    assert verified(tmp_path, data)[:5] == b"%PDF-"


def javascript(pdf: Pdf) -> Dictionary:
    return Dictionary(S=Name.JavaScript, JS=String("app.alert(1)"))


@pytest.mark.parametrize(
    "change",
    [
        lambda pdf: pdf.Root.__setitem__("/OpenAction", javascript(pdf)),
        lambda pdf: pdf.Root.__setitem__("/Names", Dictionary()),
        lambda pdf: pdf.Root.__setitem__(
            "/Outlines", Dictionary(First=Dictionary(A=Dictionary(S=Name.URI, URI=String("x"))))
        ),
        lambda pdf: pdf.Root.__setitem__("/PageMode", Name.UseNone),
        lambda pdf: pdf.pages[0].obj.__setitem__(
            "/Annots", Array([Dictionary(Subtype=Name.Widget, Rect=[0, 0, 1, 1])])
        ),
        lambda pdf: pdf.pages[0].obj.__setitem__(
            "/Annots",
            Array(
                [Dictionary(Subtype=Name.Link, A=Dictionary(S=Name.URI, URI=String("https://x")))]
            ),
        ),
        lambda pdf: pdf.pages[0].obj.Contents.__setitem__("/F", String("x.bin")),
        lambda pdf: pdf.pages[0].obj.Contents.__setitem__("/FFilter", Name.FlateDecode),
        lambda pdf: pdf.pages[0].obj.Contents.__setitem__("/FDecodeParms", Dictionary()),
        lambda pdf: pdf.Root.Pages.__setitem__("/Resources", Dictionary()),
        lambda pdf: pdf.pages[0].obj.Resources.Font.F1.__setitem__("/X1", Dictionary(S=Name.Hide)),
        lambda pdf: pdf.pages[0].obj.Resources.__setitem__(
            "/XObject", Dictionary(I1=image(pdf, OPI=Dictionary(Version=1.3)))
        ),
        lambda pdf: pdf.pages[0].obj.Resources.__setitem__(
            "/XObject", Dictionary(I1=image(pdf, URL=String("https://example.net")))
        ),
        lambda pdf: pdf.pages[0].obj.Resources.__setitem__(
            "/Properties", Dictionary(p=Dictionary(Type=Name.Other))
        ),
        lambda pdf: pdf.docinfo.__setitem__("/Title", "x"),
    ],
)
def test_t600_a_stub_page_with_a_forbidden_construct_is_refused(
    tmp_path: Path, change: Callable[[Pdf], None]
) -> None:
    refused(tmp_path, stub(change))


@pytest.mark.parametrize(
    "body",
    [
        b"<< /URI (https://example.net) >>",
        b"<< /Type /Action /S /SubmitForm >>",
        b"<< /FS /URL /F (https://example.net/x.pdf) >>",
        b"<< /Unreached 1 >>",
    ],
)
def test_t600_an_object_no_position_reaches_is_refused(tmp_path: Path, body: bytes) -> None:
    refused(tmp_path, with_object(stub(), body))


def test_the_object_helper_writes_a_readable_file() -> None:
    with pikepdf.open(io.BytesIO(with_object(stub(), b"<< /Unreached 1 >>"))) as pdf:
        assert pdf.get_warnings() == []
        assert any("/Unreached" in repr(obj) for obj in pdf.objects)


def test_t600_a_stub_with_two_pages_is_refused_in_page_mode(tmp_path: Path) -> None:
    refused(tmp_path, stub(pages=2))


def test_t600_an_incremental_update_a_hybrid_reference_and_trailing_bytes_are_refused(
    tmp_path: Path,
) -> None:
    data = stub()
    startxref = int(re.findall(rb"startxref\s+(\d+)", data)[-1])
    update = b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    xref_at = len(data) + len(update)
    incremental = (
        data
        + update
        + b"xref\n0 2\n0000000000 65535 f \n%010d 00000 n \n" % len(data)
        + b"trailer\n<< /Size 5 /Root 1 0 R /Prev %d /ID [<00><00>] >>\n" % startxref
        + b"startxref\n%d\n%%%%EOF\n" % xref_at
    )
    hybrid = data.replace(b"/Size", b"/XRefStm 9 /Size", 1)

    refused(tmp_path, incremental)
    refused(tmp_path, hybrid)
    refused(tmp_path, data + b"\n\n")
    refused(tmp_path, data.rstrip(b"\n") + b"\nX")
    refused(tmp_path, data.replace(b"xref", b"xrxf"))


def test_t600_an_object_no_cross_reference_names_is_dropped_by_the_verified_file(
    tmp_path: Path,
) -> None:
    data = stub()
    xref_at = data.rindex(b"\nxref\n") + 1
    hidden = b"99 0 obj\n<< /S /URI /URI (https://example.net) >>\nendobj\n"
    crafted = data[:xref_at] + hidden + data[xref_at:]
    old = int(re.findall(rb"startxref\s+(\d+)", crafted)[-1])
    crafted = crafted.replace(b"startxref\n%d" % old, b"startxref\n%d" % (old + len(hidden)))

    result = verified(tmp_path, crafted)

    assert b"/URI" not in result
    assert result != crafted


@pytest.mark.parametrize(
    "content",
    [
        b"/Span << /AF [ << /Type /Filespec /F (a.pdf) >> ] >> BDC (x) Tj EMC",
        b"/Span << /A << /B << /AF 1 >> >> >> DP",
        b"/Span << /X /Annots >> BDC (x) Tj EMC",
        b"BI /W 1 /H 1 /BPC 8 /CS /G /URL (https://example.net/x) ID \x00 EI",
        b"/Span << /MCID 0 BDC (x) Tj EMC",
    ],
)
def test_t600_inline_dictionaries_with_a_name_of_the_net_are_refused(
    tmp_path: Path, content: bytes
) -> None:
    def change(pdf: Pdf) -> None:
        pdf.pages[0].obj.Contents = Stream(pdf, content)

    refused(tmp_path, stub(change))


def test_t600_a_contents_array_divided_inside_a_property_list_is_read_as_one(
    tmp_path: Path,
) -> None:
    def change(pdf: Pdf) -> None:
        pdf.pages[0].obj.Contents = Array(
            [Stream(pdf, b"/Span <<"), Stream(pdf, b"/AF [] >> BDC (x) Tj EMC")]
        )

    refused(tmp_path, stub(change))


@pytest.mark.parametrize("holder", ["form", "pattern", "glyph"])
def test_t600_inline_dictionaries_in_forms_patterns_and_glyphs_are_read(
    tmp_path: Path, holder: str
) -> None:
    content = b"/Span << /AF [] >> BDC 0 0 m 1 1 l S EMC"

    def change(pdf: Pdf) -> None:
        resources = pdf.pages[0].obj.Resources
        stream = Stream(pdf, content)
        if holder == "form":
            stream.Type = Name.XObject
            stream.Subtype = Name.Form
            stream.BBox = [0, 0, 1, 1]
            resources.XObject = Dictionary(Fm=stream)
        elif holder == "pattern":
            stream.PatternType = 1
            stream.PaintType = 1
            stream.TilingType = 1
            stream.BBox = [0, 0, 1, 1]
            stream.XStep = 1
            stream.YStep = 1
            stream.Resources = Dictionary()
            resources.Pattern = Dictionary(P1=stream)
        else:
            resources.Font.F3 = Dictionary(
                Type=Name.Font,
                Subtype=Name.Type3,
                FontBBox=[0, 0, 1, 1],
                FontMatrix=[1, 0, 0, 1, 0, 0],
                CharProcs=Dictionary(a=stream),
                Encoding=Dictionary(Differences=[97, Name.a]),
            )

    refused(tmp_path, stub(change))


def test_t600_tagged_property_lists_pass_and_stay_as_written(tmp_path: Path) -> None:
    content = (
        b"/P << /MCID 0 /ActualText (a) /Alt (b) /Lang (en) /E (c) >> BDC (x) Tj EMC "
        b"/OC /oc1 BDC (y) Tj EMC"
    )

    def setup(pdf: Pdf, page: pikepdf.Page) -> None:
        page.obj.Contents = Stream(pdf, b"BT /F1 12 Tf 10 100 Td " + content + b" ET")
        page.obj.Resources.Properties = Dictionary(oc1=Dictionary(Type=Name.OCG, Name=String("L")))

    data = copied(source_pdf(setup))
    result = verified(tmp_path, data)

    with pikepdf.open(io.BytesIO(result)) as pdf:
        assert b"/ActualText (a)" in pdf.pages[0].obj.Contents.read_bytes()


def test_t600_the_inline_image_dictionary_reads_as_written() -> None:
    pdf = Pdf.new()
    page = pdf.add_blank_page()
    page.obj.Contents = Stream(
        pdf, b"BI /W 1 /H 1 /BPC 8 /CS /G /URL (https://example.net/x) ID \x00 EI"
    )

    (item,) = pikepdf.parse_content_stream(page.obj, pdf_schema.CONTENT_OPERATORS)
    dictionary = item.iimage.obj  # type: ignore[union-attr]

    assert "/URL" in dictionary
    assert "/W" in dictionary or "/Width" in dictionary


def test_t622_an_inline_dictionary_past_the_depth_limit_is_refused(tmp_path: Path) -> None:
    deep = b"<< /A " * 64 + b"1" + b" >>" * 64

    def change(pdf: Pdf) -> None:
        pdf.pages[0].obj.Contents = Stream(pdf, b"/Span " + deep + b" BDC (x) Tj EMC")

    refused(tmp_path, stub(change))


def test_the_page_tree_must_name_each_parent_and_count_its_pages(tmp_path: Path) -> None:
    # qpdf rewrites the page tree it writes, so the forged trees are byte edits of one length.
    data = stub()
    refused(tmp_path, data.replace(b"/Count 1", b"/Count 2", 1))
    refused(tmp_path, data.replace(b"/Parent 2 0 R", b"/Parent 1 0 R", 1))
    refused(tmp_path, data.replace(b"/Type /Pages", b"/Type /Pagez", 1))


# --- The commands (sections 14.2 and 14.7) -----------------------------------------------------


def test_t614_render_assemble_and_verify_build_the_whole_document(tmp_path: Path) -> None:
    (tmp_path / "source.bin").write_bytes(source_pdf(pages=3))

    assert pdf_schema.render_pages(tmp_path, child_request("render")) == RenderOk(
        ok=True, pageCount=3
    )
    for number in (1, 2, 3):
        assert (tmp_path / f"{number}.pdf").exists()
    assert not list(tmp_path.glob("*.part"))
    assert pdf_schema.assemble_document(
        tmp_path, child_request("assemble", parameters={"pageCount": 3})
    ) == AssembleOk(ok=True, pageCount=3, documentPdf="built")
    assert pdf_schema.verify_render(
        tmp_path, child_request("verify-render", mode="document", parameters={"pageCount": 3})
    ) == Ok(ok=True)
    with pikepdf.open(tmp_path / "document.verified.pdf") as document:
        assert len(document.pages) == 3
        assert set(document.Root.keys()) == {"/Type", "/Pages"}


def test_t614_each_page_of_the_whole_document_keeps_its_own_font_and_image(tmp_path: Path) -> None:
    def setup_for(base: str) -> Setup:
        def setup(pdf: Pdf, page: pikepdf.Page) -> None:
            page.obj.Resources = Dictionary(
                Font=Dictionary(F1=font(pdf, base)), XObject=Dictionary(Im=image(pdf))
            )

        return setup

    for number, base in ((1, "Helvetica"), (2, "Courier")):
        (tmp_path / f"{number}.pdf").write_bytes(copied(source_pdf(setup_for(base))))

    pdf_schema.assemble_document(tmp_path, child_request("assemble", parameters={"pageCount": 2}))

    with pikepdf.open(tmp_path / "document.pdf") as document:
        fonts = [str(page.obj.Resources.Font.F1.BaseFont) for page in document.pages]
        assert fonts == ["/Helvetica", "/Courier"]


def test_t622_assemble_above_the_document_cap_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "1.pdf").write_bytes(copied(source_pdf()))
    monkeypatch.setattr(pdf_schema, "DOCUMENT_CONTAINER_CAP", 2)

    result = pdf_schema.assemble_document(
        tmp_path, child_request("assemble", parameters={"pageCount": 1})
    )

    assert result == AssembleOk(ok=True, pageCount=1, documentPdf="too_large")
    assert not (tmp_path / "document.pdf").exists()


def test_t614_assemble_fails_on_a_page_file_of_two_pages_or_no_page_count(tmp_path: Path) -> None:
    (tmp_path / "1.pdf").write_bytes(stub(pages=2))

    assert pdf_schema.assemble_document(
        tmp_path, child_request("assemble", parameters={"pageCount": 1})
    ) == RenditionFailed(ok=False, code="render_failed")
    assert pdf_schema.assemble_document(tmp_path, child_request("assemble")) == RenditionFailed(
        ok=False, code="render_failed"
    )


def test_t614_document_mode_refuses_another_page_count(tmp_path: Path) -> None:
    (tmp_path / "document.pdf").write_bytes(stub(pages=2))

    assert pdf_schema.verify_render(
        tmp_path, child_request("verify-render", mode="document", parameters={"pageCount": 3})
    ) == RenditionFailed(ok=False, code="render_failed")


def test_t600_verify_render_in_page_mode_writes_each_verified_page(tmp_path: Path) -> None:
    for number in (3, 4):
        (tmp_path / f"{number}.pdf").write_bytes(copied(source_pdf()))

    request = child_request("verify-render", mode="pages", pages={"first": 3, "last": 4})

    assert pdf_schema.verify_render(tmp_path, request) == Ok(ok=True)
    assert (tmp_path / "4.verified.pdf").exists()
    (tmp_path / "4.pdf").write_bytes(
        stub(lambda pdf: pdf.Root.__setitem__("/OpenAction", javascript(pdf)))
    )
    assert pdf_schema.verify_render(tmp_path, request) == RenditionFailed(
        ok=False, code="render_failed"
    )
    assert pdf_schema.verify_render(tmp_path, child_request("verify-render")) == RenditionFailed(
        ok=False, code="render_failed"
    )


def test_render_stops_with_too_large_or_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "source.bin").write_bytes(source_pdf())
    monkeypatch.setattr(pdf_schema, "PAGE_CONTAINER_CAP", 2)

    assert pdf_schema.render_pages(tmp_path, child_request("render")) == RenderFailed(
        ok=False, code="render_too_large"
    )


def test_t600_render_fails_a_page_whose_resource_names_hold_a_name_of_the_net(
    tmp_path: Path,
) -> None:
    def setup(pdf: Pdf, page: pikepdf.Page) -> None:
        page.obj.Resources.Font.FS = font(pdf)

    (tmp_path / "source.bin").write_bytes(source_pdf(setup))

    assert pdf_schema.render_pages(tmp_path, child_request("render")) == RenderFailed(
        ok=False, code="render_failed"
    )
    assert not (tmp_path / "1.pdf").exists()


def test_render_fails_a_page_of_an_undefined_kind(tmp_path: Path) -> None:
    def setup(pdf: Pdf, page: pikepdf.Page) -> None:
        page.obj.Resources.Font.F1.Subtype = Name.OpenType

    (tmp_path / "source.bin").write_bytes(source_pdf(setup))

    assert pdf_schema.render_pages(tmp_path, child_request("render")) == RenderFailed(
        ok=False, code="render_failed"
    )


def test_render_writes_page_files_one_at_a_time_and_waits_for_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "source.bin").write_bytes(source_pdf(pages=2))
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        # The parent takes the finished pages while the child waits.
        waits.append(seconds)
        for path in tmp_path.glob("[0-9]*.pdf"):
            path.unlink()

    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(pdf_schema, "_finished_pages", lambda directory: 4 if not waits else 0)
    request = child_request("render")

    assert pdf_schema.render_pages(tmp_path, request) == RenderOk(ok=True, pageCount=2)
    assert waits == [pdf_schema.FINISHED_PAGES_POLL_SECONDS]


def test_check_layout_allows_one_line_ending_after_the_last_eof() -> None:
    for ending in (b"", b"\n", b"\r\n", b"\r"):
        check_layout(b"%PDF-1.7\n%%EOF" + ending)
    for bad in (b"\n\n", b" ", b"\r\r"):
        with pytest.raises(SchemaError):
            check_layout(b"%PDF-1.7\n%%EOF" + bad)
    with pytest.raises(SchemaError):
        check_layout(b"%PDF-1.7 without an end")


def test_a_file_above_its_byte_cap_or_that_doesnt_open_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "1.pdf"
    path.write_bytes(copied(source_pdf()))
    with pytest.raises(SchemaError):
        pdf_schema.check_path(path, mode="pages", page_count=1, byte_cap=10)
    with pytest.raises(SchemaError):
        verify_file(path, tmp_path / "v.pdf", mode="pages", page_count=1, byte_cap=10)
    path.write_bytes(b"%PDF-1.7\n%%EOF\n")
    with pytest.raises(SchemaError):
        pdf_schema.check_path(path, mode="pages", page_count=1, byte_cap=PAGE_BYTES)
    with pytest.raises(SchemaError):
        verify_file(path, tmp_path / "v.pdf", mode="pages", page_count=1, byte_cap=PAGE_BYTES)


def test_t620_attributes_inherited_from_the_page_tree_reach_the_page(tmp_path: Path) -> None:
    pdf = Pdf.new()
    page = pdf.add_blank_page(page_size=(300, 400))
    page.obj.Resources = Dictionary(Font=Dictionary(F1=font(pdf)))
    page.obj.Contents = Stream(pdf, b"BT /F1 12 Tf 10 100 Td (Inherited) Tj ET")
    pdf.Root.Pages.MediaBox = page.obj.MediaBox
    pdf.Root.Pages.Rotate = 270
    pdf.Root.Pages.Resources = page.obj.Resources
    del page.obj["/MediaBox"]
    del page.obj["/Resources"]
    buffer = io.BytesIO()
    pdf.save(buffer)

    data = copied(buffer.getvalue())

    with pikepdf.open(io.BytesIO(data)) as out:
        copy = out.pages[0].obj
        assert list(copy.MediaBox) == [0, 0, 300, 400]
        assert copy.Rotate == 270
        assert copy.Resources.Font.F1.BaseFont == Name.Helvetica
        assert copy.Contents.read_bytes() == b"BT /F1 12 Tf 10 100 Td (Inherited) Tj ET"
    checked(tmp_path, data)


# --- T-192: page files rendered from the committed rendition fixtures ---------------------------

FIXTURES = Path(__file__).resolve().parents[5] / "tests" / "fixtures" / "documents"

FORBIDDEN_KEYS = frozenset(
    {
        "/Annots",
        "/A",
        "/AA",
        "/OpenAction",
        "/Next",
        "/B",
        "/Threads",
        "/PA",
        "/URI",
        "/JS",
        "/EF",
        "/AF",
        "/Outlines",
        "/Names",
        "/Dests",
        "/AcroForm",
        "/PageMode",
        "/PresSteps",
        "/NA",
        "/Alternates",
        "/Ref",
        "/OPI",
        "/X1",
    }
)
"""Keys of the sources' actions, annotations, outlines, threads and schema constructs."""

FORBIDDEN_NAMES = frozenset(
    {
        "/JavaScript",
        "/Launch",
        "/URI",
        "/GoTo",
        "/GoToR",
        "/GoToE",
        "/SubmitForm",
        "/ResetForm",
        "/ImportData",
        "/Annot",
        "/Widget",
        "/Link",
        "/Square",
        "/EmbeddedFile",
        "/Filespec",
        "/Action",
        "/Outlines",
    }
)

SCHEMA_FIXTURES = (
    "schema-presteps-gotor.pdf",
    "schema-gotoe-unlisted.pdf",
    "schema-page-af.pdf",
    "schema-image-alternates.pdf",
    "schema-form-ref.pdf",
    "schema-stream-f.pdf",
    "schema-font-unlisted.pdf",
    "schema-annotation-ap.pdf",
    "schema-resources-unknown.pdf",
)


def rendered_pages(tmp_path: Path, source: bytes) -> list[bytes]:
    """Every page file `render` writes from `source`, each also passing `verify-render`."""
    (tmp_path / "source.bin").write_bytes(source)
    with pytest.MonkeyPatch.context() as patch:
        # Every page stays for the checks; the on-disk bound is T-193's subject.
        patch.setattr(pdf_schema, "_finished_pages", lambda directory: 0)
        result = pdf_schema.render_pages(tmp_path, child_request("render"))
    assert isinstance(result, RenderOk), result
    request = child_request(
        "verify-render", mode="pages", pages={"first": 1, "last": result.page_count}
    )
    assert pdf_schema.verify_render(tmp_path, request) == Ok(ok=True)
    return [(tmp_path / f"{number}.pdf").read_bytes() for number in range(1, result.page_count + 1)]


def keys_and_names(pdf: Pdf) -> tuple[set[str], set[str], set[str]]:
    """Every dictionary key, every name value and every stream-dictionary key in `pdf`."""
    keys: set[str] = set()
    names: set[str] = set()
    stream_keys: set[str] = set()

    def walk(value: object, *, top: bool = False) -> None:
        # Indirect objects are each walked once from `pdf.objects`, so cycles end there.
        if not top and isinstance(value, pikepdf.Object) and value.is_indirect:
            return
        if isinstance(value, Name):
            names.add(str(value))
        elif isinstance(value, Array):
            for item in value:
                walk(item)
        elif isinstance(value, Dictionary | Stream):
            for key in value.keys():  # noqa: SIM118 - a pikepdf dictionary iterates its keys only by keys()
                keys.add(key)
                if isinstance(value, Stream):
                    stream_keys.add(key)
                walk(value[key])

    for obj in pdf.objects:
        walk(obj, top=True)
    walk(pdf.trailer, top=True)
    return keys, names, stream_keys


def shown_strings(page: pikepdf.Page) -> set[bytes]:
    """The string operands the page's own content stream shows."""
    shown: set[bytes] = set()
    for instruction in pikepdf.parse_content_stream(page):
        if isinstance(instruction, pikepdf.ContentStreamInstruction) and str(
            instruction.operator
        ) in {"Tj", "'", '"', "TJ"}:
            for operand in instruction.operands:
                items = operand if isinstance(operand, Array) else [operand]
                shown.update(bytes(item) for item in items if isinstance(item, String))
    return shown


def assert_sanitised(data: bytes) -> None:
    with pikepdf.open(io.BytesIO(data)) as pdf:
        assert len(pdf.pages) == 1
        assert set(pdf.Root.keys()) == set(pdf_schema.CATALOGUE_KEYS)
        assert set(pdf.trailer.keys()) <= pdf_schema.TRAILER_KEYS
        page_keys = set(pdf.pages[0].obj.keys())
        assert page_keys <= set(pdf_schema.PAGE_KEYS) | set(pdf_schema.PAGE_TREE_KEYS)
        keys, names, stream_keys = keys_and_names(pdf)
        assert keys & FORBIDDEN_KEYS == set()
        assert names & FORBIDDEN_NAMES == set()
        assert "/F" not in stream_keys
        assert "/FFilter" not in stream_keys
        assert "/FDecodeParms" not in stream_keys


def test_t192_every_page_rendered_from_active_content_is_sanitised(tmp_path: Path) -> None:
    source = (FIXTURES / "active-content.pdf").read_bytes()
    with pikepdf.open(io.BytesIO(source)) as original:
        assert "/Outlines" in original.Root
        assert {"/Names", "/PageMode", "/URI"} <= set(original.Root.keys())
        assert all("/Annots" in page.obj for page in original.pages)
        own = [shown_strings(page) for page in original.pages]

    pages = rendered_pages(tmp_path, source)

    assert len(pages) == len(own) == 2
    for number, data in enumerate(pages):
        assert_sanitised(data)
        with pikepdf.open(io.BytesIO(data)) as pdf:
            shown = shown_strings(pdf.pages[0])
        other = own[1 - number] - own[number]
        assert own[number] - own[1 - number] <= shown
        assert shown & other == set()


@pytest.mark.parametrize("name", SCHEMA_FIXTURES)
def test_t192_the_page_of_each_rendition_schema_fixture_holds_none_of_its_construct(
    tmp_path: Path, name: str
) -> None:
    (data,) = rendered_pages(tmp_path, (FIXTURES / name).read_bytes())

    assert_sanitised(data)
    with pikepdf.open(io.BytesIO(data)) as pdf:
        resources = pdf.pages[0].obj.Resources
        xobjects = resources.get("/XObject", Dictionary())
        if name == "schema-stream-f.pdf":
            # The image's data travels in the page file, not in a file the stream names.
            images = [x for x in xobjects.values() if x.get("/Subtype") == Name.Image]
            assert images
            assert all(image.read_raw_bytes() for image in images)
        if name == "schema-annotation-ap.pdf":
            # The square's and the filled field's appearances are drawn as form XObjects.
            forms = [x for x in xobjects.values() if x.get("/Subtype") == Name.Form]
            assert len(forms) >= 2
            drawn = pdf.pages[0].obj.Contents
            content = b"".join(
                part.read_bytes() for part in (drawn if isinstance(drawn, Array) else [drawn])
            )
            assert content.count(b" Do") >= 2


@pytest.mark.skipif(
    not Path(convert.SOFFICE).exists(), reason="LibreOffice isn't installed at /usr/lib/libreoffice"
)
@pytest.mark.parametrize(("name", "content_type"), [("headings.docx", DOCX), ("slides.pptx", PPTX)])
def test_t192_every_page_rendered_from_a_converted_office_fixture_is_sanitised(
    tmp_path: Path, name: str, content_type: str
) -> None:
    converting = tmp_path / "convert"
    (converting / convert.PROFILE_USER_DIRECTORY).mkdir(parents=True)
    (converting / convert.OUTPUT_DIRECTORY).mkdir()
    (converting / convert.PROFILE_FILE).write_bytes(convert.PROFILE)
    (converting / "source.bin").write_bytes((FIXTURES / name).read_bytes())
    assert convert.convert_in_child(converting, content_type) == Ok(ok=True)
    rendering = tmp_path / "render"
    rendering.mkdir()

    pages = rendered_pages(rendering, (converting / "converted.pdf").read_bytes())

    assert pages
    for data in pages:
        assert_sanitised(data)


# --- T-193: the render bounds ---------------------------------------------------------------------

SHARED_IMAGE_PAGES = 2000
SHARED_IMAGE_SIDE = 400


def shared_image_2000() -> bytes:
    """`shared-image-2000.pdf`: 2,000 pages drawing one image object, so the file stays small
    while every page file `render` writes carries its own copy of the image."""
    pdf = Pdf.new()
    pixels = hashlib.shake_256(b"shared-image-2000").digest(3 * SHARED_IMAGE_SIDE**2)
    shared = Stream(pdf, zlib.compress(pixels, 1))
    shared.Type = Name.XObject
    shared.Subtype = Name.Image
    shared.Width = SHARED_IMAGE_SIDE
    shared.Height = SHARED_IMAGE_SIDE
    shared.BitsPerComponent = 8
    shared.ColorSpace = Name.DeviceRGB
    shared.Filter = Name.FlateDecode
    shared = pdf.make_indirect(shared)
    resources = pdf.make_indirect(Dictionary(XObject=Dictionary(Im1=shared)))
    content = pdf.make_indirect(Stream(pdf, b"q 200 0 0 200 0 0 cm /Im1 Do Q"))
    for _ in range(SHARED_IMAGE_PAGES):
        page = pdf.add_blank_page(page_size=(200, 200))
        page.obj.Resources = resources
        page.obj.Contents = content
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def test_t193_shared_image_2000_is_render_too_large_within_500_mib_and_four_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = shared_image_2000()
    total_cap = results.render_max_total_bytes(len(source))
    assert total_cap == 524_288_000
    (tmp_path / "source.bin").write_bytes(source)
    taken: list[int] = []
    most_on_disk = 0
    finished_pages = pdf_schema._finished_pages

    def counted(directory: Path) -> int:
        nonlocal most_on_disk
        count = finished_pages(directory)
        most_on_disk = max(most_on_disk, count)
        return count

    def parent_takes_the_pages(seconds: float) -> None:
        # The parent uploads and deletes the finished pages while the child waits.
        for path in sorted(tmp_path.glob("[0-9]*.pdf")):
            taken.append(path.stat().st_size)
            path.unlink()

    monkeypatch.setattr(pdf_schema, "_finished_pages", counted)
    monkeypatch.setattr(time, "sleep", parent_takes_the_pages)
    request = child_request(
        "render", limits=Limits(max_pages=2000, render_max_total_bytes=total_cap)
    )

    result = pdf_schema.render_pages(tmp_path, request)

    left = [path.stat().st_size for path in tmp_path.glob("[0-9]*.pdf")]
    assert result == RenderFailed(ok=False, code="render_too_large")
    assert most_on_disk <= 4
    assert len(left) <= 4
    finished = sum(taken) + sum(left)
    assert finished <= total_cap
    assert len(taken) + len(left) < SHARED_IMAGE_PAGES
