"""Read a CFE bill from its PDF when the email has no CFDI XML.

CFE's PDFs embed subset Arial fonts whose ToUnicode maps still use the original
glyph ids, so plain text extraction gives gibberish. Each embedded font's
character map is rebuilt by hashing every glyph outline and looking the hash up
in ``glyphs.json`` (Arial / Arial Bold / Arial Black / Arial Italic outline
hashes -> characters, made by ``tools/make_glyph_table.py``). The text is then
extracted with the rebuilt maps and parsed into the same :class:`Bill` the XML
gives (``source="pdf"``).

No Home Assistant imports. pypdf and fontTools are imported lazily.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import cache
import hashlib
from io import BytesIO
import json
import logging
from pathlib import Path
import re
from typing import Any

from .parser import Bill, BillParseError, LineItem, Tier, _float, _int, parse_spanish_date

_LOGGER = logging.getLogger(__name__)

GLYPHS_FILE = Path(__file__).resolve().parent / "glyphs.json"
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_GLYPHS = 4000  # CFE's subset fonts have ~100
_UNKNOWN = "\ufffd"  # mapped for unrecognized glyphs, then removed from the text


@cache
def glyph_table() -> dict[str, str]:
    """Outline hash -> character (shipped table)."""
    return json.loads(GLYPHS_FILE.read_text(encoding="utf-8"))


def glyph_hash(glyph_set: Any, name: str) -> str | None:
    """md5 of the decomposed outline; None for an empty glyph."""
    from fontTools.pens.recordingPen import DecomposingRecordingPen

    pen = DecomposingRecordingPen(glyph_set)
    glyph_set[name].draw(pen)
    if not pen.value:
        return None
    return hashlib.md5(repr(pen.value).encode()).hexdigest()


# ------------------------------------------------------------------ decoding


def _resources(obj: Any, seen: set[int]) -> Iterator[Any]:
    """Font dicts in a resources dict and its Form XObjects (recursively)."""
    resources = obj.get_object() if obj is not None else None
    if not resources or id(resources) in seen:
        return
    seen.add(id(resources))
    fonts = resources.get("/Font")
    fonts = fonts.get_object() if fonts is not None else None
    for ref in (fonts or {}).values():
        yield ref.get_object()
    xobjects = resources.get("/XObject")
    xobjects = xobjects.get_object() if xobjects is not None else None
    for ref in (xobjects or {}).values():
        xobj = ref.get_object()
        if xobj.get("/Subtype") == "/Form":
            yield from _resources(xobj.get("/Resources"), seen)


def _cid_to_gid(descendant: Any) -> dict[int, int] | None:
    """CID -> GID from /CIDToGIDMap; None means Identity."""
    mapping = descendant.get("/CIDToGIDMap")
    if mapping is None:
        return None
    mapping = mapping.get_object()
    if not hasattr(mapping, "get_data"):
        return None  # /Identity
    data = mapping.get_data()
    return {
        cid: (data[2 * cid] << 8) | data[2 * cid + 1] for cid in range(len(data) // 2)
    }


def _cmap_stream(chars: dict[int, str]) -> bytes:
    """A ToUnicode CMap (2-byte codes) for ``code -> text``."""
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    items = sorted(chars.items())
    for start in range(0, len(items), 100):
        chunk = items[start : start + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        lines.extend(
            f"<{code:04X}> <{text.encode('utf-16-be').hex().upper()}>"
            for code, text in chunk
        )
        lines.append("endbfchar")
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(lines).encode()


def _fix_font(font: Any, table: dict[str, str]) -> tuple[int, int]:
    """Replace a Type0 font's /ToUnicode by outline matching.

    Returns (glyphs matched, glyphs unknown). Fonts that are not Type0 with an
    embedded TrueType program are left alone.
    """
    from fontTools.ttLib import TTFont
    from pypdf.generic import DecodedStreamObject, NameObject

    if font.get("/Subtype") != "/Type0":
        return 0, 0
    descendants = font.get("/DescendantFonts")
    if not descendants:
        return 0, 0
    descendant = descendants.get_object()[0].get_object()
    descriptor = descendant.get("/FontDescriptor")
    descriptor = descriptor.get_object() if descriptor is not None else None
    program = descriptor.get("/FontFile2") if descriptor else None
    if program is None:
        return 0, 0
    ttf = TTFont(BytesIO(program.get_object().get_data()), lazy=True)
    order = ttf.getGlyphOrder()
    if len(order) > MAX_GLYPHS:
        return 0, 0  # not a subset font like CFE's: leave it alone
    glyph_set = ttf.getGlyphSet()
    by_gid: dict[int, str] = {}
    unknown = 0
    for gid, name in enumerate(order):
        if gid == 0:
            continue
        try:
            digest = glyph_hash(glyph_set, name)
        except Exception:  # noqa: BLE001 - one odd glyph must not sink the font
            digest = ""
        if digest is None:
            by_gid[gid] = " "  # no outline: a space
        elif digest in table:
            by_gid[gid] = table[digest]
        else:
            by_gid[gid] = _UNKNOWN  # otherwise pypdf falls back to a wrong letter
            unknown += 1
    cid_map = _cid_to_gid(descendant)
    if cid_map is None:
        chars = by_gid
    else:
        chars = {cid: by_gid[gid] for cid, gid in cid_map.items() if gid in by_gid}
    stream = DecodedStreamObject()
    stream.set_data(_cmap_stream(chars))
    font[NameObject("/ToUnicode")] = stream
    return len(by_gid) - unknown, unknown


def pdf_text(data: bytes) -> str:
    """Text of every page, with the fonts' character maps rebuilt.

    Raises BillParseError when the PDF cannot be read (broken, encrypted, too big).
    """
    from pypdf import PdfReader

    if len(data) > MAX_PDF_BYTES:
        raise BillParseError("PDF too large")
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise BillParseError("not a PDF")
    table = glyph_table()
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise BillParseError("encrypted PDF")
        fixed: list[Any] = []  # font dicts already rebuilt (shared across pages)
        unknown = 0
        pages = []
        for page in reader.pages:
            for font in _resources(page.get("/Resources"), set()):
                if any(font is done for done in fixed):
                    continue
                fixed.append(font)
                unknown += _fix_font(font, table)[1]
            pages.append(page.extract_text() or "")
    except BillParseError:
        raise
    except Exception as err:  # noqa: BLE001 - pypdf/fontTools raise many kinds
        raise BillParseError(f"unreadable PDF: {err!r}") from err
    if unknown:
        _LOGGER.warning("CFE PDF: %d glyph(s) not recognized were dropped", unknown)
    return "\n".join(pages).replace(_UNKNOWN, "")


# ------------------------------------------------------------------ parsing

_DATE = r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{2,4})"
_AMOUNT = r"(-?[\d,]+\.\d{2})"
_ISSUER = re.compile(
    r"CFE\s*370814\s*QI0|Comisi[oó]n\s+Federal\s+de\s+Electricidad", re.IGNORECASE
)
_RPU = re.compile(r"NO\.\s*DE\s*SERVICIO:\s*(\d{12})")
_DUE = re.compile(r"L[IÍ]?MITE\s+DE\s+PAGO:\s*" + _DATE)
_CUTOFF = re.compile(r"CORTE\s+A\s+PARTIR(?:\s+DEL)?:?\s*" + _DATE, re.IGNORECASE)
_PERIOD = re.compile(r"PERIODO\s+FACTURADO:\s*" + _DATE + r"\s*-\s*" + _DATE)
# "TARIFA:1ANO. MEDIDOR:..." (the two labels can run together)
_TARIFF = re.compile(r"TARIFA:\s*([0-9A-Z]+?)\s*(?:NO\.\s*MEDIDOR|$)", re.MULTILINE)
_METER = re.compile(r"NO\.\s*MEDIDOR:\s*([0-9A-Z]+?)\s*(?:MULTIPLICADOR|$)", re.MULTILINE)
_MULTIPLIER = re.compile(r"MULTIPLICADOR:\s*(\d+)")
_TO_PAY = re.compile(r"TOTAL\s+A\s+PAGAR:\s*\$?\s*([\d,]+(?:\.\d{2})?)")
_READING = re.compile(r"^\s*([\d,]+)\s+([\d,]+)\s+([\d,]+)\s*$", re.MULTILINE)
_TIER = re.compile(
    r"^\s*(B[aá]sico|Intermedio(?:\s+(?:bajo|alto))?|Excedente)\s+"
    r"([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_ITEM = re.compile(r"^\s*(\S.*?)\s+\$?" + _AMOUNT + r"\s*$")
_SUBSIDY = re.compile(r"Apoyo\s+Gubernamental\s+\$?" + _AMOUNT)
_REFERENCE = re.compile(r"\b(\d{2}\s\d{12}\s\d{6}\s\d{9}\s\d)\b")
_FOLIO = re.compile(r"\bFolio:\s*(\d{6,})")


def _match(pattern: re.Pattern[str], text: str, group: int = 1) -> str | None:
    found = pattern.search(text)
    return found.group(group).strip() if found else None


def _line_item(line: str) -> LineItem | None:
    """``Name 123.45`` with exactly one amount (not a multi-column table row)."""
    found = _ITEM.match(line)
    if not found:
        return None
    name = found.group(1)
    if re.search(r"\d\.\d{2}\b", name) or not re.search(r"[A-Za-zÀ-ÿ]", name):
        return None
    return LineItem(name=name, amount=_float(found.group(2)))


def _line_items(text: str) -> list[LineItem]:
    """The breakdown block ending in ``Total`` (Energía, IVA, ..., Total)."""
    block: list[LineItem] = []
    best: list[LineItem] = []
    for line in text.splitlines():
        item = _line_item(line)
        if item is None:
            block = []
            continue
        block.append(item)
        if item.name.lower() == "total":
            best = block
            block = []
    return best


def _reading(text: str, multiplier: int) -> tuple[int, int, float] | None:
    """(current, previous, kWh) from the meter row, e.g. ``174 0 174``."""
    rows = [
        (_int(m.group(1)), _int(m.group(2)), _float(m.group(3)))
        for m in _READING.finditer(text)
    ]
    rows = [r for r in rows if None not in r]
    for current, previous, kwh in rows:
        if current >= previous and (current - previous) * multiplier == kwh:
            return current, previous, kwh
    return rows[0] if rows else None  # type: ignore[return-value]


def is_cfe_text(text: str) -> bool:
    """The decoded text names CFE as the issuer."""
    return bool(_ISSUER.search(text))


def parse_pdf_text(text: str, rpu: str | None = None) -> Bill | None:
    """Parse the decoded text of a CFE bill PDF.

    Returns None when the text is not a CFE bill, has no service number or
    folio, or (with ``rpu``) is for another service. Missing fields are None.
    """
    if not is_cfe_text(text):
        return None
    bill_rpu = _match(_RPU, text)
    if bill_rpu is None or (rpu is not None and bill_rpu != rpu):
        return None
    folio = _match(_FOLIO, text)
    if folio is None:
        return None

    period = _PERIOD.search(text)
    start = parse_spanish_date(period.group(1)) if period else None
    end = parse_spanish_date(period.group(2)) if period else None
    days = (end - start).days if start and end and end > start else None

    multiplier = _int(_match(_MULTIPLIER, text)) or 1
    reading = _reading(text, multiplier)
    current, previous, kwh = reading if reading else (None, None, None)

    items = _line_items(text)
    named = {i.name.lower(): i.amount for i in items}
    total = named.get("total")
    if total is None:
        total = _float(_match(_TO_PAY, text))
    period_charge = next(
        (i.amount for i in items if i.name.lower().startswith("fac. del periodo")), None
    )
    dap = next((i.amount for i in items if i.name.upper() in ("DSAP", "DAP")), None)
    invoice_total = (
        round(period_charge + (dap or 0.0), 2) if period_charge is not None else None
    )

    tiers = [
        Tier(
            name=m.group(1),
            kwh=_float(m.group(2)),
            price=_float(m.group(3)),
            subtotal=_float(m.group(4)),
        )
        for m in _TIER.finditer(text)
    ]

    return Bill(
        folio=folio,
        rpu=bill_rpu,
        tariff=_match(_TARIFF, text),
        period_start=start,
        period_end=end,
        days=days,
        kwh=kwh,
        daily_kwh=round(kwh / days, 2) if kwh is not None and days else None,
        meter_id=_match(_METER, text),
        meter_previous=previous,
        meter_current=current,
        total=total,
        invoice_total=invoice_total,
        line_items=items,
        tiers=tiers,
        subsidy=_float(_match(_SUBSIDY, text)),
        due_date=parse_spanish_date(_match(_DUE, text)),
        cutoff_date=parse_spanish_date(_match(_CUTOFF, text)),
        payment_reference=_match(_REFERENCE, text),
        source="pdf",
    )


def parse_pdf_bill(data: bytes, rpu: str | None = None) -> Bill | None:
    """Parse a CFE bill PDF (see :func:`parse_pdf_text`).

    Raises BillParseError when the PDF cannot be read (broken, encrypted).
    Blocking (fontTools, pypdf): run it in an executor.
    """
    return parse_pdf_text(pdf_text(data), rpu)
