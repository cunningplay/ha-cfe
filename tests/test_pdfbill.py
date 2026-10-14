"""Tests for reading the bill from the PDF (anonymized text, synthetic PDFs)."""

from __future__ import annotations

from datetime import date
from io import BytesIO

import pytest

from conftest import ROOT, cfe_parser, cfe_pdfbill

pypdf = pytest.importorskip("pypdf")
pytest.importorskip("fontTools")

RPU = "000111222333"
parse_pdf_text = cfe_pdfbill.parse_pdf_text


# ------------------------------------------------------------ text -> Bill


def test_pdf_text_fields(bill_pdf_text: str) -> None:
    bill = parse_pdf_text(bill_pdf_text, RPU)
    assert bill is not None
    assert bill.source == "pdf"
    assert bill.folio == "000012345678"
    assert bill.rpu == RPU
    assert bill.total == 1128.28
    assert bill.invoice_total == 449.28  # Fac. del Periodo + DSAP
    assert bill.due_date == date(2026, 10, 9)
    assert bill.cutoff_date == date(2026, 10, 10)
    assert (bill.period_start, bill.period_end) == (date(2026, 8, 28), date(2026, 9, 23))
    assert bill.days == 26
    assert bill.kwh == 174
    assert bill.daily_kwh == 6.69
    assert bill.tariff == "1A"
    assert bill.meter_id == "9ZZZ99"
    assert (bill.meter_current, bill.meter_previous) == (174, 0)
    assert bill.subsidy == 75.29
    assert bill.payment_reference == "01 000111222333 261009 000001128 5"
    assert [(t.name, t.kwh, t.price, t.subtotal) for t in bill.tiers] == [
        ("Básico", 9, 1.013, 9.8),
        ("Intermedio", 4, 1.175, 5.68),
        ("Excedente", 5, 4.028, 22.38),
    ]
    assert [(i.name, i.amount) for i in bill.line_items] == [
        ("Energía", 318.24),
        ("IVA  8%", 25.46),
        ("Fac. del Periodo", 343.7),
        ("Deposito", 679.0),
        ("DSAP", 105.58),
        ("Total", 1128.28),
    ]
    assert bill.issued is None and bill.history == []


COMMON = (
    "folio", "rpu", "total", "invoice_total", "due_date", "cutoff_date", "period_start",
    "period_end", "days", "kwh", "daily_kwh", "tariff", "meter_id", "meter_previous",
    "meter_current", "payment_reference", "line_items",
)


def test_pdf_matches_xml_of_same_bill(bill_pdf_text: str, bill_xml: bytes) -> None:
    pdf = parse_pdf_text(bill_pdf_text, RPU).to_dict()
    xml = cfe_parser.parse_bill(bill_xml).to_dict()
    assert {k: pdf[k] for k in COMMON} == {k: xml[k] for k in COMMON}
    assert (pdf["source"], xml["source"]) == ("pdf", "xml")


def test_other_rpu_ignored(bill_pdf_text: str) -> None:
    assert parse_pdf_text(bill_pdf_text, "999999999999") is None
    assert parse_pdf_text(bill_pdf_text).rpu == RPU  # no filter


def test_not_cfe_ignored(bill_pdf_text: str) -> None:
    text = bill_pdf_text.replace("Comisión Federal de Electricidad", "Otra Empresa").replace(
        "CFE 370814QI0", "AAA010101AAA"
    )
    assert parse_pdf_text(text, RPU) is None
    assert parse_pdf_text("hola", RPU) is None
    # the RFC alone is enough
    only_rfc = bill_pdf_text.replace("Comisión Federal de Electricidad", "X")
    assert parse_pdf_text(only_rfc, RPU) is not None


def test_labels_lost_skips(bill_pdf_text: str) -> None:
    # unknown glyphs dropped inside a key label: not a usable bill
    assert parse_pdf_text(bill_pdf_text.replace("NO. DE SERVICIO", "NO. DE SERVIIO"), RPU) is None
    assert parse_pdf_text(bill_pdf_text.replace("Folio:", "Fol:"), RPU) is None


def test_dropped_glyphs_still_parse(bill_pdf_text: str) -> None:
    # "Í" unknown -> "LMITE"; a stray glyph dropped elsewhere
    text = bill_pdf_text.replace("LÍMITE", "LMITE").replace("Apoyo", "Apyo")
    bill = parse_pdf_text(text, RPU)
    assert bill.due_date == date(2026, 10, 9)
    assert bill.subsidy is None


def test_missing_fields_give_none(bill_pdf_text: str) -> None:
    keep = ("NO. DE SERVICIO", "Folio:", "Comisión Federal")
    text = "\n".join(line for line in bill_pdf_text.splitlines() if any(k in line for k in keep))
    bill = parse_pdf_text(text, RPU)
    assert bill is not None and bill.folio == "000012345678"
    for name in ("total", "invoice_total", "due_date", "cutoff_date", "period_start", "kwh",
                 "days", "daily_kwh", "tariff", "meter_id", "subsidy", "payment_reference"):
        assert getattr(bill, name) is None, name
    assert bill.line_items == [] and bill.tiers == []


def test_amount_falls_back_to_total_a_pagar(bill_pdf_text: str) -> None:
    text = bill_pdf_text.replace("Total 1,128.28", "Totl 1,128.28")
    bill = parse_pdf_text(text, RPU)
    assert bill.total == 1128
    assert bill.line_items == []
    assert bill.invoice_total is None


def test_odd_values_give_none(bill_pdf_text: str) -> None:
    text = (
        bill_pdf_text.replace("09 OCT 26", "31 FEB 26")
        .replace("28 AGO 26-23 SEP 26", "23 SEP 26-28 AGO 26")
        .replace("174 0 174", "174 0 x")
    )
    bill = parse_pdf_text(text, RPU)
    assert bill.due_date is None
    assert bill.days is None and bill.daily_kwh is None
    assert bill.kwh is None and bill.meter_current is None


def test_round_trip_and_old_store(bill_pdf_text: str, bill_xml: bytes) -> None:
    bill = parse_pdf_text(bill_pdf_text, RPU)
    assert cfe_parser.Bill.from_dict(bill.to_dict()) == bill
    old = cfe_parser.parse_bill(bill_xml).to_dict()
    del old["source"]  # stored by v0.2.0
    for tier in old["tiers"]:
        del tier["subtotal"]
    restored = cfe_parser.Bill.from_dict(old)
    assert restored.source == "xml" and restored.tiers[0].subtotal is None


# ------------------------------------------------------- synthetic PDF decode


def _font(chars: list[str]) -> tuple[bytes, dict[str, int]]:
    """A TrueType font made here: one distinct outline per character.

    ``á`` is a composite (``a`` + an accent glyph) to check decomposition;
    the space has no outline.
    """
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    order = [".notdef", "accent"] + [f"c{i}" for i in range(len(chars))]
    gid = {c: order.index(f"c{i}") for i, c in enumerate(chars)}
    glyphs = {}
    for i, name in enumerate(order):
        pen = TTGlyphPen(glyphs)
        if name == "accent":
            pen.moveTo((0, 700)); pen.lineTo((50, 800)); pen.lineTo((100, 700)); pen.closePath()
        elif name != ".notdef":
            char = chars[i - 2]
            if char == "á" and "a" in gid:
                pen.addComponent(order[gid["a"]], (1, 0, 0, 1, 0, 0))
                pen.addComponent("accent", (1, 0, 0, 1, 20, 0))
            elif char != " ":
                pen.moveTo((10, 0)); pen.lineTo((10, 100 + i)); pen.lineTo((20 + 3 * i, 100 + i))
                pen.lineTo((20 + 3 * i, 0)); pen.closePath()
        glyphs[name] = pen.glyph()
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({ord(c): order[g] for c, g in gid.items()})
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({name: (500, 0) for name in order})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "Synthetic", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    out = BytesIO()
    fb.save(out)
    return out.getvalue(), gid


def _table(font_bytes: bytes, gid: dict[str, int], skip: str = "") -> dict[str, str]:
    """The injected hash table for the synthetic font (``skip``: left unknown)."""
    from fontTools.ttLib import TTFont

    font = TTFont(BytesIO(font_bytes))
    glyph_set = font.getGlyphSet()
    order = font.getGlyphOrder()
    table = {}
    for char, g in gid.items():
        digest = cfe_pdfbill.glyph_hash(glyph_set, order[g])
        if digest and char not in skip:
            table[digest] = char
    return table


def _pdf(lines: list[str], font_bytes: bytes, gid: dict[str, int], encrypt: bool = False) -> bytes:
    """One page drawing ``lines`` with a Type0 font whose ToUnicode is wrong.

    The second half of the lines is drawn inside a Form XObject.
    """
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        NumberObject,
        TextStringObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(600, 1000)
    n = NameObject

    def stream(data: bytes, **entries) -> DecodedStreamObject:
        obj = DecodedStreamObject()
        obj.set_data(data)
        obj.update({n(k): v for k, v in entries.items()})
        return obj

    font_file = writer._add_object(stream(font_bytes, **{"/Length1": NumberObject(len(font_bytes))}))
    descriptor = writer._add_object(DictionaryObject({
        n("/Type"): n("/FontDescriptor"), n("/FontName"): n("/AAAAAB+font0001"),
        n("/Flags"): NumberObject(32), n("/ItalicAngle"): NumberObject(0),
        n("/FontBBox"): ArrayObject([NumberObject(v) for v in (0, -200, 1000, 800)]),
        n("/Ascent"): NumberObject(800), n("/Descent"): NumberObject(-200),
        n("/CapHeight"): NumberObject(700), n("/StemV"): NumberObject(80),
        n("/FontFile2"): font_file,
    }))
    cid_font = writer._add_object(DictionaryObject({
        n("/Type"): n("/Font"), n("/Subtype"): n("/CIDFontType2"), n("/BaseFont"): n("/AAAAAB+font0001"),
        n("/CIDSystemInfo"): DictionaryObject({n("/Registry"): TextStringObject("Adobe"),
                                               n("/Ordering"): TextStringObject("Identity"),
                                               n("/Supplement"): NumberObject(0)}),
        n("/FontDescriptor"): descriptor, n("/DW"): NumberObject(500), n("/CIDToGIDMap"): n("/Identity"),
    }))
    # Wrong ToUnicode, like CFE's: every glyph id maps to an unrelated letter.
    wrong = "".join(f"<{g:04X}> <{0x41 + g % 26:04X}>\n" for g in range(1, max(gid.values()) + 1))
    to_unicode = writer._add_object(stream(
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
        + f"{max(gid.values())} beginbfchar\n{wrong}endbfchar\n".encode()
        + b"endcmap CMapName currentdict /CMap defineresource pop end end"
    ))
    font = writer._add_object(DictionaryObject({
        n("/Type"): n("/Font"), n("/Subtype"): n("/Type0"), n("/BaseFont"): n("/AAAAAB+font0001"),
        n("/Encoding"): n("/Identity-H"), n("/DescendantFonts"): ArrayObject([cid_font]),
        n("/ToUnicode"): to_unicode,
    }))

    def draw(chunk: list[str], top: int) -> bytes:
        ops = []
        for row, line in enumerate(chunk):
            hexed = "".join(f"{gid[c]:04X}" for c in line)
            ops.append(f"BT /F1 8 Tf 1 0 0 1 10 {top - 12 * row} Tm <{hexed}> Tj ET")
        return "\n".join(ops).encode()

    half = len(lines) // 2
    fonts = DictionaryObject({n("/F1"): font})
    form = writer._add_object(stream(
        draw(lines[half:], 980 - 12 * half),
        **{"/Type": n("/XObject"), "/Subtype": n("/Form"),
           "/BBox": ArrayObject([FloatObject(v) for v in (0, 0, 600, 1000)]),
           "/Resources": DictionaryObject({n("/Font"): fonts})},
    ))
    page[n("/Resources")] = DictionaryObject({
        n("/Font"): fonts, n("/XObject"): DictionaryObject({n("/X1"): form}),
    })
    page[n("/Contents")] = writer._add_object(stream(draw(lines[:half], 980) + b"\n/X1 Do"))
    if encrypt:
        writer.encrypt("secret")
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture
def synthetic(bill_pdf_text: str):
    lines = [line for line in bill_pdf_text.splitlines() if line.strip()]
    chars = sorted({c for line in lines for c in line} | {"a"})
    font_bytes, gid = _font(chars)
    return lines, font_bytes, gid


def test_decode_synthetic_pdf(synthetic, bill_pdf_text, monkeypatch) -> None:
    lines, font_bytes, gid = synthetic
    data = _pdf(lines, font_bytes, gid)
    # Without the rebuilt maps the text is gibberish.
    plain = pypdf.PdfReader(BytesIO(data)).pages[0].extract_text()
    assert "NO. DE SERVICIO" not in plain
    monkeypatch.setattr(cfe_pdfbill, "glyph_table", lambda: _table(font_bytes, gid))
    text = cfe_pdfbill.pdf_text(data)
    assert "NO. DE SERVICIO:000111222333" in text
    assert "Básico 9 1.013 9.80" in text  # composite á decomposed
    assert "Total 1,128.28" in text  # drawn inside the Form XObject
    assert cfe_pdfbill.parse_pdf_bill(data, RPU) == parse_pdf_text(bill_pdf_text, RPU)
    assert cfe_pdfbill.parse_pdf_bill(data, "999999999999") is None


def test_unknown_glyphs_dropped(synthetic, monkeypatch, caplog) -> None:
    lines, font_bytes, gid = synthetic
    data = _pdf(lines, font_bytes, gid)
    monkeypatch.setattr(cfe_pdfbill, "glyph_table", lambda: _table(font_bytes, gid, skip="JÍ"))
    text = cfe_pdfbill.pdf_text(data)
    assert "LMITE DE PAGO" in text
    assert "glyph(s) not recognized" in caplog.text
    bill = cfe_pdfbill.parse_pdf_bill(data, RPU)
    assert bill.due_date == date(2026, 10, 9) and bill.total == 1128.28
    # a glyph used by a key label: the PDF is not taken as a bill
    monkeypatch.setattr(cfe_pdfbill, "glyph_table", lambda: _table(font_bytes, gid, skip="V"))
    assert cfe_pdfbill.parse_pdf_bill(data, RPU) is None


@pytest.mark.parametrize("data", [b"", b"<xml/>", b"%PDF-1.4\n%%garbage", b"%PDF-1.7\n1 0 obj\n<<"])
def test_broken_pdf_raises(data) -> None:
    with pytest.raises(cfe_parser.BillParseError):
        cfe_pdfbill.parse_pdf_bill(data, RPU)


def test_encrypted_pdf_raises(synthetic) -> None:
    lines, font_bytes, gid = synthetic
    with pytest.raises(cfe_parser.BillParseError, match="encrypted"):
        cfe_pdfbill.parse_pdf_bill(_pdf(lines[:3], font_bytes, gid, encrypt=True), RPU)


def test_shipped_glyph_table() -> None:
    table = cfe_pdfbill.glyph_table()
    assert len(table) > 1000
    chars = set(table.values())
    assert set("0123456789$.,:-%/()") <= chars
    assert set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz") <= chars
    assert set("áéíóúñÁÉÍÓÚÑ–—•¡¿°") <= chars
    assert all(len(k) == 32 for k in table)


# ------------------------------------------------- real bill (local only)

SAMPLES = ROOT / "samples"


@pytest.mark.skipif(not list(SAMPLES.glob("*.pdf")), reason="no local samples/*.pdf")
def test_real_samples() -> None:
    """Every local PDF decodes; with the same bill's XML, the key fields agree."""
    xml_bills = {}
    for path in SAMPLES.glob("*.xml"):
        bill = cfe_parser.parse_bill(path.read_bytes())
        if bill is not None:
            xml_bills[bill.folio] = bill
    for path in SAMPLES.glob("*.pdf"):
        bill = cfe_pdfbill.parse_pdf_bill(path.read_bytes())
        assert bill is not None, path.name
        assert bill.rpu and bill.total and bill.due_date and bill.period_end, path.name
        xml = xml_bills.get(bill.folio)
        if xml is None:
            continue
        keys = COMMON + ("tariff",)
        assert {k: getattr(bill, k) for k in keys} == {k: getattr(xml, k) for k in keys}, path.name
