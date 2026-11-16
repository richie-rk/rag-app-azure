"""Write the fixture documents of `84-testing.md` section 1.5.

Usage: uv run --frozen python tests/fixtures/documents/generate.py [--out <dir>] [--only <name>]

Writes every fixture file that section 1.5 says this script writes, beside it (or into
`--out`): the four shared documents (`tables.pdf`, `scanned.pdf`, `headings.docx`,
`slides.pptx`), the passing base files, the archive and document check fixtures, the converter
fixtures of `14-ingestion.md` section 15.11, the rendition schema, schema limit and inline
dictionary fixtures of section 14.8 and the whole-document fixture. The six Office-saved controls,
the `<file>.expected.json` files and the recorded layout results are not written here.

A regeneration writes identical bytes on any machine with the locked dependencies: every
timestamp, identifier and "random" value is fixed, and every compressed byte comes from the
pure-Python encoders below rather than from the zlib the interpreter or a wheel links against,
because zlib builds (zlib, zlib-ng, Apple's) emit different deflate streams for the same input.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import re
import struct
import sys
import uuid
import zipfile
import zlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import docx
import pikepdf
import pptx
from docx.document import Document as WordDocument
from docx.opc.packuri import PackURI as WordPackURI
from docx.opc.part import Part as WordPart
from docx.oxml import parse_xml as parse_word_xml
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from PIL import Image, ImageDraw, ImageFont
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.oxml import parse_xml as parse_slide_xml
from pptx.presentation import Presentation as SlideDeck
from pptx.slide import Slide
from pptx.util import Inches as SlideInches

HERE: Final = Path(__file__).resolve().parent

# Fixed values, so a regeneration is byte-identical.
FIXED_TIME: Final = dt.datetime(2026, 1, 15, 9, 30, tzinfo=dt.UTC)
AUTHOR: Final = "DocChat fixture generator"

# Names the converter fixtures point at (`14-ingestion.md` section 15.11 item 6). The converter
# linked-content test writes the canary file at CANARY_PATH, in a scratch directory it creates
# for another parse user, and listens on LOOPBACK_URL's port with its HTTP stand-in.
HOSTNAME_URL: Final = "file:///etc/hostname"
CANARY_DIRECTORY: Final = "/run/docchat-scratch/docchat-sibling"
CANARY_PATH: Final = f"{CANARY_DIRECTORY}/canary.txt"
CANARY_URL: Final = f"file://{CANARY_PATH}"
LOOPBACK_URL: Final = "http://127.0.0.1:18080/linked-image.png"
UNC_IMAGE_TARGET: Final = r"file:///\\host\share\a.png"
MACRO_MARKER_PATH: Final = "/tmp/docchat-macro-marker.txt"  # noqa: S108 - the path a macro would write

PDF_PAGE: Final = (612, 792)  # US Letter, in points
SCAN_DPI: Final = 200


# =============================================================================================
# Deflate (RFC 1951), written here so that compressed bytes don't depend on the zlib build
# =============================================================================================


def _code_bases(first: int, extra_bits: Sequence[int]) -> tuple[int, ...]:
    """The base value of each length or distance code: each code covers 2**extra values."""
    bases = [first]
    for extra in extra_bits[:-1]:
        bases.append(bases[-1] + (1 << extra))
    return tuple(bases)


# RFC 1951 section 3.2.5: length codes 257 to 285 and distance codes 0 to 29. Code 285 stands
# for the length 258 alone, where the doubling rule would give 259.
_LENGTH_EXTRA: Final = (*(0,) * 8, *(bits for bits in range(1, 6) for _ in range(4)), 0)
_LENGTH_BASES: Final = (*_code_bases(3, _LENGTH_EXTRA)[:-1], 258)
_DISTANCE_EXTRA: Final = (0, 0, *(bits for bits in range(14) for _ in range(2)))
_DISTANCE_BASES: Final = _code_bases(1, _DISTANCE_EXTRA)
_WINDOW: Final = 32768
_MAX_MATCH: Final = 258


def _reverse_bits(value: int, width: int) -> int:
    result = 0
    for _ in range(width):
        result = (result << 1) | (value & 1)
        value >>= 1
    return result


def _fixed_literal_code(symbol: int) -> tuple[int, int]:
    """The fixed Huffman code of a literal/length symbol, bit-reversed for LSB-first packing."""
    if symbol < 144:
        code, width = 0x30 + symbol, 8
    elif symbol < 256:
        code, width = 0x190 + symbol - 144, 9
    elif symbol < 280:
        code, width = symbol - 256, 7
    else:
        code, width = 0xC0 + symbol - 280, 8
    return _reverse_bits(code, width), width


def _length_table() -> list[tuple[int, int]]:
    table = [(0, 0)] * (_MAX_MATCH + 1)
    for index, base in enumerate(_LENGTH_BASES):
        extra = _LENGTH_EXTRA[index]
        code, width = _fixed_literal_code(257 + index)
        for offset in range(1 << extra):
            length = base + offset
            if length <= _MAX_MATCH and table[length] == (0, 0):
                table[length] = (code | (offset << width), width + extra)
    return table


def _distance_table() -> list[tuple[int, int]]:
    table = [(0, 0)] * (_WINDOW + 1)
    for index, base in enumerate(_DISTANCE_BASES):
        extra = _DISTANCE_EXTRA[index]
        code = _reverse_bits(index, 5)
        for offset in range(1 << extra):
            distance = base + offset
            if distance <= _WINDOW:
                table[distance] = (code | (offset << 5), 5 + extra)
    return table


_LITERALS: Final = [_fixed_literal_code(symbol) for symbol in range(256)]
_LENGTHS: Final = _length_table()
_DISTANCES: Final = _distance_table()
_DEFLATE_CACHE: dict[bytes, bytes] = {}


def _match_length(data: bytes, candidate: int, position: int, limit: int) -> int:
    length = 0
    while (
        length + 16 <= limit
        and data[candidate + length : candidate + length + 16]
        == data[position + length : position + length + 16]
    ):
        length += 16
    while length < limit and data[candidate + length] == data[position + length]:
        length += 1
    return length


def deflate(data: bytes) -> bytes:
    """Raw deflate: one final block of fixed Huffman codes, greedy LZ77 over a 32 KiB window.

    Each position tries two earlier positions, the last one that began with the same three
    bytes and the one at the distance of the previous match, and takes the longer match."""
    digest = hashlib.sha256(data).digest()
    cached = _DEFLATE_CACHE.get(digest)
    if cached is not None:
        return cached
    out = bytearray()
    accumulator, count = 0b011, 3  # BFINAL = 1, BTYPE = 01 (fixed Huffman codes)
    last: dict[bytes, int] = {}
    size = len(data)
    position = 0
    previous_distance = 0
    while position < size:
        length = distance = 0
        if position + 2 < size:
            key = data[position : position + 3]
            limit = min(_MAX_MATCH, size - position)
            candidate = last.get(key)
            last[key] = position
            if candidate is not None and position - candidate <= _WINDOW:
                length = _match_length(data, candidate, position, limit)
                distance = position - candidate
            if (
                previous_distance
                and previous_distance != distance
                and position >= previous_distance
            ):
                repeat = _match_length(data, position - previous_distance, position, limit)
                if repeat > length:
                    length, distance = repeat, previous_distance
            if length < 3:
                length = 0
        if length:
            bits, width = _LENGTHS[length]
            accumulator |= bits << count
            count += width
            bits, width = _DISTANCES[distance]
            accumulator |= bits << count
            count += width
            for inner in range(position + 1, min(position + length, size - 2)):
                last[data[inner : inner + 3]] = inner
            previous_distance = distance
            position += length
        else:
            bits, width = _LITERALS[data[position]]
            accumulator |= bits << count
            count += width
            position += 1
        if count >= 64:
            out += (accumulator & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")
            accumulator >>= 64
            count -= 64
    count += 7  # end of block: symbol 256 is seven zero bits
    out += accumulator.to_bytes((count + 7) // 8, "little")
    result = bytes(out)
    _DEFLATE_CACHE[digest] = result
    return result


def zlib_stream(data: bytes) -> bytes:
    """A zlib stream (RFC 1950) around `deflate(data)`, as PDF FlateDecode and PNG expect."""
    return b"\x78\x01" + deflate(data) + struct.pack(">I", zlib.adler32(data))


class _BitWriter:
    def __init__(self) -> None:
        self.out = bytearray()
        self.accumulator = 0
        self.count = 0

    def bits(self, value: int, width: int) -> None:
        self.accumulator |= value << self.count
        self.count += width
        while self.count >= 8:
            self.out.append(self.accumulator & 0xFF)
            self.accumulator >>= 8
            self.count -= 8

    def code(self, code: int, width: int) -> None:
        """A Huffman code, written most significant bit first."""
        self.bits(_reverse_bits(code, width), width)

    def zero_bits(self, count: int) -> None:
        while self.count and count:
            self.bits(0, 1)
            count -= 1
        self.out += bytes(count // 8)
        self.bits(0, count % 8)

    def finish(self) -> bytes:
        if self.count:
            self.out.append(self.accumulator & 0xFF)
        return bytes(self.out)


def deflate_zeros(size: int) -> bytes:
    """Raw deflate of `size` zero bytes at the format's highest ratio, 1,032 to 1.

    One dynamic Huffman block whose literal/length code has three symbols (the literal 0, the
    length 258 and the end of block) and whose distance code has one (distance 1): the literal
    0 starts the run, and every further 258 bytes cost two bits, a one-bit length and a one-bit
    distance, both of value 0.
    """
    writer = _BitWriter()
    writer.bits(1, 1)  # BFINAL
    writer.bits(2, 2)  # BTYPE = 10, dynamic Huffman codes
    writer.bits(286 - 257, 5)  # HLIT
    writer.bits(1 - 1, 5)  # HDIST
    writer.bits(18 - 4, 4)  # HCLEN
    # Code length code lengths, in the order of RFC 1951 section 3.2.7; symbol 18 (a run of
    # zeros) gets one bit, the lengths 1 and 2 two bits each.
    order = (16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1)
    code_length_lengths = {18: 1, 1: 2, 2: 2}
    for symbol in order:
        writer.bits(code_length_lengths.get(symbol, 0), 3)
    # Canonical codes of that alphabet: 18 -> 0, 1 -> 10, 2 -> 11.
    length_code = {18: (0b0, 1), 1: (0b10, 2), 2: (0b11, 2)}

    def zero_run(run: int) -> None:
        while run:
            step = min(run, 138)
            writer.code(*length_code[18])
            writer.bits(step - 11, 7)
            run -= step

    writer.code(*length_code[2])  # literal 0: length 2
    zero_run(255)
    writer.code(*length_code[2])  # end of block (256): length 2
    zero_run(28)
    writer.code(*length_code[1])  # length 258 (285): length 1
    writer.code(*length_code[1])  # distance 1 (code 0): length 1
    # Canonical literal/length codes: 285 -> 0, literal 0 -> 10, end of block -> 11.
    writer.code(0b10, 2)
    remaining = size - 1
    runs, tail = divmod(remaining, _MAX_MATCH)
    writer.zero_bits(2 * runs)
    for _ in range(tail):
        writer.code(0b10, 2)
    writer.code(0b11, 2)
    return writer.finish()


def crc32_of_zeros(size: int) -> int:
    chunk = bytes(1 << 20)
    crc = 0
    while size:
        step = min(size, len(chunk))
        crc = zlib.crc32(chunk[:step], crc)
        size -= step
    return crc


# =============================================================================================
# ZIP archives, written here for exact control of every entry and header
# =============================================================================================

ZIP_STORED: Final = 0
ZIP_DEFLATED: Final = 8
_DOS_DATE: Final = (0 << 9) | (1 << 5) | 1  # 1980-01-01, the earliest date a ZIP entry holds
_DOS_TIME: Final = 0


@dataclass
class ZipEntry:
    """One archive entry. `raw` is the entry's data as stored (already compressed or encrypted);
    when it is None, `data` is deflated (or stored) by the writer."""

    name: str
    data: bytes = b""
    method: int = ZIP_DEFLATED
    flags: int = 0
    raw: bytes | None = None
    crc: int | None = None
    size: int | None = None


def write_zip(entries: Sequence[ZipEntry], comment: bytes = b"") -> bytes:
    out = bytearray()
    central = bytearray()
    for entry in entries:
        name = entry.name.encode("utf-8")
        flags = entry.flags | (0x800 if not entry.name.isascii() else 0)
        if entry.raw is not None:
            stored = entry.raw
        elif entry.method == ZIP_DEFLATED:
            stored = deflate(entry.data)
        else:
            stored = entry.data
        crc = entry.crc if entry.crc is not None else zlib.crc32(entry.data)
        size = entry.size if entry.size is not None else len(entry.data)
        offset = len(out)
        out += struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            20,
            flags,
            entry.method,
            _DOS_TIME,
            _DOS_DATE,
            crc,
            len(stored),
            size,
            len(name),
            0,
        )
        out += name
        out += stored
        central += struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50,
            20,
            20,
            flags,
            entry.method,
            _DOS_TIME,
            _DOS_DATE,
            crc,
            len(stored),
            size,
            len(name),
            0,
            0,
            0,
            0,
            0,
            offset,
        )
        central += name
    start = len(out)
    out += central
    out += struct.pack(
        "<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(central), start, len(comment)
    )
    out += comment
    return bytes(out)


def _crc_table() -> list[int]:
    table = []
    for byte in range(256):
        value = byte
        for _ in range(8):
            value = (value >> 1) ^ 0xEDB88320 if value & 1 else value >> 1
        table.append(value)
    return table


_CRC_TABLE: Final = _crc_table()


def zipcrypto_encrypt(data: bytes, password: bytes, crc: int) -> bytes:
    """Traditional PKWARE encryption (APPNOTE section 6.1) with a fixed 12-byte header."""
    keys = [0x12345678, 0x23456789, 0x34567890]

    def update(byte: int) -> None:
        keys[0] = (keys[0] >> 8) ^ _CRC_TABLE[(keys[0] ^ byte) & 0xFF]
        keys[1] = ((keys[1] + (keys[0] & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
        keys[2] = (keys[2] >> 8) ^ _CRC_TABLE[(keys[2] ^ (keys[1] >> 24)) & 0xFF]

    def stream_byte() -> int:
        temp = (keys[2] | 2) & 0xFFFF
        return ((temp * (temp ^ 1)) >> 8) & 0xFF

    for byte in password:
        update(byte)
    header = bytes(range(1, 12)) + bytes([crc >> 24 & 0xFF])
    out = bytearray()
    for byte in header + data:
        out.append(byte ^ stream_byte())
        update(byte)
    return bytes(out)


def read_zip(blob: bytes) -> list[ZipEntry]:
    """The entries of an archive this script or python-docx/python-pptx wrote, in order."""
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return [ZipEntry(info.filename, archive.read(info)) for info in archive.infolist()]


# =============================================================================================
# PNG (ISO/IEC 15948), for images inside Office packages
# =============================================================================================


def png(width: int, height: int, rows: Iterable[bytes], *, color_type: int = 2) -> bytes:
    """A PNG of 8-bit samples; `color_type` 0 is grey, 2 is RGB. Each row has no filter byte."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    raw = b"".join(b"\x00" + row for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    physical = struct.pack(">IIB", 3780, 3780, 1)  # 96 dpi
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"pHYs", physical)
        + chunk(b"IDAT", zlib_stream(raw))
        + chunk(b"IEND", b"")
    )


def solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    row = bytes(rgb) * width
    return png(width, height, [row] * height)


def framed_png(
    width: int, height: int, fill: tuple[int, int, int], frame: tuple[int, int, int]
) -> bytes:
    """A filled rectangle with a 2-pixel frame, the preview picture of an object."""
    edge = bytes(frame) * width
    middle = bytes(frame) * 2 + bytes(fill) * (width - 4) + bytes(frame) * 2
    return png(width, height, [edge, edge] + [middle] * (height - 4) + [edge, edge])


# =============================================================================================
# PDF (ISO 32000-1), written object by object so each fixture holds exactly its construct
# =============================================================================================


class Name:
    """A PDF name object; `Name("Type")` is written `/Type`."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


@dataclass(frozen=True)
class Ref:
    """An indirect reference to object `number`, generation 0."""

    number: int


@dataclass
class Stream:
    """A stream object; `data` is stored as given, so it must already match `/Filter`."""

    dictionary: dict[str, PdfValue]
    data: bytes


@dataclass(frozen=True)
class Raw:
    """PDF syntax written verbatim, for a value already serialised."""

    text: bytes


type PdfValue = (
    Name
    | Ref
    | Stream
    | Raw
    | str
    | bytes
    | int
    | float
    | bool
    | list[PdfValue]
    | dict[str, PdfValue]
    | None
)
type StringCipher = Callable[[bytes], bytes]

_NAME_DELIMITERS: Final = frozenset(b"()<>[]{}/%#")


def _name_bytes(value: str) -> bytes:
    out = bytearray(b"/")
    for byte in value.encode("utf-8"):
        if byte < 0x21 or byte > 0x7E or byte in _NAME_DELIMITERS:
            out += b"#%02X" % byte
        else:
            out.append(byte)
    return bytes(out)


def literal_string(data: bytes) -> bytes:
    out = bytearray(b"(")
    for byte in data:
        if byte in b"\\()":
            out += b"\\" + bytes([byte])
        elif byte < 0x20 or byte > 0x7E:
            out += b"\\%03o" % byte
        else:
            out.append(byte)
    return bytes(out + b")")


def _hex_string(data: bytes) -> bytes:
    return b"<" + data.hex().upper().encode() + b">"


def num(value: float) -> bytes:
    """A PDF number: an integer, or a real with at most five decimals."""
    if isinstance(value, int):
        return b"%d" % value
    text = f"{value:.5f}".rstrip("0").rstrip(".")
    return (text if text not in ("-0", "") else "0").encode()


def _scalar(value: Name | Ref | Raw | bool | float | None) -> bytes:
    if isinstance(value, Name):
        return _name_bytes(value.value)
    if isinstance(value, Ref):
        return b"%d 0 R" % value.number
    if isinstance(value, Raw):
        return value.text
    if value is None:
        return b"null"
    if isinstance(value, bool):
        return b"true" if value else b"false"
    return num(value)


def serialise(value: PdfValue, cipher: StringCipher | None = None) -> bytes:
    """One direct value; `cipher` encrypts each string of an encrypted file's object."""
    if isinstance(value, Stream):
        raise TypeError(f"a stream can only be an indirect object: {value!r}")
    if isinstance(value, str | bytes):
        data = value.encode("latin-1") if isinstance(value, str) else value
        if cipher is not None:
            return _hex_string(cipher(data))
        return literal_string(data) if isinstance(value, str) else _hex_string(data)
    if isinstance(value, list):
        return b"[" + b" ".join(serialise(item, cipher) for item in value) + b"]"
    if isinstance(value, dict):
        parts = [_name_bytes(key) + b" " + serialise(item, cipher) for key, item in value.items()]
        return b"<<" + b" ".join(parts) + b">>"
    return _scalar(value)


def rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 0xFF
        state[i], state[j] = state[j], state[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) & 0xFF])
    return bytes(out)


def _md5(data: bytes) -> bytes:
    return hashlib.md5(data, usedforsecurity=False).digest()


_PASSWORD_PAD: Final = bytes.fromhex(
    "28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A"
)


@dataclass(frozen=True)
class Encryption:
    """The standard security handler, revision 3: RC4 with a 128-bit key (ISO 32000-1 7.6.3).

    Revision 3 is the newest revision whose output the passwords and the file identifier fully
    determine; the AES revisions take a random initialisation vector for every string.
    """

    owner_password: str
    user_password: str
    permissions: int

    def keys(self, file_id: bytes) -> tuple[bytes, bytes, bytes]:
        """The file key and the `/O` and `/U` values (algorithms 2, 3 and 5)."""

        def padded(password: str) -> bytes:
            return (password.encode("latin-1") + _PASSWORD_PAD)[:32]

        digest = _md5(padded(self.owner_password or self.user_password))
        for _ in range(50):
            digest = _md5(digest)
        owner_value = rc4(digest, padded(self.user_password))
        for round_ in range(1, 20):
            owner_value = rc4(bytes(byte ^ round_ for byte in digest), owner_value)
        permissions = struct.pack("<i", self.permissions)
        key = _md5(padded(self.user_password) + owner_value + permissions + file_id)
        for _ in range(50):
            key = _md5(key)
        user_value = rc4(key, _md5(_PASSWORD_PAD + file_id))
        for round_ in range(1, 20):
            user_value = rc4(bytes(byte ^ round_ for byte in key), user_value)
        return key, owner_value, user_value + bytes(16)


class PdfDocument:
    """Objects in creation order, written with a cross-reference table, or, for the large
    container fixtures, packed into compressed object streams with a cross-reference stream."""

    def __init__(self) -> None:
        self._objects: list[PdfValue] = [None]
        self._set: set[int] = set()

    def reserve(self) -> Ref:
        self._objects.append(None)
        return Ref(len(self._objects) - 1)

    def set(self, ref: Ref, value: PdfValue) -> Ref:
        self._objects[ref.number] = value
        self._set.add(ref.number)
        return ref

    def add(self, value: PdfValue) -> Ref:
        return self.set(self.reserve(), value)

    def write(
        self,
        root: Ref,
        seed: str,
        *,
        encryption: Encryption | None = None,
        object_streams: bool = False,
    ) -> bytes:
        missing = sorted(set(range(1, len(self._objects))) - self._set)
        if missing:
            raise ValueError(f"objects reserved but never set: {missing}")
        file_id = _md5(f"docchat-fixture:{seed}".encode())
        if object_streams:
            return self._write_compact(root, file_id)
        key = b""
        encrypt_number = 0
        extra: dict[str, PdfValue] = {}
        if encryption is not None:
            key, owner_value, user_value = encryption.keys(file_id)
            encrypt = self.add(
                {
                    "Filter": Name("Standard"),
                    "V": 2,
                    "R": 3,
                    "Length": 128,
                    "O": Raw(_hex_string(owner_value)),
                    "U": Raw(_hex_string(user_value)),
                    "P": encryption.permissions,
                }
            )
            encrypt_number = encrypt.number
            extra["Encrypt"] = encrypt
        trailer: dict[str, PdfValue] = {
            "Size": len(self._objects),
            "Root": root,
            **extra,
            "ID": [file_id, file_id],
        }
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets: list[int] = []
        for number in range(1, len(self._objects)):
            cipher: StringCipher | None = None
            if encryption is not None and number != encrypt_number:
                object_key = _md5(key + number.to_bytes(3, "little") + b"\x00\x00")

                def cipher(data: bytes, object_key: bytes = object_key) -> bytes:
                    return rc4(object_key, data)

            offsets.append(len(out))
            out += b"%d 0 obj\n" % number + self._body(self._objects[number], cipher)
            out += b"\nendobj\n"
        start = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % len(self._objects)
        for offset in offsets:
            out += b"%010d 00000 n \n" % offset
        out += b"trailer\n" + serialise(trailer) + b"\nstartxref\n%d\n%%%%EOF\n" % start
        return bytes(out)

    @staticmethod
    def _body(value: PdfValue, cipher: StringCipher | None) -> bytes:
        if isinstance(value, Stream):
            data = cipher(value.data) if cipher is not None else value.data
            dictionary = {**value.dictionary, "Length": len(data)}
            return serialise(dictionary, cipher) + b"\nstream\n" + data + b"\nendstream"
        return serialise(value, cipher)

    def _write_compact(self, root: Ref, file_id: bytes) -> bytes:
        """Every non-stream object goes into an object stream of at most 100 objects."""
        count = len(self._objects)
        packed = [n for n in range(1, count) if not isinstance(self._objects[n], Stream)]
        streams = [n for n in range(1, count) if isinstance(self._objects[n], Stream)]
        location: dict[int, tuple[int, int]] = {}
        holders: list[int] = []
        for first in range(0, len(packed), 100):
            group = packed[first : first + 100]
            holder = self.reserve().number
            header: list[bytes] = []
            bodies: list[bytes] = []
            offset = 0
            for index, number in enumerate(group):
                body = self._body(self._objects[number], None) + b"\n"
                header.append(b"%d %d" % (number, offset))
                bodies.append(body)
                offset += len(body)
                location[number] = (holder, index)
            head = b" ".join(header) + b"\n"
            self.set(
                Ref(holder),
                Stream(
                    {
                        "Type": Name("ObjStm"),
                        "N": len(group),
                        "First": len(head),
                        "Filter": Name("FlateDecode"),
                    },
                    zlib_stream(head + b"".join(bodies)),
                ),
            )
            holders.append(holder)
        xref_number = self.reserve().number
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, int] = {}
        for number in streams + holders:
            offsets[number] = len(out)
            out += b"%d 0 obj\n" % number + self._body(self._objects[number], None)
            out += b"\nendobj\n"
        offsets[xref_number] = len(out)
        rows = bytearray(struct.pack(">BIH", 0, 0, 0xFFFF))
        for number in range(1, len(self._objects)):
            if number in location:
                rows += struct.pack(">BIH", 2, *location[number])
            else:
                rows += struct.pack(">BIH", 1, offsets[number], 0)
        xref = Stream(
            {
                "Type": Name("XRef"),
                "Size": len(self._objects),
                "W": [1, 4, 2],
                "Root": root,
                "ID": [file_id, file_id],
                "Filter": Name("FlateDecode"),
            },
            zlib_stream(bytes(rows)),
        )
        out += b"%d 0 obj\n" % xref_number + self._body(xref, None) + b"\nendobj\n"
        out += b"startxref\n%d\n%%%%EOF\n" % offsets[xref_number]
        self._set.add(xref_number)
        self._objects[xref_number] = xref
        return bytes(out)


def flate(data: bytes, dictionary: dict[str, PdfValue] | None = None) -> Stream:
    """A FlateDecode stream of `data`."""
    return Stream({**(dictionary or {}), "Filter": Name("FlateDecode")}, zlib_stream(data))


# --- Fonts and text ---------------------------------------------------------------------------

# Advance widths of the standard 14 fonts Helvetica and Helvetica-Bold for WinAnsiEncoding
# codes 32 to 126, in thousandths of the font size (Adobe's core font metrics), used to lay out
# lines and table cells.
# fmt: off
_HELVETICA_WIDTHS: Final = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556,
    556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722,
    667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944,
    667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222,
    500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334,
    584,
)
_HELVETICA_BOLD_WIDTHS: Final = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556,
    556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667,
    611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667,
    667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556,
    278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
)
# fmt: on
WIDTHS: Final = {"F1": _HELVETICA_WIDTHS, "F2": _HELVETICA_BOLD_WIDTHS}


def standard_font(base_font: str) -> dict[str, PdfValue]:
    return {
        "Type": Name("Font"),
        "Subtype": Name("Type1"),
        "BaseFont": Name(base_font),
        "Encoding": Name("WinAnsiEncoding"),
    }


def text_fonts(document: PdfDocument) -> dict[str, PdfValue]:
    """The `/Font` resources of a text page: F1 Helvetica and F2 Helvetica-Bold."""
    return {
        "F1": document.add(standard_font("Helvetica")),
        "F2": document.add(standard_font("Helvetica-Bold")),
    }


def text_width(text: str, font: str, size: float) -> float:
    widths = WIDTHS[font]
    return sum(widths[ord(character) - 32] for character in text) * size / 1000


def wrap(text: str, font: str, size: float, width: float) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if line and text_width(candidate, font, size) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def pdf_text(text: str) -> bytes:
    if not text.isascii():
        raise ValueError(f"fixture PDF text is ASCII: {text!r}")
    return literal_string(text.encode("ascii"))


class Canvas:
    """A content stream, built from drawing operations."""

    def __init__(self) -> None:
        self._lines: list[bytes] = []

    def raw(self, line: str | bytes) -> None:
        self._lines.append(line.encode("latin-1") if isinstance(line, str) else line)

    def text(self, x: float, y: float, text: str, font: str = "F1", size: float = 10.5) -> None:
        self.raw(
            b"BT /%s %s Tf %s %s Td %s Tj ET"
            % (font.encode(), num(size), num(x), num(y), pdf_text(text))
        )

    def line(self, x1: float, y1: float, x2: float, y2: float, width: float = 0.5) -> None:
        self.raw(b"%s w %s %s m %s %s l S" % (num(width), num(x1), num(y1), num(x2), num(y2)))

    def fill_rect(self, x: float, y: float, w: float, h: float, gray: float) -> None:
        self.raw(b"q %s g %s %s %s %s re f Q" % (num(gray), num(x), num(y), num(w), num(h)))

    def stroke_rect(self, x: float, y: float, w: float, h: float, width: float = 0.5) -> None:
        self.raw(b"%s w %s %s %s %s re S" % (num(width), num(x), num(y), num(w), num(h)))

    def data(self) -> bytes:
        return b"\n".join(self._lines) + b"\n"


@dataclass
class PdfPage:
    """One page being built: its content, resources and any extra page keys."""

    canvas: Canvas = field(default_factory=Canvas)
    resources: dict[str, PdfValue] = field(default_factory=dict)
    extra: dict[str, PdfValue] = field(default_factory=dict)
    compress: bool = True


def assemble_pdf(
    document: PdfDocument,
    pages: Sequence[PdfPage],
    catalogue_extra: dict[str, PdfValue] | None = None,
    page_refs: Sequence[Ref] | None = None,
) -> Ref:
    """Add the pages, the page tree and the catalogue; return the catalogue's reference.

    `page_refs` lets a caller reserve the page objects first, so that annotations and
    destinations can name a page before it is written."""
    tree = document.reserve()
    refs = list(page_refs) if page_refs is not None else [document.reserve() for _ in pages]
    for ref, page in zip(refs, pages, strict=True):
        data = page.canvas.data()
        content = document.add(flate(data) if page.compress else Stream({}, data))
        document.set(
            ref,
            {
                "Type": Name("Page"),
                "Parent": tree,
                "MediaBox": [0, 0, PDF_PAGE[0], PDF_PAGE[1]],
                "Resources": page.resources,
                "Contents": content,
                **page.extra,
            },
        )
    document.set(tree, {"Type": Name("Pages"), "Kids": list(refs), "Count": len(refs)})
    return document.add({"Type": Name("Catalog"), "Pages": tree, **(catalogue_extra or {})})


# =============================================================================================
# Compound files ([MS-CFB] version 3), for the VBA project and the OLE package object
# =============================================================================================

_FREE_SECTOR: Final = 0xFFFFFFFF
_END_OF_CHAIN: Final = 0xFFFFFFFE
_FAT_SECTOR: Final = 0xFFFFFFFD
_NO_STREAM: Final = 0xFFFFFFFF
_MINI_CUTOFF: Final = 4096


@dataclass
class CfbNode:
    """A stream (`data` set) or a storage (`children` set) of a compound file."""

    name: str
    data: bytes | None = None
    children: list[CfbNode] = field(default_factory=list)
    clsid: bytes = bytes(16)


@dataclass
class _DirectoryEntry:
    name: str
    kind: int  # 1 storage, 2 stream, 5 root
    clsid: bytes = bytes(16)
    data: bytes = b""
    left: int = _NO_STREAM
    right: int = _NO_STREAM
    child: int = _NO_STREAM
    black: bool = True
    start: int = _END_OF_CHAIN
    size: int = 0


def _cfb_order(node: CfbNode) -> tuple[int, str]:
    return len(node.name), node.name.upper()


def _directory(children: Sequence[CfbNode], clsid: bytes) -> list[_DirectoryEntry]:
    """The directory entries: the root, then each storage's children as a red-black tree."""
    entries = [_DirectoryEntry("Root Entry", 5, clsid)]

    def add_storage(parent: int, nodes: Sequence[CfbNode]) -> None:
        ordered = sorted(nodes, key=_cfb_order)
        ids = []
        for node in ordered:
            is_stream = node.data is not None
            entries.append(
                _DirectoryEntry(
                    node.name,
                    2 if is_stream else 1,
                    node.clsid,
                    node.data or b"",
                    start=_END_OF_CHAIN if is_stream else 0,
                )
            )
            ids.append(len(entries) - 1)
        # Siblings form a red-black tree ordered by name; a tree split at medians has every leaf
        # on its last two levels, so colouring the last level red when it is incomplete keeps
        # each path's count of black nodes equal.
        depth: dict[int, int] = {}

        def build(low: int, high: int, level: int) -> int:
            if low >= high:
                return _NO_STREAM
            middle = (low + high) // 2
            node_id = ids[middle]
            depth[node_id] = level
            entries[node_id].left = build(low, middle, level + 1)
            entries[node_id].right = build(middle + 1, high, level + 1)
            return node_id

        entries[parent].child = build(0, len(ids), 0)
        if ids:
            deepest = max(depth.values())
            perfect = len(ids) == 2 ** (deepest + 1) - 1
            for node_id in ids:
                entries[node_id].black = perfect or depth[node_id] < deepest
        for node, node_id in zip(ordered, ids, strict=True):
            if node.data is None:
                add_storage(node_id, node.children)

    add_storage(0, children)
    return entries


@dataclass
class _Allocation:
    """Where each part of a compound file sits: the FAT, the mini stream and the sectors."""

    fat: list[int]
    fat_sectors: int
    directory_start: int
    mini_fat: list[int]
    mini_fat_start: int
    mini_stream: bytes
    large: list[_DirectoryEntry]


def _allocate(entries: list[_DirectoryEntry]) -> _Allocation:
    mini_stream = bytearray()
    mini_fat: list[int] = []
    large: list[_DirectoryEntry] = []
    for entry in entries:
        if entry.kind != 2:
            continue
        entry.size = len(entry.data)
        if entry.size == 0:
            entry.start = _END_OF_CHAIN
        elif entry.size < _MINI_CUTOFF:
            first = len(mini_stream) // 64
            count = -(-entry.size // 64)
            entry.start = first
            mini_fat += [*range(first + 1, first + count), _END_OF_CHAIN]
            mini_stream += entry.data + bytes(count * 64 - entry.size)
        else:
            large.append(entry)
    large_sectors = [-(-len(entry.data) // 512) for entry in large]
    counts = [-(-len(entries) // 4), -(-len(mini_fat) // 128), -(-len(mini_stream) // 512)]
    payload = sum(counts) + sum(large_sectors)
    fat_sectors = 1
    while fat_sectors * 128 < fat_sectors + payload:
        fat_sectors += 1
    fat = [_FAT_SECTOR] * fat_sectors

    def chain(count: int) -> int:
        if count == 0:
            return _END_OF_CHAIN
        start = len(fat)
        fat.extend([*range(start + 1, start + count), _END_OF_CHAIN])
        return start

    directory_start, mini_fat_start, entries[0].start = (chain(count) for count in counts)
    entries[0].size = len(mini_stream)
    for entry, count in zip(large, large_sectors, strict=True):
        entry.start = chain(count)
    fat += [_FREE_SECTOR] * (fat_sectors * 128 - len(fat))
    return _Allocation(
        fat, fat_sectors, directory_start, mini_fat, mini_fat_start, bytes(mini_stream), large
    )


def compound_file(children: Sequence[CfbNode], clsid: bytes = bytes(16)) -> bytes:
    """A version 3 compound file (512-byte sectors) holding `children` under its root: the
    header, then the FAT, the directory, the mini FAT, the mini stream and the large streams."""
    entries = _directory(children, clsid)
    layout = _allocate(entries)
    header = bytearray(bytes.fromhex("D0CF11E0A1B11AE1") + bytes(16))
    header += struct.pack("<HHHHH", 0x003E, 0x0003, 0xFFFE, 9, 6) + bytes(6)
    header += struct.pack(
        "<IIIIIIIII",
        0,
        layout.fat_sectors,
        layout.directory_start,
        0,
        _MINI_CUTOFF,
        layout.mini_fat_start,
        -(-len(layout.mini_fat) // 128),
        _END_OF_CHAIN,
        0,
    )
    difat = list(range(layout.fat_sectors)) + [_FREE_SECTOR] * (109 - layout.fat_sectors)
    header += struct.pack("<109I", *difat)
    sectors = bytearray(struct.pack(f"<{len(layout.fat)}I", *layout.fat))
    directory = bytearray()
    for entry in entries:
        name = (entry.name + "\x00").encode("utf-16-le")
        directory += name + bytes(64 - len(name))
        directory += struct.pack(
            "<HBBIII",
            len(name),
            entry.kind,
            1 if entry.black else 0,
            entry.left,
            entry.right,
            entry.child,
        )
        directory += entry.clsid + struct.pack("<IQQIQ", 0, 0, 0, entry.start, entry.size)
    unused = bytes(64) + struct.pack("<HBBIII", 0, 0, 0, _NO_STREAM, _NO_STREAM, _NO_STREAM)
    directory += (unused + bytes(48)) * (-len(entries) % 4)
    mini_fat = struct.pack(f"<{len(layout.mini_fat)}I", *layout.mini_fat)
    sectors += directory + mini_fat + b"\xff" * (-len(mini_fat) % 512)
    sectors += layout.mini_stream + bytes(-len(layout.mini_stream) % 512)
    for entry in layout.large:
        sectors += entry.data + bytes(-len(entry.data) % 512)
    return bytes(header) + bytes(sectors)


# =============================================================================================
# VBA projects ([MS-OVBA]) and OLE package objects
# =============================================================================================


def ovba_compress(data: bytes) -> bytes:
    """A CompressedContainer of one chunk made of literal tokens only ([MS-OVBA] 2.4.1)."""
    if len(data) > 3640:
        raise ValueError("a literal-only chunk holds at most 3,640 bytes")
    body = bytearray()
    for start in range(0, len(data), 8):
        body.append(0)  # flag byte: the next eight tokens are literals
        body += data[start : start + 8]
    return b"\x01" + struct.pack("<H", (len(body) - 1) | (0b011 << 12) | (1 << 15)) + body


def ovba_encrypt(data: bytes, project_id: str, seed: int) -> str:
    """Data Encryption of [MS-OVBA] 2.4.3.2, used by the `CMG`, `DPB` and `GC` lines."""
    project_key = sum(project_id.encode("ascii")) & 0xFF
    out = [seed, seed ^ 2, seed ^ project_key]
    unencrypted, encrypted_1, encrypted_2 = project_key, out[2], out[1]
    ignored = (seed & 6) // 2
    for byte in bytes(ignored) + struct.pack("<I", len(data)) + data:
        value = byte ^ ((encrypted_2 + unencrypted) & 0xFF)
        out.append(value)
        encrypted_2, encrypted_1, unencrypted = encrypted_1, value, byte
    return bytes(out).hex().upper()


@dataclass(frozen=True)
class VbaModule:
    name: str
    document: bool  # a document module (ThisDocument) rather than a standard module
    code: str


def _record(identifier: int, data: bytes) -> bytes:
    return struct.pack("<HI", identifier, len(data)) + data


def _record_pair(identifier: int, ansi: str, reserved: int) -> bytes:
    unicode = ansi.encode("utf-16-le")
    return (
        _record(identifier, ansi.encode("cp1252"))
        + struct.pack("<HI", reserved, len(unicode))
        + unicode
    )


def vba_project(modules: Sequence[VbaModule]) -> bytes:
    """A `vbaProject.bin` with no performance cache, which every VBA host recompiles on load."""
    project_id = "{5B7E3F1A-2C4D-4E6F-8A9B-0C1D2E3F4A5B}"
    directory = bytearray()
    directory += _record(0x0001, struct.pack("<I", 1))  # PROJECTSYSKIND: 32-bit Windows
    directory += _record(0x0002, struct.pack("<I", 0x0409))  # PROJECTLCID
    directory += _record(0x0014, struct.pack("<I", 0x0409))  # PROJECTLCIDINVOKE
    directory += _record(0x0003, struct.pack("<H", 1252))  # PROJECTCODEPAGE
    directory += _record(0x0004, b"Project")  # PROJECTNAME
    directory += _record_pair(0x0005, "", 0x0040)  # PROJECTDOCSTRING
    directory += _record_pair(0x0006, "", 0x003D)  # PROJECTHELPFILEPATH: two empty paths
    directory += _record(0x0007, struct.pack("<I", 0))  # PROJECTHELPCONTEXT
    directory += _record(0x0008, struct.pack("<I", 0))  # PROJECTLIBFLAGS
    directory += struct.pack("<HIIH", 0x0009, 4, 0x6D6E9C4A, 0x0011)  # PROJECTVERSION
    directory += _record_pair(0x000C, "", 0x003C)  # PROJECTCONSTANTS
    directory += _record(0x000F, struct.pack("<H", len(modules)))  # PROJECTMODULES
    directory += _record(0x0013, struct.pack("<H", 0xFFFF))  # PROJECTCOOKIE
    storage = [CfbNode("_VBA_PROJECT", bytes.fromhex("CC61FFFF000000"))]
    for module in modules:
        directory += _record(0x0019, module.name.encode("cp1252"))  # MODULENAME
        directory += _record(0x0047, module.name.encode("utf-16-le"))  # MODULENAMEUNICODE
        directory += _record_pair(0x001A, module.name, 0x0032)  # MODULESTREAMNAME
        directory += _record_pair(0x001C, "", 0x0048)  # MODULEDOCSTRING
        directory += _record(0x0031, struct.pack("<I", 0))  # MODULEOFFSET: no cache precedes
        directory += _record(0x001E, struct.pack("<I", 0))  # MODULEHELPCONTEXT
        directory += _record(0x002C, struct.pack("<H", 0xFFFF))  # MODULECOOKIE
        directory += struct.pack("<HI", 0x0022 if module.document else 0x0021, 0)  # MODULETYPE
        directory += struct.pack("<HI", 0x002B, 0)  # module terminator
        source = module.code.replace("\n", "\r\n").encode("cp1252")
        storage.append(CfbNode(module.name, ovba_compress(source)))
    directory += struct.pack("<HI", 0x0010, 0)  # dir stream terminator
    storage.append(CfbNode("dir", ovba_compress(bytes(directory))))
    lines = [f'ID="{project_id}"']
    lines += [
        f"Document={module.name}/&H00000000" if module.document else f"Module={module.name}"
        for module in modules
    ]
    # Unprotected (CMG), no password (DPB) and visible (GC), each encrypted with a fixed seed.
    protection = ovba_encrypt(bytes(4), project_id, 0x31)
    password = ovba_encrypt(bytes(1), project_id, 0x45)
    visibility = ovba_encrypt(b"\xff", project_id, 0x57)
    lines += [
        'Name="Project"',
        'HelpContextID="0"',
        'VersionCompatible32="393222000"',
        f'CMG="{protection}"',
        f'DPB="{password}"',
        f'GC="{visibility}"',
        "",
        "[Host Extender Info]",
        "&H00000001={3832D640-CF90-11CF-8E43-00A0C911005A};VBE;&H00000000",
        "",
        "[Workspace]",
    ]
    lines += [f"{module.name}=0, 0, 0, 0, C" for module in modules]
    project_stream = ("\r\n".join(lines) + "\r\n").encode("cp1252")
    names = (
        b"".join(
            module.name.encode("cp1252") + b"\x00" + module.name.encode("utf-16-le") + b"\x00\x00"
            for module in modules
        )
        + b"\x00\x00"
    )
    return compound_file(
        [
            CfbNode("VBA", children=storage),
            CfbNode("PROJECT", project_stream),
            CfbNode("PROJECTwm", names),
        ]
    )


def word_macro_project(marker: str) -> bytes:
    """A Word project whose `AutoOpen` macro writes `marker` (Word runs it on opening)."""
    this_document = "\n".join(
        [
            'Attribute VB_Name = "ThisDocument"',
            'Attribute VB_Base = "1Normal.ThisDocument"',
            "Attribute VB_GlobalNameSpace = False",
            "Attribute VB_Creatable = False",
            "Attribute VB_PredeclaredId = True",
            "Attribute VB_Exposed = True",
            "Attribute VB_TemplateDerived = True",
            "Attribute VB_Customizable = True",
            "",
        ]
    )
    return vba_project(
        [
            VbaModule("ThisDocument", True, this_document),
            VbaModule("Module1", False, _marker_macro("AutoOpen", marker)),
        ]
    )


def presentation_macro_project(marker: str) -> bytes:
    """A PowerPoint project whose `Auto_Open` macro writes `marker`."""
    return vba_project([VbaModule("Module1", False, _marker_macro("Auto_Open", marker))])


def benign_macro_project() -> bytes:
    """A project with one macro that runs only when a reader starts it."""
    code = "\n".join(
        [
            'Attribute VB_Name = "Module1"',
            "Sub FixtureGreeting()",
            '    MsgBox "DocChat fixture macro"',
            "End Sub",
            "",
        ]
    )
    return vba_project([VbaModule("Module1", False, code)])


def _marker_macro(name: str, marker: str) -> str:
    return "\n".join(
        [
            'Attribute VB_Name = "Module1"',
            f"Sub {name}()",
            "    Dim handle As Integer",
            "    handle = FreeFile",
            f'    Open "{marker}" For Output As #handle',
            f'    Print #handle, "{name} ran"',
            "    Close #handle",
            "End Sub",
            "",
        ]
    )


PACKAGER_CLSID: Final = uuid.UUID("0003000C-0000-0000-C000-000000000046").bytes_le


def ole_package(label: str, payload: bytes) -> bytes:
    """An OLE Packager object (ProgID `Package`) carrying one file, as `oleObject1.bin`."""
    original = f"C:\\Users\\fixture\\Documents\\{label}"
    temporary = f"C:\\Users\\fixture\\AppData\\Local\\Temp\\{label}"
    body = (
        struct.pack("<H", 2)
        + label.encode()
        + b"\x00"
        + original.encode()
        + b"\x00"
        + struct.pack("<HHI", 0, 3, len(temporary) + 1)
        + temporary.encode()
        + b"\x00"
        + struct.pack("<I", len(payload))
        + payload
    )
    for text in (temporary, label, original):
        body += struct.pack("<I", len(text)) + text.encode("utf-16-le")
    native = struct.pack("<I", len(body)) + body
    user_type = b"OLE Package\x00"
    prog_id = b"Package\x00"
    # CompObjStream ([MS-OLEDS] 2.3.8): header, user type, no clipboard format, program
    # identifier, then the Unicode marker and three empty Unicode strings.
    comp_obj = (
        struct.pack("<II", 0xFFFE0001, 0x00000A03)
        + b"\xff\xff\xff\xff"
        + PACKAGER_CLSID
        + struct.pack("<I", len(user_type))
        + user_type
        + struct.pack("<I", 0)
        + struct.pack("<I", len(prog_id))
        + prog_id
        + struct.pack("<IIII", 0x71B239F4, 0, 0, 0)
    )
    return compound_file(
        [CfbNode("\x01Ole10Native", native), CfbNode("\x01CompObj", comp_obj)], PACKAGER_CLSID
    )


# =============================================================================================
# A small workbook, the embedded `.xlsx` of an OLE object or a chart
# =============================================================================================

_SPREADSHEET_NS: Final = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_RELATIONSHIPS_NS: Final = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_RELS_NS: Final = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES_NS: Final = "http://schemas.openxmlformats.org/package/2006/content-types"
_RELS_TYPE: Final = "application/vnd.openxmlformats-package.relationships+xml"
_XML_HEAD: Final = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def xlsx(rows: Sequence[Sequence[str | float]]) -> bytes:
    """A one-sheet workbook (`Sheet1`) with inline strings and numbers from cell A1."""
    cells = []
    for row_index, row in enumerate(rows, start=1):
        row_cells = []
        for column_index, value in enumerate(row):
            reference = f"{chr(ord('A') + column_index)}{row_index}"
            if isinstance(value, str):
                row_cells.append(
                    f'<c r="{reference}" t="inlineStr"><is><t>{_escape(value)}</t></is></c>'
                )
            else:
                row_cells.append(f'<c r="{reference}"><v>{value:g}</v></c>')
        cells.append(f'<row r="{row_index}">{"".join(row_cells)}</row>')
    sheet = (
        f'{_XML_HEAD}<worksheet xmlns="{_SPREADSHEET_NS}"><sheetData>{"".join(cells)}'
        "</sheetData></worksheet>"
    )
    workbook = (
        f'{_XML_HEAD}<workbook xmlns="{_SPREADSHEET_NS}" xmlns:r="{_RELATIONSHIPS_NS}">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    styles = (
        f'{_XML_HEAD}<styleSheet xmlns="{_SPREADSHEET_NS}"><fonts count="1"><font>'
        '<sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill>'
        '<patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/>'
        '</fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/>'
        '</border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" '
        'borderId="0"/></cellStyleXfs><cellXfs count="1"><xf numFmtId="0" fontId="0" '
        'fillId="0" borderId="0" xfId="0"/></cellXfs><cellStyles count="1"><cellStyle '
        'name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    )
    sheet_types = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    overrides = "".join(
        f'<Override PartName="/xl/{part}" ContentType="{sheet_types}.{kind}+xml"/>'
        for part, kind in (
            ("workbook.xml", "sheet.main"),
            ("worksheets/sheet1.xml", "worksheet"),
            ("styles.xml", "styles"),
        )
    )
    content_types = (
        f'{_XML_HEAD}<Types xmlns="{_CONTENT_TYPES_NS}"><Default Extension="rels" '
        f'ContentType="{_RELS_TYPE}"/><Default Extension="xml" ContentType="application/xml"/>'
        f"{overrides}</Types>"
    )
    entries = [
        ZipEntry("[Content_Types].xml", content_types.encode()),
        ZipEntry("_rels/.rels", relationships(("rId1", "officeDocument", "xl/workbook.xml"))),
        ZipEntry("xl/workbook.xml", workbook.encode()),
        ZipEntry(
            "xl/_rels/workbook.xml.rels",
            relationships(
                ("rId1", "worksheet", "worksheets/sheet1.xml"), ("rId2", "styles", "styles.xml")
            ),
        ),
        ZipEntry("xl/styles.xml", styles.encode()),
        ZipEntry("xl/worksheets/sheet1.xml", sheet.encode()),
    ]
    return write_zip(entries)


def relationships(*items: tuple[str, str, str], external: bool = False) -> bytes:
    """A relationships part; each item is an identifier, a relationship type (a full URI, or
    the last segment of an officeDocument relationship type) and a target."""
    mode = ' TargetMode="External"' if external else ""
    body = "".join(
        f'<Relationship Id="{rel_id}" '
        f'Type="{kind if "/" in kind else f"{_RELATIONSHIPS_NS}/{kind}"}" '
        f'Target="{_escape(target)}"{mode}/>'
        for rel_id, kind, target in items
    )
    return f'{_XML_HEAD}<Relationships xmlns="{_PACKAGE_RELS_NS}">{body}</Relationships>'.encode()


# =============================================================================================
# Office packages: python-docx and python-pptx build the parts, this script writes the archive
# =============================================================================================

REL: Final = _RELATIONSHIPS_NS
RT_HYPERLINK: Final = f"{REL}/hyperlink"
RT_IMAGE: Final = f"{REL}/image"
RT_OLE_OBJECT: Final = f"{REL}/oleObject"
RT_PACKAGE: Final = f"{REL}/package"
RT_SUBDOCUMENT: Final = f"{REL}/subDocument"
RT_CUSTOM_XML: Final = f"{REL}/customXml"
RT_PRINTER_SETTINGS: Final = f"{REL}/printerSettings"
RT_STYLES_WITH_EFFECTS: Final = (
    "http://schemas.microsoft.com/office/2007/relationships/stylesWithEffects"
)
RT_VBA_PROJECT: Final = "http://schemas.microsoft.com/office/2006/relationships/vbaProject"

CT_WORD_MAIN: Final = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
CT_WORD_TEMPLATE: Final = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"
)
CT_WORD_MACRO: Final = "application/vnd.ms-word.document.macroEnabled.main+xml"
CT_SLIDES_MAIN: Final = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
)
CT_SLIDES_TEMPLATE: Final = (
    "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml"
)
CT_SLIDES_SHOW: Final = (
    "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml"
)
CT_SLIDES_MACRO: Final = "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"
CT_VBA_PROJECT: Final = "application/vnd.ms-office.vbaProject"
CT_XLSX: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CT_XLSM: Final = "application/vnd.ms-excel.sheet.macroEnabled.12"
CT_OLE_OBJECT: Final = "application/vnd.openxmlformats-officedocument.oleObject"

_APP_PROPERTIES: Final = (
    f'{_XML_HEAD}<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
    'extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/'
    f'docPropsVTypes"><Application>{AUTHOR}</Application></Properties>'
).encode()


def find_entry(entries: Sequence[ZipEntry], name: str) -> ZipEntry:
    for entry in entries:
        if entry.name == name:
            return entry
    raise KeyError(name)


def replace_once(data: bytes, old: bytes, new: bytes) -> bytes:
    if data.count(old) != 1:
        raise ValueError(f"expected exactly one {old!r}")
    return data.replace(old, new)


def edit_entry(entries: Sequence[ZipEntry], name: str, transform: Callable[[bytes], bytes]) -> None:
    entry = find_entry(entries, name)
    entry.data = transform(entry.data)


def insert_before(entries: Sequence[ZipEntry], name: str, closing: str, fragment: str) -> None:
    """Insert `fragment` before the one `closing` tag of an XML part."""
    edit_entry(
        entries,
        name,
        lambda data: replace_once(data, closing.encode(), fragment.encode() + closing.encode()),
    )


def office_package(saved: bytes) -> list[ZipEntry]:
    """The entries of a saved package, without the template's thumbnail and with neutral
    extended properties (the templates name the application and machine that made them)."""
    entries = [entry for entry in read_zip(saved) if entry.name != "docProps/thumbnail.jpeg"]
    edit_entry(
        entries,
        "_rels/.rels",
        lambda data: re.sub(rb'<Relationship [^>]*Target="docProps/thumbnail.jpeg"/>', b"", data),
    )
    if not any(entry.name.endswith(".jpeg") for entry in entries):
        edit_entry(
            entries,
            "[Content_Types].xml",
            lambda data: data.replace(b'<Default Extension="jpeg" ContentType="image/jpeg"/>', b""),
        )
    find_entry(entries, "docProps/app.xml").data = _APP_PROPERTIES
    return entries


def add_package_relationship(
    entries: Sequence[ZipEntry],
    rels_name: str,
    rel_id: str,
    rel_type: str,
    target: str,
    *,
    external: bool = False,
) -> None:
    mode = ' TargetMode="External"' if external else ""
    insert_before(
        entries,
        rels_name,
        "</Relationships>",
        f'<Relationship Id="{rel_id}" Type="{rel_type}" Target="{_escape(target)}"{mode}/>',
    )


def set_main_content_type(entries: Sequence[ZipEntry], old: str, new: str) -> None:
    edit_entry(
        entries,
        "[Content_Types].xml",
        lambda data: replace_once(
            data, f'ContentType="{old}"'.encode(), f'ContentType="{new}"'.encode()
        ),
    )


# --- Word -------------------------------------------------------------------------------------

_WORD_NS: Final = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    f'xmlns:r="{REL}" xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:o="urn:schemas-microsoft-com:office:office" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)
# The styles the fixture documents use, kept from python-docx's template; the rest of the
# template's 164 styles and its 137 latent-style entries only add bytes.
_KEPT_STYLES: Final = frozenset(
    {
        "Normal",
        "DefaultParagraphFont",
        "TableNormal",
        "NoList",
        "Title",
        "TitleChar",
        "Subtitle",
        "SubtitleChar",
        "Heading1",
        "Heading2",
        "Heading3",
        "Heading1Char",
        "Heading2Char",
        "Heading3Char",
        "Header",
        "HeaderChar",
        "Footer",
        "FooterChar",
        "TableGrid",
        "ListBullet",
        "ListParagraph",
        "Caption",
    }
)
_HYPERLINK_STYLE: Final = (
    f'<w:style {_WORD_NS} w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/>'
    '<w:basedOn w:val="DefaultParagraphFont"/><w:uiPriority w:val="99"/><w:unhideWhenUsed/>'
    '<w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr></w:style>'
)
# The picture frame shape type that Word declares once for VML pictures and OLE objects.
_VML_PICTURE_FRAME: Final = (
    '<v:shapetype id="_x0000_t75" coordsize="21600,21600" o:spt="75" o:preferrelative="t" '
    'path="m@4@5l@4@11@9@11@9@5xe" filled="f" stroked="f"><v:stroke joinstyle="miter"/>'
    '<v:formulas><v:f eqn="if lineDrawn pixelLineWidth 0"/><v:f eqn="sum @0 1 0"/>'
    '<v:f eqn="sum 0 0 @1"/><v:f eqn="prod @2 1 2"/><v:f eqn="prod @3 21600 pixelWidth"/>'
    '<v:f eqn="prod @3 21600 pixelHeight"/><v:f eqn="sum @0 0 1"/><v:f eqn="prod @6 1 2"/>'
    '<v:f eqn="prod @7 21600 pixelWidth"/><v:f eqn="sum @8 21600 0"/>'
    '<v:f eqn="prod @7 21600 pixelHeight"/><v:f eqn="sum @10 21600 0"/></v:formulas>'
    '<v:path o:extrusionok="f" gradientshapeok="t" o:connecttype="rect"/>'
    '<o:lock v:ext="edit" aspectratio="t"/></v:shapetype>'
)


def new_document(title: str) -> WordDocument:
    document = docx.Document()
    part = document.part
    for rel_id, rel in list(part.rels.items()):
        if rel.reltype in (RT_STYLES_WITH_EFFECTS, RT_CUSTOM_XML):
            part.drop_rel(rel_id)
    styles = document.styles.element
    for child in list(styles):
        if child.tag == qn("w:latentStyles") or (
            child.tag == qn("w:style") and child.get(qn("w:styleId")) not in _KEPT_STYLES
        ):
            styles.remove(child)
    styles.append(parse_word_xml(_HYPERLINK_STYLE))
    properties = document.core_properties
    properties.author = AUTHOR
    properties.last_modified_by = AUTHOR
    properties.title = title
    properties.comments = ""
    properties.created = FIXED_TIME
    properties.modified = FIXED_TIME
    properties.revision = 1
    return document


def word_entries(document: WordDocument) -> list[ZipEntry]:
    buffer = io.BytesIO()
    document.save(buffer)
    return office_package(buffer.getvalue())


def append_word_xml(paragraph: Paragraph, fragment: str) -> None:
    """Append the elements of a WordprocessingML fragment to a paragraph."""
    wrapper = parse_word_xml(f"<w:p {_WORD_NS}>{fragment}</w:p>")
    for child in list(wrapper):
        paragraph._p.append(child)


def word_run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{_escape(text)}</w:t></w:r>'


def word_field(instruction: str, result: str) -> str:
    """A complex field: its instruction and the result stored in the file."""
    return (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve"> {_escape(instruction)} </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        f'{result}<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )


def word_hyperlink(paragraph: Paragraph, url: str, text: str) -> None:
    rel_id = paragraph.part.relate_to(url, RT_HYPERLINK, is_external=True)
    append_word_xml(
        paragraph,
        f'<w:hyperlink r:id="{rel_id}" w:history="1"><w:r><w:rPr>'
        f'<w:rStyle w:val="Hyperlink"/></w:rPr><w:t xml:space="preserve">'
        f"{_escape(text)}</w:t></w:r></w:hyperlink>",
    )


def word_picture(
    rel_id: str, picture_id: int, name: str, size: tuple[int, int], *, linked: bool = False
) -> str:
    """An inline DrawingML picture run; `linked` names the image by `r:link` instead of
    embedding it."""
    cx, cy = size
    blip = f'r:link="{rel_id}"' if linked else f'r:embed="{rel_id}"'
    label = f'id="{picture_id}" name="{_escape(name)}"'
    return (
        f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr {label}/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f"<pic:pic><pic:nvPicPr><pic:cNvPr {label}/><pic:cNvPicPr/>"
        f"</pic:nvPicPr><pic:blipFill><a:blip {blip}/><a:stretch><a:fillRect/></a:stretch>"
        f'</pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/>'
        '</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"
    )


def word_image(document: WordDocument, png_bytes: bytes) -> str:
    rel_id, _ = document.part.get_or_add_image(io.BytesIO(png_bytes))
    return rel_id


def word_ole_object(
    image_rel_id: str,
    object_rel_id: str,
    *,
    prog_id: str,
    shape: int,
    linked: bool,
    size_pt: tuple[int, int] = (144, 72),
    icon: bool = False,
    declare_frame: bool = False,
) -> str:
    """A VML OLE object run: its preview picture and the `o:OLEObject` that embeds or links it.
    The first VML object of a document declares the picture frame shape type."""
    width, height = size_pt
    frame = _VML_PICTURE_FRAME if declare_frame else ""
    shape_id = f"_x0000_i10{shape:02d}"
    if linked:
        kind = 'Type="Link"'
        tail = ' UpdateMode="Always"><o:LinkType>EnhancedMetaFile</o:LinkType>'
    else:
        kind = f'Type="Embed" ObjectID="_17000000{shape:02d}"'
        tail = ">"
    aspect = "Icon" if icon else "Content"
    return (
        f'<w:r><w:object w:dxaOrig="{width * 20}" w:dyaOrig="{height * 20}">{frame}'
        f'<v:shape id="{shape_id}" type="#_x0000_t75" style="width:{width}pt;height:{height}pt" '
        f'o:ole=""><v:imagedata r:id="{image_rel_id}" o:title=""/></v:shape>'
        f'<o:OLEObject {kind} ProgID="{prog_id}" ShapeID="{shape_id}" DrawAspect="{aspect}" '
        f'r:id="{object_rel_id}"{tail}</o:OLEObject></w:object></w:r>'
    )


def word_embedded_part(
    document: WordDocument, part_name: str, content_type: str, blob: bytes, rel_type: str
) -> str:
    part = WordPart(WordPackURI(part_name), content_type, blob, document.part.package)
    return document.part.relate_to(part, rel_type)


# --- PowerPoint -------------------------------------------------------------------------------

_SLIDE_NS: Final = (
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    f'xmlns:r="{REL}" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
)


def new_deck(title: str) -> SlideDeck:
    deck = pptx.Presentation()
    part = deck.part
    for rel_id, rel in list(part.rels.items()):
        if rel.reltype == RT_PRINTER_SETTINGS:
            part.drop_rel(rel_id)
    properties = deck.core_properties
    properties.author = AUTHOR
    properties.last_modified_by = AUTHOR
    properties.title = title
    properties.comments = ""
    properties.created = FIXED_TIME
    properties.modified = FIXED_TIME
    properties.revision = 1
    return deck


def deck_entries(deck: SlideDeck) -> list[ZipEntry]:
    buffer = io.BytesIO()
    deck.save(buffer)
    return office_package(buffer.getvalue())


def titled_slide(deck: SlideDeck, layout: int, title: str, notes: str | None = None) -> Slide:
    """A slide of one of the template's layouts (0 title, 1 title and content, 5 title only)
    with its title set and, when given, speaker notes."""
    slide: Slide = deck.slides.add_slide(deck.slide_layouts[layout])
    title_shape = slide.shapes.title
    if title_shape is None:
        raise ValueError(f"layout {layout} has no title placeholder")
    title_shape.text = title
    if notes is not None:
        notes_frame = slide.notes_slide.notes_text_frame
        if notes_frame is None:
            raise ValueError("the notes master has no body placeholder")
        notes_frame.text = notes
    return slide


def bullet_slide(
    deck: SlideDeck, title: str, bullets: Sequence[str], notes: str | None = None
) -> Slide:
    slide = titled_slide(deck, 1, title, notes)
    frame = slide.placeholders[1].text_frame
    frame.text = bullets[0]
    for bullet in bullets[1:]:
        frame.add_paragraph().text = bullet
    return slide


def table_slide(
    deck: SlideDeck, title: str, rows: Sequence[Sequence[str]], notes: str | None = None
) -> Slide:
    slide = titled_slide(deck, 5, title, notes)
    shape = slide.shapes.add_table(
        len(rows),
        len(rows[0]),
        SlideInches(0.6),
        SlideInches(1.8),
        SlideInches(8.8),
        SlideInches(0.45 * len(rows)),
    )
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            shape.table.cell(row_index, column_index).text = value
    return slide


def append_slide_xml(slide: Slide, fragment: str) -> None:
    wrapper = parse_slide_xml(f"<p:spTree {_SLIDE_NS}>{fragment}</p:spTree>")
    tree = slide.shapes._spTree
    for child in list(wrapper):
        tree.append(child)


def slide_picture(
    rel_id: str, shape_id: int, name: str, box: tuple[int, int, int, int], *, linked: bool = False
) -> str:
    x, y, cx, cy = box
    blip = f'r:link="{rel_id}"' if linked else f'r:embed="{rel_id}"'
    return (
        f'<p:pic><p:nvPicPr><p:cNvPr id="{shape_id}" name="{_escape(name)}"/><p:cNvPicPr>'
        '<a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr><p:blipFill>'
        f"<a:blip {blip}/><a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr><a:xfrm>"
        f'<a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect">'
        "<a:avLst/></a:prstGeom></p:spPr></p:pic>"
    )


# =============================================================================================
# The shared Word document and deck (84-testing.md section 1.5)
# =============================================================================================


def text_blocks(text: str) -> list[str]:
    """The blocks of a text, separated by blank lines, each with its lines joined by spaces."""
    return [" ".join(block.split()) for block in text.strip().split("\n\n") if block.strip()]


# headings.docx: three heading levels, 18 headed sections and two tables. A block that starts
# with one to three "#" is a heading of that level, "[table <name>]" places a table, and every
# other block is a paragraph.
_HANDBOOK: Final = """
# 1 Getting started

Thank you for volunteering at Orchard Lane Community Library. This handbook explains how the
library works, what volunteers do at each desk and what to do if something goes wrong. Keep it
handy during your first month, and ask the duty librarian whenever something is not covered here.

## 1.1 About Orchard Lane Library

Orchard Lane opened in 1962 and moved to its current building on Quince Street in 2011. It holds
about 64,000 books, films and games, runs 14 public computers and is open six days a week. Around
90 volunteers give an average of three hours a week each.

The library is run by a board of seven trustees elected by members at the annual meeting in May.
Day-to-day decisions are made by the library manager, Rosalind Achebe, who also coordinates the
volunteer programme.

## 1.2 Your first week

Your first week combines a short orientation with shadow shifts at the circulation and
information desks. The table below shows a typical first week; the volunteer coordinator confirms
your own times by email.

[table first-week]

### 1.2.1 Orientation session

The orientation session lasts ninety minutes. It covers the building layout, the emergency exits,
the library management system and the code of conduct for volunteers. You receive your volunteer
badge and a locker key at the end of the session.

### 1.2.2 Building access

Volunteers enter through the staff door on Pear Lane using the code given at orientation. The
code changes on the first Monday of each month. Never share it, and never hold the door open for
anyone you do not recognise.

# 2 Working at the desks

Most volunteer shifts are spent at one of the two public desks. Each desk has a printed
quick-reference card, and the sections below describe the tasks in more detail.

## 2.1 Circulation desk

The circulation desk on the ground floor handles loans, returns, holds and new memberships. Two
people staff it at busy times, usually a librarian and a volunteer.

### 2.1.1 Checking items out

Scan the member's card first, then each item. The system warns you if a member owes more than 10
in fines or has reached the limit of 25 items. Explain any warning politely and refer disputes to
the librarian on duty.

### 2.1.2 Returns and holds

Returned items go through the scanner and onto the sorting trolley. If the screen shows a hold,
place the item on the holds shelf with a slip showing the member's surname and the date the hold
expires, which is seven days later.

## 2.2 Information desk

The information desk on the first floor helps people find material, use the catalogue and get
started on the public computers. Volunteers at this desk answer simple questions and pass harder
ones to a librarian.

### 2.2.1 Answering questions

Start by asking what the person wants to achieve rather than what they asked for. Show them how to
search the catalogue themselves when they have time, and write call numbers on the notepads
provided rather than on scrap paper.

### 2.2.2 Computer help

Public computer sessions last one hour and can be extended once if nobody is waiting. Volunteers
may help people log in, print and save files, but never type a person's passwords or personal
details for them.

# 3 Programs and safety

Volunteers also support the library's programmes for children and families, and every volunteer
shares responsibility for the safety of visitors and colleagues.

## 3.1 Children's programs

Children's programmes run on weekday afternoons and Saturday mornings. Volunteers who work with
children hold a current background check, which the library arranges and pays for.

### 3.1.1 Story time

Story time runs three times a week in the children's corner. The schedule below lists each
session; volunteers set out cushions, greet families and tidy up afterwards.

[table story-time]

### 3.1.2 Homework club

Homework club meets on Mondays and Thursdays from 15:30 to 17:00 for pupils aged 8 to 14.
Volunteers help pupils find information and stay focused, but do not do the homework for them.

## 3.2 Safety procedures

Report any accident, however small, to the duty librarian and record it in the incident book
behind the circulation desk before the end of your shift.

### 3.2.1 Fire evacuation

When the alarm sounds, stop what you are doing and guide visitors to the nearest exit. Do not use
the lift. The assembly point is the car park on Pear Lane, where the duty librarian checks the
volunteer sign-in sheet against the people present.
"""
_HANDBOOK_TABLES: Final = {
    "first-week": [
        ["Day", "Activity", "Location", "Contact"],
        ["Monday", "Orientation session", "Meeting room 2", "Volunteer coordinator"],
        ["Tuesday", "Shadow shift at circulation", "Ground floor desk", "Duty librarian"],
        ["Thursday", "Shadow shift at information", "First floor desk", "Duty librarian"],
        ["Saturday", "First solo shift", "Your chosen desk", "Shift lead"],
    ],
    "story-time": [
        ["Session", "Day", "Time", "Age group"],
        ["Baby rhymes", "Tuesday", "10:00", "Under 2"],
        ["Toddler tales", "Wednesday", "10:30", "2 to 4"],
        ["Saturday stories", "Saturday", "11:00", "4 to 7"],
    ],
}


def build_headings_docx() -> bytes:
    document = new_document("Orchard Lane Community Library volunteer handbook")
    document.add_paragraph("Orchard Lane Community Library", style="Title")
    document.add_paragraph("Volunteer handbook, 2026 edition", style="Subtitle")
    first_part = True
    for block in text_blocks(_HANDBOOK):
        if block.startswith("#"):
            marks, _, heading = block.partition(" ")
            paragraph = document.add_heading(heading, len(marks))
            if len(marks) == 1:
                # Each part starts a page, so the converted PDF's pages follow the outline.
                paragraph.paragraph_format.page_break_before = not first_part
                first_part = False
        elif block.startswith("[table "):
            rows = _HANDBOOK_TABLES[block.removeprefix("[table ").removesuffix("]")]
            grid = document.add_table(rows=len(rows), cols=len(rows[0]))
            grid.style = "Table Grid"
            for row_index, row in enumerate(rows):
                for column_index, value in enumerate(row):
                    grid.cell(row_index, column_index).text = value
            for cell in grid.rows[0].cells:
                cell.paragraphs[0].runs[0].bold = True
        else:
            document.add_paragraph(block)
    return write_zip(word_entries(document))


# slides.pptx: ten slides, tables on slides 4 and 7, speaker notes on slides 2, 4 and 9.
def build_slides_pptx() -> bytes:
    deck = new_deck("Blue Heron Greenhouse Cooperative: 2026 season review")
    title = titled_slide(deck, 0, "Blue Heron Greenhouse Cooperative")
    title.placeholders[1].text = "2026 season review, members' meeting of 14 November 2026"
    bullet_slide(
        deck,
        "Season at a glance",
        [
            "Harvest of 18.4 tonnes, up 12 percent on 2025",
            "Sales of 96,300 across three channels",
            "4,150 volunteer hours from 63 members",
            "Water use cut by 21 percent with drip lines",
        ],
        notes="Open with thanks to the volunteers. The harvest figure includes the October glut "
        "of squash, which went to the food bank.",
    )
    bullet_slide(
        deck,
        "What we grew",
        [
            "Tomatoes and peppers in the two heated houses",
            "Salad leaves all year in the cold frames",
            "Squash, beans and courgettes in the outdoor beds",
            "New this year: ginger and turmeric trials",
        ],
    )
    table_slide(
        deck,
        "Harvest by crop",
        [
            ["Crop", "Beds", "Harvest (kg)", "Change on 2025"],
            ["Tomatoes", "24", "6,200", "+15%"],
            ["Salad leaves", "18", "2,750", "+4%"],
            ["Peppers", "10", "1,980", "+22%"],
            ["Squash", "12", "4,100", "+9%"],
            ["Beans", "8", "1,640", "-3%"],
            ["Courgettes", "6", "1,730", "+18%"],
        ],
        notes="Peppers did best thanks to the new shade screens. Beans fell slightly after the "
        "cold, wet June.",
    )
    bullet_slide(
        deck,
        "Water and energy",
        [
            "Drip irrigation in all heated houses since March",
            "Rainwater tanks supplied 64 percent of our water",
            "A heat pump replaced the gas boiler in house 2",
            "Energy use down 17 percent per kilogram harvested",
        ],
    )
    bullet_slide(
        deck,
        "Volunteers",
        [
            "63 active members, 11 of them new this year",
            "Saturday work parties averaged 14 people",
            "Two school groups visited every month",
            "Training days on pruning, composting and safety",
        ],
    )
    table_slide(
        deck,
        "Sales by month, June to October",
        [
            ["Month", "Farmers market", "Veg boxes", "Restaurants"],
            ["June", "3,200", "4,100", "1,900"],
            ["July", "5,600", "4,300", "2,700"],
            ["August", "6,900", "4,400", "3,100"],
            ["September", "6,100", "4,400", "2,800"],
            ["October", "4,800", "4,200", "2,300"],
        ],
    )
    bullet_slide(
        deck,
        "Pest management",
        [
            "Biological control for whitefly in both heated houses",
            "Netting kept cabbage white butterflies off the brassicas",
            "Slug traps replaced pellets in the outdoor beds",
            "No chemical sprays for the second year running",
        ],
    )
    bullet_slide(
        deck,
        "Plans for 2027",
        [
            "Add a third heated house for winter salads",
            "Extend the veg box scheme to 120 households",
            "Install soil moisture sensors in the outdoor beds",
            "Start a seed-saving group for members",
        ],
        notes="Ask members to vote on the third heated house at the spring meeting; the estimated "
        "cost is 42,000.",
    )
    bullet_slide(
        deck,
        "Thank you",
        [
            "Questions and ideas are welcome",
            "Contact the growing team at the greenhouse office",
            "Next members' meeting: 13 February 2027",
        ],
    )
    return write_zip(deck_entries(deck))


# =============================================================================================
# The passing base files and the Office fixtures made from them
# =============================================================================================

BASE_LINK: Final = "https://example.net/docchat/base"


def base_document() -> WordDocument:
    """base.docx: one heading, one paragraph (with one https hyperlink and one embedded `.xlsx`
    shown as an OLE object), and one header part."""
    document = new_document("Base document")
    header = document.sections[0].header
    header.paragraphs[0].text = "DocChat base fixture"
    document.add_heading("Base document", 1)
    paragraph = document.add_paragraph(
        "This paragraph is the body of the base document. Each archive and document check "
        "fixture is this file with one change. The workbook below lists the quarterly figures, "
        "and the link names the fixture's home page: "
    )
    word_hyperlink(paragraph, BASE_LINK, "DocChat base fixture")
    append_word_xml(paragraph, word_run(" "))
    workbook = xlsx(
        [["Quarter", "Visitors"], ["Q1", 1250], ["Q2", 1480], ["Q3", 1710], ["Q4", 1390]]
    )
    package = word_embedded_part(
        document, "/word/embeddings/Microsoft_Excel_Worksheet.xlsx", CT_XLSX, workbook, RT_PACKAGE
    )
    preview = word_image(document, framed_png(192, 96, (235, 241, 222), (84, 130, 53)))
    append_word_xml(
        paragraph,
        word_ole_object(
            preview, package, prog_id="Excel.Sheet.12", shape=25, linked=False, declare_frame=True
        ),
    )
    return document


def build_base_docx() -> bytes:
    return write_zip(word_entries(base_document()))


def base_deck() -> SlideDeck:
    """base.pptx: two slides, the second with speaker notes."""
    deck = new_deck("Base presentation")
    bullet_slide(
        deck,
        "Base presentation",
        [
            "Each archive and document check fixture is this deck with one change",
            "Slide two carries the speaker notes",
        ],
    )
    bullet_slide(
        deck,
        "Second slide",
        ["The notes of this slide hold one sentence"],
        notes="These are the speaker notes of the base presentation.",
    )
    return deck


def build_base_pptx() -> bytes:
    return write_zip(deck_entries(base_deck()))


def build_base_chart_pptx() -> bytes:
    """base-chart.pptx: one slide with a column chart whose data sits in an embedded `.xlsx`."""
    deck = new_deck("Base chart")
    slide = titled_slide(deck, 5, "Visitors by quarter")
    data = CategoryChartData()  # type: ignore[no-untyped-call]
    data.categories = ["Q1", "Q2", "Q3", "Q4"]
    data.add_series("Visitors", (1250, 1480, 1710, 1390))  # type: ignore[no-untyped-call]
    # python-pptx annotates add_chart as returning the chart; it returns the chart's frame.
    frame: Any = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED,
        SlideInches(1),
        SlideInches(1.8),
        SlideInches(8),
        SlideInches(5),
        data,  # type: ignore[arg-type]
    )
    # python-pptx writes the workbook with XlsxWriter, which stamps the current time into it;
    # the same cells written here keep the bytes fixed. The layout is python-pptx's: categories
    # in column A from row 2, the series name in B1 and its values below it.
    workbook = xlsx([["", "Visitors"], ["Q1", 1250], ["Q2", 1480], ["Q3", 1710], ["Q4", 1390]])
    frame.chart.part.chart_workbook.update_from_xlsx_blob(workbook)
    return write_zip(deck_entries(deck))


def _fixture_note(document: WordDocument, text: str) -> Paragraph:
    return document.add_paragraph(text)


def build_external_template_docx() -> bytes:
    """base.docx attached to a template that only a web request would fetch."""
    entries = word_entries(base_document())
    insert_before(
        entries,
        "word/settings.xml",
        "<w:defaultTabStop",
        '<w:attachedTemplate r:id="rIdTemplate1"/>',
    )
    template = ("rIdTemplate1", "attachedTemplate", "https://example.net/templates/report.dotx")
    entries.append(ZipEntry("word/_rels/settings.xml.rels", relationships(template, external=True)))
    return write_zip(entries)


def build_dde_field_docx() -> bytes:
    """base.docx with a paragraph holding a DDEAUTO field that links to a workbook cell."""
    document = base_document()
    paragraph = _fixture_note(document, "Budget total from the linked workbook: ")
    append_word_xml(
        paragraph,
        word_field(r'DDEAUTO Excel "C:\\Reports\\budget.xlsx" "Sheet1!R1C1" \a', word_run("1,200")),
    )
    return write_zip(word_entries(document))


def build_ole_package_docx() -> bytes:
    """base.docx with a paragraph holding an OLE Packager object that carries a text file."""
    document = base_document()
    paragraph = _fixture_note(document, "The packaged note: ")
    blob = ole_package("notes.txt", b"Packaged note for the DocChat ole-package fixture.\r\n")
    object_id = word_embedded_part(
        document, "/word/embeddings/oleObject1.bin", CT_OLE_OBJECT, blob, RT_OLE_OBJECT
    )
    icon = word_image(document, framed_png(64, 64, (255, 255, 255), (68, 114, 196)))
    append_word_xml(
        paragraph,
        word_ole_object(
            icon, object_id, prog_id="Package", shape=26, linked=False, size_pt=(48, 48), icon=True
        ),
    )
    return write_zip(word_entries(document))


def build_remote_image_pptx() -> bytes:
    """base.pptx whose first slide shows a picture linked by a web address, not embedded."""
    deck = base_deck()
    slide = deck.slides[0]
    rel_id = slide.part.relate_to(
        "https://example.net/images/remote-photo.png", RT_IMAGE, is_external=True
    )
    append_slide_xml(
        slide,
        slide_picture(
            rel_id, 10, "Remote photo", (6400800, 4572000, 1828800, 1371600), linked=True
        ),
    )
    return write_zip(deck_entries(deck))


def build_bomb_docx() -> bytes:
    """base.docx with one entry that inflates to 512 MiB and one byte (`zip.size`)."""
    entries = word_entries(base_document())
    size = 536_870_913
    entries.append(
        ZipEntry(
            "word/media/zeros.bin", raw=deflate_zeros(size), crc=crc32_of_zeros(size), size=size
        )
    )
    return write_zip(entries)


def build_ratio_docx() -> bytes:
    """base.docx with one entry above 10 MiB that declares a ratio of about 1,000 to 1
    (`zip.ratio`); the archive's declared total stays far below 512 MiB."""
    entries = word_entries(base_document())
    size = 11 * 1024 * 1024
    entries.append(
        ZipEntry(
            "word/media/zeros.bin", raw=deflate_zeros(size), crc=crc32_of_zeros(size), size=size
        )
    )
    return write_zip(entries)


def build_many_entries_pptx() -> bytes:
    """base.pptx with 10,000 more entries, so the archive holds more than 10,000 (`zip.entries`)."""
    entries = deck_entries(base_deck())
    entries += [ZipEntry(f"x/{index:04d}", method=ZIP_STORED) for index in range(10_000)]
    return write_zip(entries)


def build_encrypted_entry_docx() -> bytes:
    """base.docx whose `word/document.xml` is encrypted with the traditional PKWARE cipher
    (`zip.encrypted`, general-purpose flag bit 0)."""
    entries = word_entries(base_document())
    document = find_entry(entries, "word/document.xml")
    crc = zlib.crc32(document.data)
    document.raw = zipcrypto_encrypt(deflate(document.data), b"docchat", crc)
    document.flags = 1
    return write_zip(entries)


def build_unreadable_archive_docx() -> bytes:
    """base.docx cut short before its central directory, so `zipfile` can't read it."""
    archive = build_base_docx()
    return archive[: archive.rindex(b"PK\x01\x02")]


def build_duplicate_names_docx() -> bytes:
    """base.docx with a second copy of its main part under a name that differs only in case."""
    entries = word_entries(base_document())
    entries.append(ZipEntry("word/Document.xml", find_entry(entries, "word/document.xml").data))
    return write_zip(entries)


def build_macro_docm_as_docx() -> bytes:
    """base.docx saved as a macro-enabled document (`.docm`) and named `.docx` (`macro.type`)."""
    entries = word_entries(base_document())
    set_main_content_type(entries, CT_WORD_MAIN, CT_WORD_MACRO)
    insert_before(
        entries,
        "[Content_Types].xml",
        "</Types>",
        f'<Default Extension="bin" ContentType="{CT_VBA_PROJECT}"/>',
    )
    add_package_relationship(
        entries, "word/_rels/document.xml.rels", "rIdVba1", RT_VBA_PROJECT, "vbaProject.bin"
    )
    entries.append(ZipEntry("word/vbaProject.bin", benign_macro_project()))
    return write_zip(entries)


def build_macro_pptm_as_pptx() -> bytes:
    """base.pptx saved as a macro-enabled presentation (`.pptm`) and named `.pptx`."""
    entries = deck_entries(base_deck())
    set_main_content_type(entries, CT_SLIDES_MAIN, CT_SLIDES_MACRO)
    insert_before(
        entries,
        "[Content_Types].xml",
        "</Types>",
        f'<Default Extension="bin" ContentType="{CT_VBA_PROJECT}"/>',
    )
    add_package_relationship(
        entries, "ppt/_rels/presentation.xml.rels", "rIdVba1", RT_VBA_PROJECT, "vbaProject.bin"
    )
    entries.append(ZipEntry("ppt/vbaProject.bin", benign_macro_project()))
    return write_zip(entries)


def build_vba_override_docx() -> bytes:
    """base.docx whose content types add an Override of the VBA project type."""
    entries = word_entries(base_document())
    insert_before(
        entries,
        "[Content_Types].xml",
        "</Types>",
        f'<Override PartName="/word/vbaProject.bin" ContentType="{CT_VBA_PROJECT}"/>',
    )
    return write_zip(entries)


def build_xlsm_default_pptx() -> bytes:
    """base.pptx whose content types add a Default for macro-enabled workbooks."""
    entries = deck_entries(base_deck())
    insert_before(
        entries,
        "[Content_Types].xml",
        "</Types>",
        f'<Default Extension="xlsm" ContentType="{CT_XLSM}"/>',
    )
    return write_zip(entries)


def build_vbaproject_entry_docx() -> bytes:
    """base.docx holding an entry `word/VBAPROJECT.BIN` with no content-type declaration."""
    entries = word_entries(base_document())
    entries.append(ZipEntry("word/VBAPROJECT.BIN", benign_macro_project()))
    return write_zip(entries)


def build_template_dotx_as_docx() -> bytes:
    """base.docx saved as a Word template (`.dotx`) and named `.docx` (`sig.type`)."""
    entries = word_entries(base_document())
    set_main_content_type(entries, CT_WORD_MAIN, CT_WORD_TEMPLATE)
    return write_zip(entries)


def build_template_potx_as_pptx() -> bytes:
    """base.pptx saved as a PowerPoint template (`.potx`) and named `.pptx` (`sig.type`)."""
    entries = deck_entries(base_deck())
    set_main_content_type(entries, CT_SLIDES_MAIN, CT_SLIDES_TEMPLATE)
    return write_zip(entries)


def build_show_ppsx_as_pptx() -> bytes:
    """base.pptx saved as a slide show (`.ppsx`) and named `.pptx` (`sig.type`)."""
    entries = deck_entries(base_deck())
    set_main_content_type(entries, CT_SLIDES_MAIN, CT_SLIDES_SHOW)
    return write_zip(entries)


def build_deck_pptx_as_docx() -> bytes:
    """base.pptx, byte for byte, under a `.docx` name (`sig.type`)."""
    return build_base_pptx()


def build_no_office_document_docx() -> bytes:
    """base.docx whose `_rels/.rels` names no officeDocument relationship."""
    entries = word_entries(base_document())
    edit_entry(
        entries,
        "_rels/.rels",
        lambda data: re.sub(rb'<Relationship [^>]*Type="[^"]*/officeDocument"[^>]*/>', b"", data),
    )
    return write_zip(entries)


# --- The converter fixtures (14-ingestion.md section 15.11 item 6) ----------------------------

_STORED_RESULT: Final = "[the result stored in the file when it was saved]"


def build_include_fields_docx() -> bytes:
    """INCLUDETEXT and INCLUDEPICTURE fields, each naming /etc/hostname and the canary file."""
    document = base_document()
    _fixture_note(
        document,
        "The fields below include local files. Each shows the result stored "
        "in the file, never the content of the file it names.",
    )
    placeholder = word_image(document, solid_png(48, 48, (200, 200, 200)))
    picture_id = 100
    for field_name in ("INCLUDETEXT", "INCLUDEPICTURE"):
        for target in (HOSTNAME_URL, CANARY_URL):
            paragraph = document.add_paragraph(f"{field_name} of a local file: ")
            if field_name == "INCLUDETEXT":
                result = word_run(_STORED_RESULT)
            else:
                picture_id += 1
                result = word_picture(placeholder, picture_id, "Stored picture", (457200, 457200))
            append_word_xml(paragraph, word_field(f'{field_name} "{target}"', result))
    return write_zip(word_entries(document))


def build_linked_section_docx() -> bytes:
    """Two linked sections (`w:subDoc`), naming /etc/hostname and the canary file."""
    document = base_document()
    _fixture_note(document, "The two sections below are linked to local files.")
    for target in (HOSTNAME_URL, CANARY_URL):
        rel_id = document.part.relate_to(target, RT_SUBDOCUMENT, is_external=True)
        append_word_xml(document.add_paragraph(), f'<w:subDoc r:id="{rel_id}"/>')
    return write_zip(word_entries(document))


def build_linked_image_unc_docx() -> bytes:
    """A picture linked, not embedded, by the UNC path \\\\host\\share\\a.png."""
    document = base_document()
    paragraph = _fixture_note(document, "A picture linked from a file share: ")
    rel_id = document.part.relate_to(UNC_IMAGE_TARGET, RT_IMAGE, is_external=True)
    append_word_xml(paragraph, word_picture(rel_id, 101, "a.png", (914400, 914400), linked=True))
    return write_zip(word_entries(document))


def build_linked_image_http_pptx() -> bytes:
    """A picture linked, not embedded, by an http address on the loopback interface."""
    deck = base_deck()
    slide = deck.slides[0]
    rel_id = slide.part.relate_to(LOOPBACK_URL, RT_IMAGE, is_external=True)
    append_slide_xml(
        slide,
        slide_picture(
            rel_id, 10, "Linked picture", (6400800, 4572000, 1828800, 1371600), linked=True
        ),
    )
    return write_zip(deck_entries(deck))


def build_ole_link_docx() -> bytes:
    """Two linked OLE objects, naming /etc/hostname and the canary file."""
    document = base_document()
    preview = word_image(document, framed_png(192, 96, (242, 242, 242), (89, 89, 89)))
    for shape, target in ((27, HOSTNAME_URL), (28, CANARY_URL)):
        paragraph = _fixture_note(document, "An object linked to a local file: ")
        rel_id = document.part.relate_to(target, RT_OLE_OBJECT, is_external=True)
        append_word_xml(
            paragraph,
            word_ole_object(preview, rel_id, prog_id="Word.Document.12", shape=shape, linked=True),
        )
    return write_zip(word_entries(document))


def build_ole_link_pptx() -> bytes:
    """A slide with an OLE object linked to /etc/hostname, updated automatically."""
    deck = base_deck()
    slide = deck.slides[0]
    link_id = slide.part.relate_to(HOSTNAME_URL, RT_OLE_OBJECT, is_external=True)
    _, image_id = slide.part.get_or_add_image_part(
        io.BytesIO(framed_png(192, 96, (242, 242, 242), (89, 89, 89)))
    )
    x, y, cx, cy = 5486400, 4572000, 2743200, 1371600
    box = f'<a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/>'
    preview = slide_picture(image_id, 0, "", (x, y, cx, cy))
    append_slide_xml(
        slide,
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="11" name="Linked object"/>'
        '<p:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></p:cNvGraphicFramePr>'
        f"<p:nvPr/></p:nvGraphicFramePr><p:xfrm>{box}</p:xfrm><a:graphic>"
        '<a:graphicData uri="http://schemas.openxmlformats.org/presentationml/2006/ole">'
        f'<p:oleObj name="Document" r:id="{link_id}" imgW="{cx}" imgH="{cy}" '
        f'progId="Word.Document.12"><p:link updateAutomatic="1"/>{preview}</p:oleObj>'
        "</a:graphicData></a:graphic></p:graphicFrame>",
    )
    return write_zip(deck_entries(deck))


def build_xxe_docx() -> bytes:
    """The main document part's document type declares an external entity naming
    /etc/hostname. The core properties part declares it too and uses it as the title, so a
    parser that expanded entities would read the host name into the title. (A reference in the
    main part makes LibreOffice refuse the whole file, which would prove nothing about it.)"""
    entries = word_entries(base_document())
    declaration = b'<!ENTITY host SYSTEM "' + HOSTNAME_URL.encode() + b'">'
    edit_entry(
        entries,
        "word/document.xml",
        lambda data: replace_once(
            data, b"?>\n", b"?>\n<!DOCTYPE w:document [" + declaration + b"]>\n"
        ),
    )
    edit_entry(
        entries,
        "docProps/core.xml",
        lambda data: replace_once(
            replace_once(
                data, b"?>\n", b"?>\n<!DOCTYPE cp:coreProperties [" + declaration + b"]>\n"
            ),
            b"<dc:title>Base document</dc:title>",
            b"<dc:title>&host;</dc:title>",
        ),
    )
    return write_zip(entries)


def build_vba_autoopen_docx() -> bytes:
    """A document-type package carrying a `vbaProject.bin` whose AutoOpen macro writes the
    marker file. No content type declares the project, so `inspect` refuses it under rule
    `macro.vba`; the document's relationship to it is what a VBA host follows."""
    entries = word_entries(base_document())
    add_package_relationship(
        entries, "word/_rels/document.xml.rels", "rIdVba1", RT_VBA_PROJECT, "vbaProject.bin"
    )
    entries.append(ZipEntry("word/vbaProject.bin", word_macro_project(MACRO_MARKER_PATH)))
    return write_zip(entries)


def build_vba_autoopen_pptx() -> bytes:
    """A presentation-type package carrying a `vbaProject.bin` whose Auto_Open macro writes
    the marker file, with no content-type declaration for it."""
    entries = deck_entries(base_deck())
    add_package_relationship(
        entries, "ppt/_rels/presentation.xml.rels", "rIdVba1", RT_VBA_PROJECT, "vbaProject.bin"
    )
    entries.append(ZipEntry("ppt/vbaProject.bin", presentation_macro_project(MACRO_MARKER_PATH)))
    return write_zip(entries)


# =============================================================================================
# tables.pdf: twelve pages of text with four ruled tables
# =============================================================================================


@dataclass(frozen=True)
class Cell:
    text: str
    columns: int = 1
    rows: int = 1


class Flow:
    """Lays out titles, headings, paragraphs and ruled tables down Letter pages."""

    top: Final = 712.0
    bottom: Final = 72.0
    left: Final = 72.0
    width: Final = 468.0
    row_height: Final = 16.0

    def __init__(self, document: PdfDocument, running_head: str, running_foot: str) -> None:
        self.fonts = text_fonts(document)
        self.running_head = running_head
        self.running_foot = running_foot
        self.pages: list[PdfPage] = []
        self.y = self.top
        self.new_page()

    @property
    def canvas(self) -> Canvas:
        return self.pages[-1].canvas

    def new_page(self) -> None:
        self.pages.append(
            PdfPage(resources={"Font": self.fonts, "ProcSet": [Name("PDF"), Name("Text")]})
        )
        self.y = self.top

    def _room(self, height: float) -> None:
        if self.y - height < self.bottom:
            self.new_page()

    def text_block(
        self,
        text: str,
        *,
        font: str = "F1",
        size: float = 10.5,
        leading: float = 14.0,
        after: float = 8.0,
    ) -> None:
        lines = wrap(text, font, size, self.width)
        self._room(leading * len(lines))
        for line in lines:
            self.y -= leading
            self.canvas.text(self.left, self.y, line, font, size)
        self.y -= after

    def heading(self, text: str, size: float = 13.0) -> None:
        self._room(size * 1.6 + 4 * 14)  # keep a heading with the start of its section
        self.y -= 6
        self.text_block(text, font="F2", size=size, leading=size * 1.4, after=4)

    def table(
        self,
        caption: str,
        header: Sequence[Sequence[Cell]],
        rows: Sequence[Sequence[str]],
        widths: Sequence[float],
    ) -> list[int]:
        """Draw a ruled table; a table that reaches the foot of a page continues on the next
        page under its repeated header rows. Returns the page numbers it occupies."""
        lefts = [self.left + sum(widths[:index]) for index in range(len(widths) + 1)]
        header_height = self.row_height * len(header)

        def draw_header(title: str) -> None:
            self._room(14 + header_height + self.row_height)
            self.y -= 14
            self.canvas.text(self.left, self.y + 3, title, "F2", 9.5)
            occupied: set[tuple[int, int]] = set()
            for row_index, row in enumerate(header):
                column = 0
                for cell in row:
                    while (row_index, column) in occupied:
                        column += 1
                    for r in range(row_index, row_index + cell.rows):
                        for c in range(column, column + cell.columns):
                            occupied.add((r, c))
                    x = lefts[column]
                    w = lefts[column + cell.columns] - x
                    h = self.row_height * cell.rows
                    top = self.y - self.row_height * row_index
                    self.canvas.fill_rect(x, top - h, w, h, 0.88)
                    self.canvas.stroke_rect(x, top - h, w, h)
                    self.canvas.text(x + 4, top - h / 2 - 3.2, cell.text, "F2", 9)
                    column += cell.columns
            self.y -= header_height

        pages = [len(self.pages)]
        draw_header(caption)
        for row in rows:
            if self.y - self.row_height < self.bottom:
                self.new_page()
                pages.append(len(self.pages))
                draw_header(f"{caption.split('.', maxsplit=1)[0]} (continued)")
            for index, value in enumerate(row):
                x, w = lefts[index], widths[index]
                self.canvas.stroke_rect(x, self.y - self.row_height, w, self.row_height)
                if index == 0:
                    self.canvas.text(x + 4, self.y - 11.5, value, "F1", 9)
                else:
                    self.canvas.text(
                        x + w - 4 - text_width(value, "F1", 9), self.y - 11.5, value, "F1", 9
                    )
            self.y -= self.row_height
        self.y -= 12
        return pages

    def finish(self) -> list[PdfPage]:
        count = len(self.pages)
        for number, page in enumerate(self.pages, start=1):
            page.canvas.text(self.left, 752, self.running_head, "F1", 8.5)
            page.canvas.line(self.left, 746, self.left + self.width, 746)
            foot = f"{self.running_foot}  |  Page {number} of {count}"
            page.canvas.text(
                self.left + self.width - text_width(foot, "F1", 8.5), 40, foot, "F1", 8.5
            )
        return self.pages


_WORK_ORDER_ASSETS: Final = (
    ("Alder Creek intake screen", "Plant A"),
    ("Millbrook booster pump 1", "Electrical"),
    ("Cedar Flats well 3 pump", "Plant B"),
    ("Eastgate trunk main", "Network 1"),
    ("Alder Creek filter 2", "Plant A"),
    ("North Ridge reservoir", "Network 2"),
    ("Dunmore chlorine dosing", "Plant B"),
    ("Southfield pressure valve 4", "Network 1"),
    ("Birch Hill clearwell", "Plant A"),
    ("Millbrook booster pump 2", "Electrical"),
    ("Lakeside reservoir", "Network 2"),
    ("Cedar Flats aerator", "Plant B"),
    ("Old Town water tower", "Network 1"),
    ("Dunmore solar inverter", "Electrical"),
    ("Birch Hill filter bed 2", "Plant B"),
    ("Millbrook booster pump 3", "Electrical"),
)
_WORK_TYPES: Final = ("Inspection", "Repair", "Calibration", "Cleaning", "Replacement", "Overhaul")
# Days from opening to closing; the median is 6 and the longest, 23 days, is the Birch Hill
# filter bed media replacement that section 6 describes.
# fmt: off
_WORK_DURATIONS: Final = (3, 6, 2, 9, 5, 7, 1, 12, 4, 6, 8, 5, 6, 3, 10, 6, 2, 7, 6, 4, 11, 5, 6, 8,
                          23, 3, 9, 6, 5, 7, 2, 6, 14, 4, 6, 8, 5, 6, 3, 7)
# fmt: on


def _work_orders() -> list[list[str]]:
    rows = []
    opened = dt.date(2025, 1, 6)
    for index, duration in enumerate(_WORK_DURATIONS):
        if duration == 23:
            asset, crew, work = "Birch Hill filter bed 2", "Plant B", "Replacement"
            opened = dt.date(2025, 8, 4)
        else:
            asset, crew = _WORK_ORDER_ASSETS[index % len(_WORK_ORDER_ASSETS)]
            work = _WORK_TYPES[(index * 5) % len(_WORK_TYPES)]
        closed = opened + dt.timedelta(days=duration)
        rows.append(
            [
                f"WO-25-{40 + index * 31:04d}",
                asset,
                work,
                opened.isoformat(),
                closed.isoformat(),
                crew,
            ]
        )
        opened += dt.timedelta(days=9)
    return rows


# The report's text: "===" starts a page, "# " a heading, "[table <n>]" places table n, and
# every other block is a paragraph.
_REPORT: Final = """
# 1 Introduction

The Fernhollow Valley Water Cooperative supplies drinking water to about 48,000 members across six
districts of the valley. This report describes how the cooperative's four treatment plants, its
storage reservoirs and its distribution network performed during the 2025 calendar year, and it
sets out the work planned for 2026.

Demand rose for the third year in a row. Total deliveries reached 9,412 megalitres, 3.1 percent
more than in 2024, while the share of water lost to leaks fell from 11.8 to 10.9 percent. No
boil-water notice was issued during the year, and every routine sample met the drinking water
standard.

The report follows the order of the cooperative's operating plan. Sections 2 to 5 cover treatment,
sources, deliveries and quality; sections 6 to 10 cover maintenance, customer service, energy,
capital projects and staff; section 11 looks ahead. Figures are for the year ending 31 December
2025 unless a table says otherwise.

===

# 2 Treatment plants

Four plants treat the cooperative's water. Alder Creek and Birch Hill draw from surface reservoirs
and use coagulation, filtration and ultraviolet disinfection. Cedar Flats and Dunmore treat
groundwater from wells and need only aeration, filtration and chlorination. Table 1 lists each
plant's design capacity and its average load.

[table 1]

Birch Hill ran at reduced output for eleven days in August while its second filter bed was
rebuilt. Alder Creek covered the shortfall, which is why its peak day of 12.6 megalitres was the
highest recorded at any plant since 2019.

===

# 3 Source water

Rainfall in the catchment was 1,046 millimetres, close to the thirty-year average, but it arrived
unevenly. A dry spell from mid-June to early September drew the Alder Creek reservoir down to 58
percent of capacity, its lowest level since 2018, before autumn storms refilled it to 96 percent
by the end of November.

Groundwater levels at the Cedar Flats and Dunmore well fields stayed within their normal range.
The cooperative pumped 2,870 megalitres from wells, about 30 percent of the year's supply, within
the annual limit of 3,200 megalitres set in its abstraction licence.

The source protection team inspected 64 farms in the Alder Creek catchment and agreed new fencing
along 7.2 kilometres of stream bank. Turbidity peaks after heavy rain were shorter than in
previous years, which the team attributes to the riparian planting completed in 2023.

===

# 4 Deliveries

Table 2 shows the water delivered to each district in each quarter of 2025, with the change from
the previous year. The third quarter was the busiest, as it is every year, because garden
watering and visitor numbers both peak in July and August.

[table 2]

Lakeside grew fastest, by 6.8 percent, after 140 new homes were connected at the former quarry
site. Old Town grew least; its older housing stock has been fitted with water-efficient
appliances under the cooperative's rebate scheme since 2021.

===

# 5 Water quality

The laboratory analysed 6,218 samples from treatment plants, reservoirs and customer taps. All
bacteriological samples were clear of E. coli. Two samples from the Eastgate network exceeded the
operational target for iron in October; flushing the affected mains brought both back within
target within a week.

Average free chlorine at customer taps was 0.42 milligrams per litre, inside the target band of
0.2 to 0.8. Lead was below the detection limit in 97 percent of samples from older properties, and
the remaining homes were offered a free replacement of their lead supply pipe.

Taste and odour complaints fell to 38, from 61 in 2024. Most followed an algal bloom in the Alder
Creek reservoir in late August, which the new powdered activated carbon dosing unit at Alder Creek
brought under control within four days.

===

# 6 Maintenance

Crews completed 1,284 planned and reactive work orders in 2025. Table 3 lists the work orders
raised for major assets (treatment plants, pumping stations, reservoirs and trunk mains) with the
date each was opened and closed and the crew that carried it out.

[table 3]

The median time to close a work order on a major asset was 6 days, against a target of 10. The
longest job was the replacement of the Birch Hill filter bed media, which took 23 days including
the eleven days the bed was out of service.

===

# 7 Customer service

The service centre answered 21,560 calls and 4,930 web messages. Eighty-six percent of calls were
answered within 30 seconds. The most common reasons for contact were billing questions, reports of
low pressure and requests for meter readings.

Members reported 412 leaks on public pipes, and crews attended 95 percent of them within four
hours. The online leak map, launched in April, now receives about a third of all leak reports and
shows each report's status until the repair is complete.

The cooperative received 57 formal complaints, down from 73, and resolved all but two within the
twenty working days its service charter allows. Both late cases concerned disputed estimated
bills and were settled in January 2026.

===

# 8 Energy use

Treatment and pumping used 7.9 gigawatt hours of electricity, or 0.84 kilowatt hours per cubic
metre delivered. The solar array at Dunmore supplied 1.1 gigawatt hours, and a power purchase
agreement signed in May covers a further 2.5 gigawatt hours a year from the Hollin Ridge wind farm.

Pumping is the largest load. Variable-speed drives fitted to the three Millbrook booster pumps cut
their consumption by 18 percent, and the control system now shifts reservoir filling to the
night, when tariffs are lowest, wherever storage allows.

Net greenhouse gas emissions from operations were 1,960 tonnes of carbon dioxide equivalent, 9
percent lower than in 2024 and 31 percent lower than in the 2019 baseline year.

===

# 9 Capital projects

The board approved a capital programme of 18.6 million for 2025, of which 15.2 million was spent.
Table 4 shows the budget and spending for each project, in thousands, and the share of the work
completed by the end of the year.

[table 4]

Two projects finished on budget. The smart meter rollout fell behind when a supplier delivered
2,000 faulty meters in the spring; the replacements arrived in September and installation will
finish by June 2026.

===

# 10 Staffing and safety

The cooperative employed 142 staff at the end of 2025, including 9 apprentices in the treatment
and network trades. Staff turnover was 7.4 percent. Every operator renewed their treatment
certificate during the year.

There were three lost-time injuries, compared with five in 2024, and no serious incident. All
three were sprains suffered while working in trenches, and a review led to new matting and
lighting standards for excavations.

Staff volunteers ran 26 school visits to the Alder Creek plant, reaching about 900 pupils, and the
annual open day in September drew 1,350 visitors.

===

# 11 Outlook for 2026

Demand is expected to grow by about 2 percent in 2026 as new housing at Lakeside and Millbrook is
connected. The capital programme of 21.4 million focuses on finishing the smart meter rollout,
roofing the Lakeside reservoir and starting the design of a fifth treatment plant at Hollin Ridge.

The cooperative will also review its drought plan in the light of the 2025 dry spell. The review
will test whether the current triggers for restrictions give enough warning and will consult
members on a voluntary saving target for summer peaks.

Questions about this report can be sent to the Operations Office at the cooperative's head office
in Millbrook.
"""


def _report_tables() -> dict[str, tuple[str, list[list[Cell]], list[list[str]], list[float]]]:
    """Caption, header rows, body rows and column widths of the report's four tables; table 2
    has merged header cells and table 3 is long enough to continue on a second page."""
    return {
        "1": (
            "Table 1. Treatment plant capacity and load, 2025",
            [
                [
                    Cell(text)
                    for text in (
                        "Plant",
                        "Commissioned",
                        "Capacity (ML/day)",
                        "Average load (%)",
                        "Peak day (ML)",
                    )
                ]
            ],
            [
                ["Alder Creek", "1978", "14.0", "71", "12.6"],
                ["Birch Hill", "1991", "9.5", "64", "7.9"],
                ["Cedar Flats", "2004", "6.0", "58", "4.8"],
                ["Dunmore", "2016", "4.5", "47", "3.1"],
            ],
            [108, 84, 96, 96, 84],
        ),
        "2": (
            "Table 2. Water delivered by district, 2025",
            [
                [
                    Cell("District", rows=2),
                    Cell("Deliveries by quarter (ML)", columns=4),
                    Cell("Change on 2024 (%)", rows=2),
                ],
                [Cell("Q1"), Cell("Q2"), Cell("Q3"), Cell("Q4")],
            ],
            [
                ["North Ridge", "412", "505", "618", "431", "+2.4"],
                ["Millbrook", "318", "341", "452", "330", "+4.1"],
                ["Eastgate", "287", "351", "356", "292", "+3.6"],
                ["Southfield", "455", "489", "671", "518", "+1.9"],
                ["Lakeside", "201", "236", "398", "214", "+6.8"],
                ["Old Town", "419", "362", "401", "355", "+1.2"],
                ["Total", "2,092", "2,284", "2,896", "2,140", "+3.1"],
            ],
            [108, 66, 66, 66, 66, 96],
        ),
        "3": (
            "Table 3. Work orders for major assets, 2025",
            [[Cell(text) for text in ("Order", "Asset", "Work type", "Opened", "Closed", "Crew")]],
            _work_orders(),
            [62, 136, 70, 66, 66, 68],
        ),
        "4": (
            "Table 4. Capital projects, 2025 (thousands)",
            [[Cell(text) for text in ("Project", "Budget", "Spent", "Remaining", "Complete (%)")]],
            [
                ["Birch Hill filter renewal", "3,400", "3,350", "50", "100"],
                ["Lakeside reservoir roof", "2,900", "2,120", "780", "75"],
                ["Eastgate main replacement", "4,100", "3,640", "460", "90"],
                ["Smart meter rollout, phase 2", "3,600", "2,510", "1,090", "65"],
                ["Alder Creek carbon dosing", "1,200", "1,180", "20", "100"],
                ["Southfield pressure zones", "2,300", "1,610", "690", "60"],
                ["Control system upgrade", "1,100", "790", "310", "70"],
                ["Total", "18,600", "15,200", "3,400", "82"],
            ],
            [168, 72, 72, 72, 84],
        ),
    }


def build_tables_pdf() -> bytes:
    document = PdfDocument()
    flow = Flow(document, "Fernhollow Valley Water Cooperative", "2025 Operations Report")
    flow.text_block("Fernhollow Valley Water Cooperative", font="F2", size=20, leading=26)
    flow.text_block("2025 Operations Report", font="F2", size=14, leading=20)
    flow.text_block(
        "Prepared by the Operations Office and published on 12 February 2026.", after=18
    )
    tables = _report_tables()
    table_pages = []
    for block in text_blocks(_REPORT):
        if block == "===":
            flow.new_page()
        elif block.startswith("# "):
            flow.heading(block.removeprefix("# "))
        elif block.startswith("[table "):
            table_pages.append(flow.table(*tables[block.removeprefix("[table ").removesuffix("]")]))
        else:
            flow.text_block(block)
    pages = flow.finish()
    if len(pages) != 12 or table_pages != [[2], [4], [6, 7], [10]]:
        raise AssertionError(f"tables.pdf layout moved: {len(pages)} pages, tables {table_pages}")
    return document.write(assemble_pdf(document, pages), "tables.pdf")


# =============================================================================================
# scanned.pdf and blank-scan.pdf: image-only pages at 200 dpi, no text layer
# =============================================================================================

# The typed minutes on the six scanned pages: "===" starts a page, "! " is the letterhead, "# " a
# heading, "> " a single line, and every other block a paragraph.
_MINUTES: Final = """
! MERIDIAN RIDGE TRAIL ASSOCIATION

> Minutes of the annual members' meeting

> Held at the Kestrel Hall on Saturday 7 February 2026 at 10:00

# 1. Attendance and apologies

Forty-two members attended, and apologies were received from nine. The chair, Hal Brennan, opened
the meeting at 10:05 and welcomed three new members from the Upper Fennick volunteer group. The
minutes of the 2025 meeting were approved without changes.

# 2. Chair's report

The chair thanked the trail crews, who gave 2,340 hours of volunteer work during the year. He
highlighted the reopening of the Saddleback footbridge in June and the new waymarking on the
Cobble Run loop, which was finished a month ahead of plan.

===

# 3. Trail survey results

The survey team walked all 86 kilometres of association trails between April and October. Eleven
kilometres were rated poor, mostly because of erosion on steep sections of the Ridgeline Path,
compared with fourteen kilometres a year earlier.

The worst section remains the climb above Hollow Gate, where water runs down the trail after
storms. The team recommends stone drains every 30 metres on that climb and a reroute of the lowest
400 metres.

> Ridgeline Path: 31 km surveyed, 7 km rated poor.

> Fennick Valley trail: 24 km surveyed, 3 km rated poor.

> Saddleback spur: 19 km surveyed, 1 km rated poor.

> Cobble Run loop: 12 km surveyed, none rated poor.

===

# 4. Treasurer's report

The treasurer, Ines Moravec, reported income of 48,600 for the year, of which 21,900 came from
membership fees, 15,400 from grants and 11,300 from donations and events. Spending was 44,150,
leaving a surplus of 4,450, which has been added to the bridge reserve.

The largest costs were materials for the Saddleback footbridge at 18,700 and tool replacement at
6,200. An independent examiner reviewed the accounts and found no issues. Members approved the
accounts unanimously.

===

# 5. Election of officers

The chair, treasurer and secretary were re-elected unopposed. Noor Haddad was elected volunteer
coordinator, replacing Pete Lindqvist, who stepped down after eight years and was thanked warmly
for his work.

# 6. Membership fees

The committee proposed raising the annual fee from 20 to 24 to cover rising insurance costs. After
discussion the proposal was approved by 35 votes to 4, with 3 abstentions. Concession rates stay
at 10.

===

# 7. Work programme for 2026

The work programme for 2026 has three priorities: drainage and a reroute on the Hollow Gate climb,
resurfacing two kilometres of the Fennick Valley trail, and a second accessible loop near the
Kestrel Hall car park. Work parties will meet on the first and third Saturday of each month from
March to October.

The association will apply to the county for a grant of 30,000 towards the accessible loop.
Members offered to help with the application and with a survey of likely users.

===

# 8. Any other business

Members asked for an update on dogs off leads near the lambing fields. The committee will put up
seasonal signs from March to May and will ask the landowner about a temporary diversion.

The meeting closed at 12:20. The next annual meeting will be held at the Kestrel Hall on Saturday
6 February 2027.

> Signed: Hal Brennan, chair

> Minutes taken by Ruth Achterberg, secretary
"""
_MINUTE_KINDS: Final = {"! ": "title", "# ": "heading", "> ": "line"}


def _minute_pages() -> list[list[tuple[str, str]]]:
    pages: list[list[tuple[str, str]]] = [[]]
    for block in text_blocks(_MINUTES):
        if block == "===":
            pages.append([])
            continue
        kind = _MINUTE_KINDS.get(block[:2], "para")
        pages[-1].append((kind, block[2:] if kind != "para" else block))
    return pages


_SCAN_SIZE: Final = (PDF_PAGE[0] * SCAN_DPI // 72, PDF_PAGE[1] * SCAN_DPI // 72)  # 1700 x 2200


def aileron() -> bytes:
    """Pillow's built-in Aileron Regular, so no font comes from the host system."""
    font = ImageFont.load_default(size=10)
    if not isinstance(font, ImageFont.FreeTypeFont):
        raise TypeError("Pillow was built without FreeType")
    return font.font_bytes


def _scan_font(size: int) -> ImageFont.FreeTypeFont:
    # Laid out by FreeType alone: Pillow uses Raqm only where the host has FriBiDi, which would
    # make the scanned pixels depend on the machine.
    return ImageFont.truetype(io.BytesIO(aileron()), size, layout_engine=ImageFont.Layout.BASIC)


def scan_image(blocks: Sequence[tuple[str, str]], footer: str) -> bytes:
    """One page of typed text rendered at 200 dpi and thresholded to one bit per pixel, as a
    bilevel office scanner writes it; rows packed from the most significant bit, 1 = white."""
    width, height = _SCAN_SIZE
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    left, text_width_px = 200, width - 400
    y = 200
    for kind, text in blocks:
        size, leading, gap = {
            "title": (46, 64, 20),
            "heading": (36, 52, 8),
            "para": (31, 46, 26),
            "line": (31, 46, 6),
        }[kind]
        font = _scan_font(size)
        if kind == "heading":
            y += 18
        line = ""
        lines = []
        for word in text.split():
            candidate = f"{line} {word}" if line else word
            if line and font.getlength(candidate) > text_width_px:
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)
        for text_line in lines:
            draw.text((left, y), text_line, font=font, fill=0)
            if kind in ("title", "heading"):
                draw.text((left + 1, y), text_line, font=font, fill=0)  # a heavier stroke
            y += leading
        y += gap
    footer_font = _scan_font(28)
    draw.text(
        ((width - footer_font.getlength(footer)) / 2, height - 160),
        footer,
        font=footer_font,
        fill=0,
    )
    if y > height - 220:
        raise AssertionError(f"scanned page overflows: {footer}")
    return image.convert("1", dither=Image.Dither.NONE).tobytes()


def _scan_page(document: PdfDocument, raw: bytes) -> PdfPage:
    width, height = _SCAN_SIZE
    image = document.add(
        Stream(
            {
                "Type": Name("XObject"),
                "Subtype": Name("Image"),
                "Width": width,
                "Height": height,
                "ColorSpace": Name("DeviceGray"),
                "BitsPerComponent": 1,
                "Filter": Name("FlateDecode"),
            },
            zlib_stream(raw),
        )
    )
    page = PdfPage(resources={"XObject": {"Im1": image}, "ProcSet": [Name("PDF"), Name("ImageB")]})
    page.canvas.raw(f"q {PDF_PAGE[0]} 0 0 {PDF_PAGE[1]} 0 0 cm /Im1 Do Q")
    return page


def build_scanned_pdf() -> bytes:
    document = PdfDocument()
    minutes = _minute_pages()
    pages = [
        _scan_page(document, scan_image(blocks, f"Page {number} of {len(minutes)}"))
        for number, blocks in enumerate(minutes, start=1)
    ]
    return document.write(assemble_pdf(document, pages), "scanned.pdf")


def build_blank_scan_pdf() -> bytes:
    """One scanned page with nothing on it: OCR finds no text."""
    document = PdfDocument()
    width, height = _SCAN_SIZE
    page = _scan_page(document, b"\xff" * (width // 8 * height))
    return document.write(assemble_pdf(document, [page]), "blank-scan.pdf")


# =============================================================================================
# base.pdf and the encrypted variants
# =============================================================================================

_BASE_PDF_TEXT: Final = (
    (
        "This page is the body of the base PDF. The archive and document check fixtures change one "
        "thing about it: one encrypts it with a password that a reader must enter, another with an "
        "owner password only, which still lets every reader open it."
    ),
    (
        "The Fernhollow Valley Water Cooperative delivered 9,412 megalitres of water in 2025, and "
        "every routine sample met the drinking water standard."
    ),
)


def _base_pdf_document() -> tuple[PdfDocument, Ref]:
    document = PdfDocument()
    flow = Flow(document, "DocChat base fixture", "base.pdf")
    flow.heading("Base document", size=16)
    for paragraph in _BASE_PDF_TEXT:
        flow.text_block(paragraph)
    return document, assemble_pdf(document, flow.finish())


def build_base_pdf() -> bytes:
    document, root = _base_pdf_document()
    return document.write(root, "base.pdf")


# Print, fill in forms, extract for accessibility and print at full quality; no changes and no
# copying (ISO 32000-1 Table 22).
_OWNER_PERMISSIONS: Final = -1084


def build_owner_only_pdf() -> bytes:
    document, root = _base_pdf_document()
    return document.write(
        root, "owner-only.pdf", encryption=Encryption("docchat-owner", "", _OWNER_PERMISSIONS)
    )


def build_encrypted_pdf() -> bytes:
    document, root = _base_pdf_document()
    return document.write(
        root,
        "encrypted.pdf",
        encryption=Encryption("docchat-owner", "docchat-user", _OWNER_PERMISSIONS),
    )


# =============================================================================================
# active-content.pdf: one of each action and annotation type (ADR 0145), on two pages
# =============================================================================================


def _form(
    document: PdfDocument,
    box: tuple[float, float],
    drawing: str,
    resources: dict[str, PdfValue] | None = None,
) -> Ref:
    """A form XObject: an annotation's normal appearance."""
    return document.add(
        flate(
            drawing.encode("latin-1"),
            {
                "Type": Name("XObject"),
                "Subtype": Name("Form"),
                "BBox": [0, 0, box[0], box[1]],
                "Resources": resources or {},
            },
        )
    )


def _javascript(code: str) -> dict[str, PdfValue]:
    return {"Type": Name("Action"), "S": Name("JavaScript"), "JS": code}


def _link(rect: Sequence[float], **keys: PdfValue) -> dict[str, PdfValue]:
    return {
        "Type": Name("Annot"),
        "Subtype": Name("Link"),
        "Rect": list(rect),
        "Border": [0, 0, 0],
        **keys,
    }


def build_active_content_pdf() -> bytes:
    document = PdfDocument()
    page_1, page_2 = document.reserve(), document.reserve()
    fonts = text_fonts(document)
    helvetica = fonts["F1"]
    form_fonts: dict[str, PdfValue] = {"Font": {"Helv": helvetica}}
    note = b"Attached note of the DocChat active-content fixture.\n"
    attachment = document.add(
        flate(
            note,
            {
                "Type": Name("EmbeddedFile"),
                "Subtype": Name("text/plain"),
                "Params": {"Size": len(note)},
            },
        )
    )
    attachment_spec = document.add(
        {
            "Type": Name("Filespec"),
            "F": "attachment.txt",
            "UF": "attachment.txt",
            "EF": {"F": attachment, "UF": attachment},
            "Desc": "Attached note",
        }
    )

    def button(name: str, label: str, y: float, action: dict[str, PdfValue]) -> Ref:
        appearance = _form(
            document,
            (120, 20),
            f"0.85 g 0 0 120 20 re f 0 G 0.5 w 0.25 0.25 119.5 19.5 re S "
            f"BT /Helv 10 Tf 0 g 8 6 Td ({label}) Tj ET",
            form_fonts,
        )
        return document.add(
            {
                "Type": Name("Annot"),
                "Subtype": Name("Widget"),
                "FT": Name("Btn"),
                "Ff": 65536,
                "T": name,
                "Rect": [380, y, 500, y + 20],
                "F": 4,
                "P": page_1,
                "MK": {"CA": label},
                "AP": {"N": appearance},
                "A": action,
            }
        )

    text_field = document.add(
        {
            "Type": Name("Annot"),
            "Subtype": Name("Widget"),
            "FT": Name("Tx"),
            "T": "customer",
            "V": "Ada Example",
            "DA": "/Helv 10 Tf 0 g",
            "Rect": [380, 610, 540, 630],
            "F": 4,
            "P": page_1,
            "AP": {
                "N": _form(
                    document,
                    (160, 20),
                    "/Tx BMC q BT /Helv 10 Tf 0 g 2 6 Td (Ada Example) Tj ET Q EMC",
                    form_fonts,
                )
            },
            "AA": {
                "K": _javascript("AFSpecial_Keystroke(0);"),
                "Fo": _javascript("app.alert('Focus on the customer field');"),
            },
        }
    )
    submit = button(
        "submit",
        "Submit",
        570,
        {
            "Type": Name("Action"),
            "S": Name("SubmitForm"),
            "F": {"FS": Name("URL"), "F": "https://example.net/docchat/submit"},
            "Flags": 4,
        },
    )
    reset = button("reset", "Reset", 540, {"S": Name("ResetForm")})
    import_button = button("import", "Import", 510, {"S": Name("ImportData"), "F": "data.fdf"})
    hide = button("hide", "Hide", 480, {"S": Name("Hide"), "T": "customer", "H": True})
    paperclip = _form(document, (16, 24), "0 0 1 rg 0.6 w 4 2 8 20 re S 6 5 4 14 re S")
    file_annotation = document.add(
        {
            "Type": Name("Annot"),
            "Subtype": Name("FileAttachment"),
            "Rect": [380, 430, 396, 454],
            "FS": attachment_spec,
            "Name": Name("Paperclip"),
            "Contents": "Attached note",
            "AP": {"N": paperclip},
            "F": 4,
            "P": page_1,
        }
    )
    page_one_annotations: list[PdfValue] = [
        document.add(
            _link(
                [72, 600, 300, 614],
                A={
                    "Type": Name("Action"),
                    "S": Name("URI"),
                    "URI": "https://example.net/docchat/active",
                },
            )
        ),
        document.add(
            _link(
                [72, 580, 300, 594], A={"S": Name("Launch"), "F": "report.txt", "NewWindow": True}
            )
        ),
        document.add(
            _link(
                [72, 560, 300, 574],
                Dest=[page_1, Name("XYZ"), 0, 792, 0],
                PA={"S": Name("URI"), "URI": "https://example.net/docchat/previous"},
            )
        ),
        document.add(_link([72, 540, 300, 554], Dest=[page_2, Name("Fit")])),
        document.add(
            _link(
                [72, 520, 300, 534],
                A={"S": Name("GoToR"), "F": "other.pdf", "D": [0, Name("Fit")], "NewWindow": True},
            )
        ),
        document.add(
            _link(
                [72, 500, 300, 514],
                A={
                    "S": Name("GoToE"),
                    "D": [0, Name("Fit")],
                    "T": {"R": Name("C"), "N": "attachment.pdf"},
                },
            )
        ),
        document.add(_link([72, 480, 300, 494], A={"S": Name("Named"), "N": Name("NextPage")})),
        document.add(_link([72, 460, 300, 474], A={"S": Name("GoTo"), "D": [page_2, Name("Fit")]})),
        document.add(_link([72, 440, 300, 454], A={"S": Name("Thread"), "D": 0})),
        text_field,
        submit,
        reset,
        import_button,
        hide,
        file_annotation,
    ]

    screen = document.reserve()
    clip_spec = document.add({"Type": Name("Filespec"), "F": "clip.mp4", "UF": "clip.mp4"})
    rendition: dict[str, PdfValue] = {
        "Type": Name("Action"),
        "S": Name("Rendition"),
        "OP": 0,
        "AN": screen,
        "R": {
            "Type": Name("Rendition"),
            "S": Name("MR"),
            "C": {"Type": Name("MediaClip"), "S": Name("MCD"), "D": clip_spec, "CT": "video/mp4"},
        },
    }
    document.set(
        screen,
        {
            "Type": Name("Annot"),
            "Subtype": Name("Screen"),
            "Rect": [72, 560, 232, 650],
            "P": page_2,
            "T": "Clip",
            "A": rendition,
        },
    )
    samples = bytes(([0xC0] * 10 + [0x40] * 10) * 40)  # a 400 Hz square wave, 0.1 s at 8 kHz
    sound = document.add(
        Stream({"Type": Name("Sound"), "R": 8000, "C": 1, "B": 8, "E": Name("Unsigned")}, samples)
    )
    movie = document.reserve()
    document.set(
        movie,
        {
            "Type": Name("Annot"),
            "Subtype": Name("Movie"),
            "Rect": [252, 560, 412, 650],
            "T": "Movie",
            "Movie": {"F": "clip.mov", "Aspect": [320, 240]},
            "A": True,
        },
    )
    model = document.add(
        flate(
            b"U3D placeholder of the DocChat active-content fixture\n",
            {"Type": Name("3D"), "Subtype": Name("U3D")},
        )
    )
    video = document.add(
        flate(
            b"\x00\x00\x00\x18ftypmp42" + bytes(16),
            {"Type": Name("EmbeddedFile"), "Subtype": Name("video/mp4")},
        )
    )
    asset = document.add(
        {"Type": Name("Filespec"), "F": "clip.mp4", "UF": "clip.mp4", "EF": {"F": video}}
    )
    instance = document.add(
        {"Type": Name("RichMediaInstance"), "Subtype": Name("Video"), "Asset": asset}
    )
    rich_media = document.add(
        {
            "Type": Name("Annot"),
            "Subtype": Name("RichMedia"),
            "Rect": [72, 420, 232, 510],
            "RichMediaContent": {
                "Type": Name("RichMediaContent"),
                "Assets": {"Names": ["clip.mp4", asset]},
                "Configurations": [
                    {
                        "Type": Name("RichMediaConfiguration"),
                        "Subtype": Name("Video"),
                        "Instances": [instance],
                    }
                ],
            },
            "RichMediaSettings": {
                "Type": Name("RichMediaSettings"),
                "Activation": {"Type": Name("RichMediaActivation"), "Condition": Name("XA")},
            },
        }
    )
    page_two_annotations: list[PdfValue] = [
        screen,
        movie,
        document.add(
            {
                "Type": Name("Annot"),
                "Subtype": Name("Sound"),
                "Rect": [432, 620, 452, 640],
                "Sound": sound,
                "Name": Name("Speaker"),
            }
        ),
        document.add(
            {
                "Type": Name("Annot"),
                "Subtype": Name("3D"),
                "Rect": [252, 420, 412, 510],
                "3DD": model,
                "3DA": {"A": Name("PO"), "D": Name("PC")},
            }
        ),
        rich_media,
        document.add(
            _link([72, 380, 300, 394], A={"S": Name("Sound"), "Sound": sound, "Volume": 1.0})
        ),
        document.add(
            _link(
                [72, 360, 300, 374],
                A={"S": Name("Movie"), "Annotation": movie, "Operation": Name("Play")},
            )
        ),
        document.add(
            _link(
                [72, 340, 300, 354],
                A={
                    "S": Name("RichMediaExecute"),
                    "TA": rich_media,
                    "TI": instance,
                    "CMD": {"Type": Name("RichMediaCommand"), "C": "play"},
                },
            )
        ),
        document.add(
            _link(
                [72, 320, 300, 334], A=_javascript("app.alert('DocChat active-content fixture');")
            )
        ),
    ]

    thread, bead_1, bead_2 = document.reserve(), document.reserve(), document.reserve()
    document.set(thread, {"Type": Name("Thread"), "F": bead_1, "I": {"Title": "Fixture article"}})
    document.set(
        bead_1,
        {
            "Type": Name("Bead"),
            "T": thread,
            "N": bead_2,
            "V": bead_2,
            "P": page_1,
            "R": [72, 660, 540, 700],
        },
    )
    document.set(
        bead_2,
        {"Type": Name("Bead"), "N": bead_1, "V": bead_1, "P": page_2, "R": [72, 660, 540, 700]},
    )
    outlines, item_1, item_2 = document.reserve(), document.reserve(), document.reserve()
    document.set(outlines, {"Type": Name("Outlines"), "First": item_1, "Last": item_2, "Count": 2})
    document.set(
        item_1,
        {
            "Title": "Project home page",
            "Parent": outlines,
            "Next": item_2,
            "A": {"S": Name("URI"), "URI": "https://example.net/docchat/home"},
        },
    )
    document.set(
        item_2,
        {
            "Title": "Report archive",
            "Parent": outlines,
            "Prev": item_1,
            "A": {"S": Name("URI"), "URI": "https://example.net/docchat/archive"},
        },
    )

    page_one = PdfPage(resources={"Font": fonts, "ProcSet": [Name("PDF"), Name("Text")]})
    lines_one = [
        "Active content fixture, page 1 of 2",
        "Links on this page open a web address, start a program, jump within the file, open",
        "another file, open an embedded file, turn the page, and follow an article thread.",
        "The form on the right submits, resets, imports and hides; a note is attached below it.",
    ]
    page_two = PdfPage(resources={"Font": fonts, "ProcSet": [Name("PDF"), Name("Text")]})
    lines_two = [
        "Active content fixture, page 2 of 2",
        "This page carries a screen, a movie, a sound, a 3D model and a rich media annotation,",
        "and links that play the sound, the movie and the rich media and run a script.",
    ]
    for page, lines in ((page_one, lines_one), (page_two, lines_two)):
        for index, line in enumerate(lines):
            page.canvas.text(
                72, 720 - index * 16, line, "F2" if index == 0 else "F1", 14 if index == 0 else 10.5
            )
    for index, label in enumerate(
        (
            "Web address",
            "Start a program",
            "This page",
            "Page 2",
            "Another file",
            "Embedded file",
            "Next page",
            "Go to page 2",
            "Article thread",
        )
    ):
        page_one.canvas.text(76, 603 - index * 20, label, "F1", 10)
    page_one.extra = {
        "Annots": page_one_annotations,
        "B": [bead_1],
        "AA": {
            "O": _javascript("console.println('page opened');"),
            "C": _javascript("console.println('page closed');"),
        },
    }
    page_two.extra = {"Annots": page_two_annotations, "B": [bead_2]}
    root = assemble_pdf(
        document,
        [page_one, page_two],
        {
            "PageMode": Name("UseOutlines"),
            "URI": {"Base": "https://example.net/docchat/"},
            "OpenAction": {
                **_javascript("app.alert('DocChat active-content fixture opened');"),
                "Next": {"S": Name("URI"), "URI": "https://example.net/docchat/opened"},
            },
            "AA": {"WC": _javascript("app.alert('Closing');")},
            "Names": {
                "JavaScript": {"Names": ["init", document.add(_javascript("var docchat = 1;"))]},
                "EmbeddedFiles": {"Names": ["attachment.txt", attachment_spec]},
                "Dests": {"Names": ["second-page", [page_2, Name("Fit")]]},
            },
            "Outlines": outlines,
            "AcroForm": {
                "Fields": [text_field, submit, reset, import_button, hide],
                "DA": "/Helv 10 Tf 0 g",
                "DR": form_fonts,
            },
            "Threads": [thread],
        },
        page_refs=[page_1, page_2],
    )
    return document.write(root, "active-content.pdf")


# =============================================================================================
# The rendition schema fixtures (14-ingestion.md section 14.8): one page of text, one construct
# =============================================================================================


def _schema_page(document: PdfDocument, lines: Sequence[str]) -> PdfPage:
    page = PdfPage(resources={"Font": text_fonts(document), "ProcSet": [Name("PDF"), Name("Text")]})
    page.canvas.text(72, 720, "Rendition schema fixture", "F2", 14)
    for index, line in enumerate(lines):
        page.canvas.text(72, 696 - index * 15, line)
    return page


def _single_page(
    name: str,
    document: PdfDocument,
    page: PdfPage,
    catalogue_extra: dict[str, PdfValue] | None = None,
    page_ref: Ref | None = None,
) -> bytes:
    root = assemble_pdf(
        document, [page], catalogue_extra, page_refs=[page_ref] if page_ref is not None else None
    )
    return document.write(root, name)


def _gray_image(size: int, light: int, dark: int) -> Stream:
    rows = b"".join(
        bytes(light + (dark - light) * ((x + y) % size) // size for x in range(size))
        for y in range(size)
    )
    return flate(
        rows,
        {
            "Type": Name("XObject"),
            "Subtype": Name("Image"),
            "Width": size,
            "Height": size,
            "ColorSpace": Name("DeviceGray"),
            "BitsPerComponent": 8,
        },
    )


def build_schema_presteps_gotor_pdf() -> bytes:
    document = PdfDocument()
    page = _schema_page(
        document,
        [
            "This page carries a presentation step whose navigation node",
            "opens a page of another file when the reader moves on.",
        ],
    )
    page.extra = {
        "PresSteps": {
            "Type": Name("NavNode"),
            "NA": {"S": Name("GoToR"), "F": "other.pdf", "D": [0, Name("Fit")]},
        }
    }
    return _single_page("schema-presteps-gotor.pdf", document, page)


def build_schema_gotoe_unlisted_pdf() -> bytes:
    document = PdfDocument()
    page = _schema_page(
        document,
        [
            "This page holds, under a page key that no table lists, an",
            "action that opens a file embedded in another file.",
        ],
    )
    page.extra = {
        "X1": {
            "S": Name("GoToE"),
            "D": [0, Name("Fit")],
            "T": {"R": Name("C"), "N": "attached.pdf"},
        }
    }
    return _single_page("schema-gotoe-unlisted.pdf", document, page)


def build_schema_page_af_pdf() -> bytes:
    document = PdfDocument()
    data = b'<?xml version="1.0"?>\n<invoice number="2026-0042" total="118.00"/>\n'
    embedded = document.add(
        flate(
            data,
            {
                "Type": Name("EmbeddedFile"),
                "Subtype": Name("text/xml"),
                "Params": {"Size": len(data)},
            },
        )
    )
    page = _schema_page(
        document,
        [
            "This page names an associated file: a file specification",
            "that embeds a small XML document.",
        ],
    )
    page.extra = {
        "AF": [
            {
                "F": "data.xml",
                "UF": "data.xml",
                "EF": {"F": embedded},
                "AFRelationship": Name("Data"),
            }
        ]
    }
    return _single_page("schema-page-af.pdf", document, page)


def build_schema_image_alternates_pdf() -> bytes:
    document = PdfDocument()
    alternate = document.add(_gray_image(16, 80, 200))
    image = _gray_image(16, 240, 60)
    image.dictionary["Alternates"] = [{"Image": alternate, "DefaultForPrinting": True}]
    page = _schema_page(
        document, ["The square below is an image that names an alternate image", "for printing."]
    )
    page.resources["XObject"] = {"Im1": document.add(image)}
    page.canvas.raw("q 96 0 0 96 72 560 cm /Im1 Do Q")
    return _single_page("schema-image-alternates.pdf", document, page)


def build_schema_form_ref_pdf() -> bytes:
    document = PdfDocument()
    form_font = document.add(standard_font("Helvetica"))
    form = flate(
        b"0.5 w 0.5 0.5 239 59 re S BT /F1 10 Tf 10 26 Td (Stand-in for page 1 of "
        b"another file) Tj ET\n",
        {
            "Type": Name("XObject"),
            "Subtype": Name("Form"),
            "BBox": [0, 0, 240, 60],
            "Resources": {"Font": {"F1": form_font}},
            "Ref": {"F": "other.pdf", "Page": 0},
        },
    )
    page = _schema_page(
        document,
        [
            "The box below is a form that refers to page 1 of another",
            "file; it draws its own stand-in content.",
        ],
    )
    page.resources["XObject"] = {"Fm1": document.add(form)}
    page.canvas.raw("q 1 0 0 1 72 560 cm /Fm1 Do Q")
    return _single_page("schema-form-ref.pdf", document, page)


def build_schema_stream_f_pdf() -> bytes:
    document = PdfDocument()
    image = _gray_image(16, 230, 40)
    image.dictionary["F"] = "x.bin"
    page = _schema_page(
        document,
        ["The square below is an image whose stream also names an", "external file for its data."],
    )
    page.resources["XObject"] = {"Im1": document.add(image)}
    page.canvas.raw("q 96 0 0 96 72 560 cm /Im1 Do Q")
    return _single_page("schema-stream-f.pdf", document, page)


def build_schema_font_unlisted_pdf() -> bytes:
    document = PdfDocument()
    page = _schema_page(
        document,
        [
            "The font that draws this text carries, under a key that no",
            "table lists, an action that opens a web address.",
        ],
    )
    fonts = page.resources["Font"]
    assert isinstance(fonts, dict)
    font = standard_font("Helvetica")
    font["X1"] = {"S": Name("URI"), "URI": "https://example.net/docchat/font"}
    fonts["F1"] = document.add(font)
    return _single_page("schema-font-unlisted.pdf", document, page)


def build_schema_annotation_ap_pdf() -> bytes:
    document = PdfDocument()
    page_ref = document.reserve()
    helvetica = document.add(standard_font("Helvetica"))
    square = document.add(
        {
            "Type": Name("Annot"),
            "Subtype": Name("Square"),
            "Rect": [72, 520, 192, 600],
            "F": 4,
            "P": page_ref,
            "C": [0.8, 0.1, 0.1],
            "BS": {"W": 2},
            "Contents": "A reviewer's square",
            "AP": {"N": _form(document, (120, 80), "0.8 0.1 0.1 RG 2 w 1 1 118 78 re S")},
        }
    )
    field_value = "Filled value"
    widget = document.add(
        {
            "Type": Name("Annot"),
            "Subtype": Name("Widget"),
            "FT": Name("Tx"),
            "T": "reviewer",
            "V": field_value,
            "DA": "/Helv 11 Tf 0 g",
            "Rect": [240, 560, 440, 584],
            "F": 4,
            "P": page_ref,
            "AP": {
                "N": _form(
                    document,
                    (200, 24),
                    f"/Tx BMC q BT /Helv 11 Tf 0 g 2 7 Td ({field_value}) Tj ET Q EMC",
                    {"Font": {"Helv": helvetica}},
                )
            },
        }
    )
    link = document.add(_link([72, 480, 300, 494], Dest=[page_ref, Name("Fit")]))
    page = _schema_page(
        document,
        [
            "A square annotation and a filled form field, each with a normal",
            "appearance, and a link to this same page.",
        ],
    )
    page.canvas.text(76, 483, "Back to the top of this page", "F1", 10)
    page.extra = {"Annots": [square, widget, link]}
    return _single_page(
        "schema-annotation-ap.pdf",
        document,
        page,
        {
            "AcroForm": {
                "Fields": [widget],
                "DA": "/Helv 11 Tf 0 g",
                "DR": {"Font": {"Helv": helvetica}},
            }
        },
        page_ref,
    )


def build_schema_resources_unknown_pdf() -> bytes:
    document = PdfDocument()
    page = _schema_page(
        document,
        [
            "The resources of this page hold an unknown dictionary that",
            "describes a program launch.",
        ],
    )
    page.resources["X1"] = Raw(b"<< /S /Launch /F (calc.exe) >>")
    return _single_page("schema-resources-unknown.pdf", document, page)


# --- The schema limit fixtures ----------------------------------------------------------------

_NESTED_FORMS: Final = 21


def build_schema_depth_65_pdf() -> bytes:
    """Twenty-one nested form XObjects: below the page dictionary, form k sits 3k containers deep
    (resources, the XObject dictionary, the form), so form 21 is at depth 63, its resources at
    64 and their /ProcSet array at 65."""
    document = PdfDocument()
    inner = document.add(
        flate(
            b"0.6 g 0 0 10 10 re f\n",
            {
                "Type": Name("XObject"),
                "Subtype": Name("Form"),
                "BBox": [0, 0, 10, 10],
                "Resources": {"ProcSet": [Name("PDF")]},
            },
        )
    )
    for level in range(_NESTED_FORMS - 1, 0, -1):
        inner = document.add(
            flate(
                f"q 1 0 0 1 2 2 cm /Fm{level + 1} Do Q\n".encode(),
                {
                    "Type": Name("XObject"),
                    "Subtype": Name("Form"),
                    "BBox": [0, 0, 100, 100],
                    "Resources": {"XObject": {f"Fm{level + 1}": inner}, "ProcSet": [Name("PDF")]},
                },
            )
        )
    page = _schema_page(
        document,
        [
            "The small square below is drawn by twenty-one form XObjects,",
            "each nested inside the one before.",
        ],
    )
    page.resources["XObject"] = {"Fm1": inner}
    page.canvas.raw("q 1 0 0 1 72 560 cm /Fm1 Do Q")
    return _single_page("schema-depth-65.pdf", document, page)


_CONTAINER_BASE: Final = 17


def _container_page(functions: int, short_functions: int, lines: Sequence[str]) -> PdfPage:
    """A text page whose axial shading stitches `functions` exponential functions.

    Counted from the page dictionary through the keys the rendition schema lists, the page holds
    17 containers of its own (the page; its /MediaBox, /Resources and /Contents; the /Font,
    /Shading and /ProcSet entries of its resources and the font; the shading with its /Coords,
    /Domain and /Extend; the stitching function with its /Domain, /Functions, /Bounds and
    /Encode), plus four for each stitched function (its dictionary, /Domain, /C0 and /C1), or
    three for each of the `short_functions` that leaves /C0 at its default. Every stitched
    function ramps from mid grey to light grey, so the band shows a saw-tooth gradient."""
    short = "<< /FunctionType 2 /Domain [0 1] /C1 [0.92] /N 1 >>"
    full = "<< /FunctionType 2 /Domain [0 1] /C0 [0.7] /C1 [0.92] /N 1 >>"
    stitched = [short] * short_functions + [full] * (functions - short_functions)
    bounds = " ".join(str(index) for index in range(1, functions))
    encode = " ".join(["0 1"] * functions)
    function = (
        f"<< /FunctionType 3 /Domain [0 {functions}] /Functions [{' '.join(stitched)}] "
        f"/Bounds [{bounds}] /Encode [{encode}] >>"
    )
    shading: dict[str, PdfValue] = {
        "ShadingType": 2,
        "ColorSpace": Name("DeviceGray"),
        "Coords": [72, 0, 540, 0],
        "Domain": [0, functions],
        "Extend": [False, False],
        "Function": Raw(function.encode()),
    }
    # The font and the shading are direct objects of the page's resources, so nothing is shared
    # and each page is one object besides its content stream.
    page = PdfPage(
        resources={
            "Font": {"F1": standard_font("Helvetica")},
            "Shading": {"Sh1": shading},
            "ProcSet": [Name("PDF"), Name("Text")],
        }
    )
    for index, line in enumerate(lines):
        page.canvas.text(72, 720 - index * 15, line)
    page.canvas.raw("q 72 640 468 30 re W n /Sh1 sh Q")
    return page


def build_schema_containers_100001_pdf() -> bytes:
    """One page of exactly 100,001 distinct containers (the page cap is 100,000)."""
    document = PdfDocument()
    functions = (100_001 - _CONTAINER_BASE) // 4  # 24,996 stitched functions of four containers
    page = _container_page(
        functions,
        0,
        [
            "Rendition schema fixture: the gradient below stitches 24,996 functions, so this page",
            "holds 100,001 containers, one more than a page rendition may hold.",
        ],
    )
    return document.write(
        assemble_pdf(document, [page]), "schema-containers-100001.pdf", object_streams=True
    )


def build_schema_document_1000001_pdf() -> bytes:
    """The generated document of the schema limits test: 2,000 pages whose containers total
    1,000,001 (1,999 pages of 500 and one of 501), so every page renders and the whole-document
    rendition is one container above its cap. No object is shared between pages."""
    document = PdfDocument()
    pages = []
    for number in range(1, 2001):
        lines = [f"Page {number} of 2000"]
        if number == 2000:
            pages.append(_container_page(121, 0, lines))  # 17 + 484 = 501
        else:
            pages.append(_container_page(121, 1, lines))  # 17 + 483 = 500
    return document.write(
        assemble_pdf(document, pages), "schema-document-1000001.pdf", object_streams=True
    )


# --- The inline dictionary fixtures (14-ingestion.md section 15.6 item 14.2, invariant 5) ----


def build_schema_inline_af_pdf() -> bytes:
    document = PdfDocument()
    page = _schema_page(
        document,
        [
            "The next line is marked content whose property list, written",
            "in the content stream, names an associated file.",
        ],
    )
    page.canvas.raw("/Span << /AF [ << /Type /Filespec /F (a.pdf) >> ] >> BDC")
    page.canvas.text(72, 640, "Marked content with an inline property list.")
    page.canvas.raw("EMC")
    page.compress = False
    return _single_page("schema-inline-af.pdf", document, page)


def build_schema_inline_image_url_pdf() -> bytes:
    document = PdfDocument()
    page = _schema_page(
        document,
        ["The grey square below is an inline image whose dictionary", "names a web address."],
    )
    page.canvas.raw("q 24 0 0 24 72 600 cm")
    page.canvas.raw(b"BI /W 1 /H 1 /BPC 8 /CS /G /URL (https://example.net/x) ID \x80 EI")
    page.canvas.raw("Q")
    page.compress = False
    return _single_page("schema-inline-image-url.pdf", document, page)


def build_schema_inline_tagged_pdf() -> bytes:
    """The passing control: tagged-PDF property lists and an optional-content tag that names a
    /Properties resource, none holding a name of the net."""
    document = PdfDocument()
    page_ref = document.reserve()
    layer = document.add({"Type": Name("OCG"), "Name": "Layer 1"})
    structure_root = document.reserve()
    paragraph = document.add(
        {"Type": Name("StructElem"), "S": Name("P"), "P": structure_root, "Pg": page_ref, "K": 0}
    )
    document.set(
        structure_root,
        {
            "Type": Name("StructTreeRoot"),
            "K": [paragraph],
            "ParentTree": {"Nums": [0, [paragraph]]},
            "ParentTreeNextKey": 1,
        },
    )
    page = _schema_page(
        document,
        [
            "The lines below are tagged: a paragraph with a language, a",
            "number with replacement text, a figure with alternate text,",
            "an abbreviation with its expansion and a line in a layer.",
        ],
    )
    page.resources["Properties"] = {"oc1": layer}
    canvas = page.canvas
    canvas.raw("/P << /MCID 0 /Lang (en-US) >> BDC")
    canvas.text(72, 620, "A tagged paragraph in English.")
    canvas.raw("EMC")
    canvas.raw("/Span << /ActualText (fifty) >> BDC")
    canvas.text(72, 600, "50")
    canvas.raw("EMC")
    canvas.raw("/Figure << /Alt (A grey square) >> BDC")
    canvas.fill_rect(72, 560, 24, 24, 0.5)
    canvas.raw("EMC")
    canvas.raw("/Span << /E (Document Intelligence) >> BDC")
    canvas.text(72, 540, "DI")
    canvas.raw("EMC")
    canvas.raw("/OC /oc1 BDC")
    canvas.text(72, 520, "A line in the optional-content layer.")
    canvas.raw("EMC")
    page.compress = False
    page.extra = {"StructParents": 0}
    return _single_page(
        "schema-inline-tagged.pdf",
        document,
        page,
        {
            "MarkInfo": {"Marked": True},
            "StructTreeRoot": structure_root,
            "Lang": "en-US",
            "OCProperties": {"OCGs": [layer], "D": {"Order": [layer], "ON": [layer]}},
        },
        page_ref,
    )


# --- The whole-document fixture ---------------------------------------------------------------


def _sfnt_tables(font: bytes) -> dict[bytes, bytes]:
    count = struct.unpack(">H", font[4:6])[0]
    tables = {}
    for index in range(count):
        tag, _, offset, length = struct.unpack(">4sIII", font[12 + 16 * index : 28 + 16 * index])
        tables[tag] = font[offset : offset + length]
    return tables


_GLYPH_NAMES: Final = {
    " ": "space",
    ".": "period",
    ",": "comma",
    ":": "colon",
    "-": "hyphen",
    **{
        str(digit): name
        for digit, name in enumerate(
            ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
        )
    },
}


def _memo_font(document: PdfDocument, text: str) -> tuple[Ref, bytes]:
    """An embedded TrueType font (Pillow's Aileron Regular) whose encoding gives the characters
    of `text` the codes 1, 2, 3 and on in order of first use; returns the font and the text
    in those codes. Two pages with different text get different codes for the same letters."""
    font_bytes = aileron()
    tables = _sfnt_tables(font_bytes)
    units: int = struct.unpack(">H", tables[b"head"][18:20])[0]
    bbox = list(struct.unpack(">hhhh", tables[b"head"][36:44]))
    ascent, descent = struct.unpack(">hh", tables[b"hhea"][4:8])
    cap_height = struct.unpack(">h", tables[b"OS/2"][88:90])[0]
    metrics = ImageFont.truetype(
        io.BytesIO(font_bytes), size=units, layout_engine=ImageFont.Layout.BASIC
    )
    characters = list(dict.fromkeys(text))
    codes = {character: index + 1 for index, character in enumerate(characters)}

    def scale(value: float) -> int:
        return round(value * 1000 / units)

    differences: list[PdfValue] = [1, *(Name(_GLYPH_NAMES.get(c, c)) for c in characters)]
    to_unicode = "\n".join(
        [
            "/CIDInit /ProcSet findresource begin",
            "12 dict begin",
            "begincmap",
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
            "/CMapName /Adobe-Identity-UCS def",
            "/CMapType 2 def",
            "1 begincodespacerange",
            "<00> <FF>",
            "endcodespacerange",
            f"{len(characters)} beginbfchar",
            *(f"<{codes[c]:02X}> <{ord(c):04X}>" for c in characters),
            "endbfchar",
            "endcmap",
            "CMapName currentdict /CMap defineresource pop",
            "end",
            "end",
            "",
        ]
    ).encode()
    descriptor = document.add(
        {
            "Type": Name("FontDescriptor"),
            "FontName": Name("Aileron-Regular"),
            "Flags": 32,
            "FontBBox": [scale(value) for value in bbox],
            "ItalicAngle": 0,
            "Ascent": scale(ascent),
            "Descent": scale(descent),
            "CapHeight": scale(cap_height),
            "StemV": 80,
            "FontFile2": document.add(flate(font_bytes, {"Length1": len(font_bytes)})),
        }
    )
    font = document.add(
        {
            "Type": Name("Font"),
            "Subtype": Name("TrueType"),
            "BaseFont": Name("Aileron-Regular"),
            "FirstChar": 1,
            "LastChar": len(characters),
            "Widths": [scale(metrics.getlength(c)) for c in characters],
            "Encoding": {
                "Type": Name("Encoding"),
                "BaseEncoding": Name("WinAnsiEncoding"),
                "Differences": differences,
            },
            "FontDescriptor": descriptor,
            "ToUnicode": document.add(flate(to_unicode)),
        }
    )
    return font, bytes(codes[c] for c in text)


def _memo_image(pattern: Callable[[int, int], tuple[int, int, int]]) -> Stream:
    rows = b"".join(
        bytes(channel for x in range(64) for channel in pattern(x, y)) for y in range(64)
    )
    return flate(
        rows,
        {
            "Type": Name("XObject"),
            "Subtype": Name("Image"),
            "Width": 64,
            "Height": 64,
            "ColorSpace": Name("DeviceRGB"),
            "BitsPerComponent": 8,
        },
    )


def build_schema_memo_two_pages_pdf() -> bytes:
    """Two pages laid out alike, each drawing its own text in its own embedded font and its own
    image. The pages' objects have the same shapes, so a renderer that copies each page alone
    numbers each page's font and image alike."""
    document = PdfDocument()
    memos = [
        (
            "Memo 1: the north reservoir inspection is due in March.",
            lambda x, y: (40 + 3 * x, 60, 220 - 3 * y),
        ),
        (
            "Memo 2: the south pump station report is due in April.",
            lambda x, y: (30, 200, 30) if (x // 8 + y // 8) % 2 else (240, 240, 200),
        ),
    ]
    pages = []
    for text, pattern in memos:
        font, encoded = _memo_font(document, text)
        image = document.add(_memo_image(pattern))
        page = PdfPage(
            resources={
                "Font": {"F1": font},
                "XObject": {"Im1": image},
                "ProcSet": [Name("PDF"), Name("Text"), Name("ImageC")],
            }
        )
        page.canvas.raw(b"BT /F1 16 Tf 72 700 Td " + _hex_string(encoded) + b" Tj ET")
        page.canvas.raw("q 128 0 0 128 72 520 cm /Im1 Do Q")
        pages.append(page)
    return document.write(assemble_pdf(document, pages), "schema-memo-two-pages.pdf")


# =============================================================================================
# The fixtures, by file name
# =============================================================================================

FIXTURES: Final[dict[str, Callable[[], bytes]]] = {
    # The four documents every test level shares.
    "tables.pdf": build_tables_pdf,
    "scanned.pdf": build_scanned_pdf,
    "headings.docx": build_headings_docx,
    "slides.pptx": build_slides_pptx,
    # The passing base files.
    "base.docx": build_base_docx,
    "base.pptx": build_base_pptx,
    "base-chart.pptx": build_base_chart_pptx,
    "base.pdf": build_base_pdf,
    "owner-only.pdf": build_owner_only_pdf,
    # The archive and document check fixtures (T-189), each a base file with one change.
    "bomb.docx": build_bomb_docx,
    "many-entries.pptx": build_many_entries_pptx,
    "macro.docm-as.docx": build_macro_docm_as_docx,
    "encrypted.pdf": build_encrypted_pdf,
    "blank-scan.pdf": build_blank_scan_pdf,
    "external-template.docx": build_external_template_docx,
    "dde-field.docx": build_dde_field_docx,
    "remote-image.pptx": build_remote_image_pptx,
    "ole-package.docx": build_ole_package_docx,
    "encrypted-entry.docx": build_encrypted_entry_docx,
    "unreadable-archive.docx": build_unreadable_archive_docx,
    "duplicate-names.docx": build_duplicate_names_docx,
    "ratio.docx": build_ratio_docx,
    # The Office type check fixtures (T-610), each a base file with one change.
    "macro.pptm-as.pptx": build_macro_pptm_as_pptx,
    "vba-override.docx": build_vba_override_docx,
    "xlsm-default.pptx": build_xlsm_default_pptx,
    "vbaproject-entry.docx": build_vbaproject_entry_docx,
    "template.dotx-as.docx": build_template_dotx_as_docx,
    "template.potx-as.pptx": build_template_potx_as_pptx,
    "show.ppsx-as.pptx": build_show_ppsx_as_pptx,
    "deck.pptx-as.docx": build_deck_pptx_as_docx,
    "no-office-document.docx": build_no_office_document_docx,
    # The converter fixtures (14-ingestion.md section 15.11 item 6).
    "include-fields.docx": build_include_fields_docx,
    "linked-section.docx": build_linked_section_docx,
    "linked-image-unc.docx": build_linked_image_unc_docx,
    "linked-image-http.pptx": build_linked_image_http_pptx,
    "ole-link.docx": build_ole_link_docx,
    "ole-link.pptx": build_ole_link_pptx,
    "xxe.docx": build_xxe_docx,
    "vba-autoopen.docx": build_vba_autoopen_docx,
    "vba-autoopen.pptx": build_vba_autoopen_pptx,
    # The render sanitising fixture and the rendition schema fixtures (section 14.8).
    "active-content.pdf": build_active_content_pdf,
    "schema-presteps-gotor.pdf": build_schema_presteps_gotor_pdf,
    "schema-gotoe-unlisted.pdf": build_schema_gotoe_unlisted_pdf,
    "schema-page-af.pdf": build_schema_page_af_pdf,
    "schema-image-alternates.pdf": build_schema_image_alternates_pdf,
    "schema-form-ref.pdf": build_schema_form_ref_pdf,
    "schema-stream-f.pdf": build_schema_stream_f_pdf,
    "schema-font-unlisted.pdf": build_schema_font_unlisted_pdf,
    "schema-annotation-ap.pdf": build_schema_annotation_ap_pdf,
    "schema-resources-unknown.pdf": build_schema_resources_unknown_pdf,
    # The schema limit fixtures.
    "schema-depth-65.pdf": build_schema_depth_65_pdf,
    "schema-containers-100001.pdf": build_schema_containers_100001_pdf,
    "schema-document-1000001.pdf": build_schema_document_1000001_pdf,
    # The inline dictionary fixtures.
    "schema-inline-af.pdf": build_schema_inline_af_pdf,
    "schema-inline-image-url.pdf": build_schema_inline_image_url_pdf,
    "schema-inline-tagged.pdf": build_schema_inline_tagged_pdf,
    # The whole-document fixture.
    "schema-memo-two-pages.pdf": build_schema_memo_two_pages_pdf,
}

# The user password of the one fixture that needs a password to open.
PASSWORDS: Final = {"encrypted.pdf": "docchat-user"}


def check_pdf(name: str, data: bytes) -> None:
    """Fail when qpdf can't open a written PDF without recovery, can't parse a page's content,
    or warns about it."""
    with pikepdf.open(
        io.BytesIO(data), password=PASSWORDS.get(name, ""), attempt_recovery=False
    ) as pdf:
        for page in pdf.pages:
            pikepdf.parse_content_stream(page)
        warnings = pdf.get_warnings()
    if warnings:
        raise AssertionError(f"{name}: qpdf warns: {warnings}")


def build(names: Iterable[str] | None = None) -> dict[str, bytes]:
    """Build the named fixtures (all of them by default), checking each PDF with qpdf."""
    built: dict[str, bytes] = {}
    for name in names if names is not None else FIXTURES:
        data = FIXTURES[name]()
        if name.endswith(".pdf"):
            check_pdf(name, data)
        built[name] = data
    return built


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write the fixture documents of 84-testing.md 1.5."
    )
    parser.add_argument("--out", type=Path, default=HERE, help="directory to write into")
    parser.add_argument("--only", action="append", choices=sorted(FIXTURES), help="one fixture")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, data in build(args.only).items():
        (args.out / name).write_bytes(data)
        sys.stdout.write(f"{name}: {len(data):,} bytes\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
