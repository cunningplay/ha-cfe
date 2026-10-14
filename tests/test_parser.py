"""Tests for the CFE bill parser (anonymized fixture only)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from conftest import cfe_mail, cfe_parser

parse_bill = cfe_parser.parse_bill
parse_spanish_date = cfe_parser.parse_spanish_date


def test_cfe_bill(bill_xml: bytes) -> None:
    bill = parse_bill(bill_xml)
    assert bill is not None
    assert bill.folio == "000012345678"
    assert bill.serie == "AA"
    assert bill.issued == datetime(2026, 9, 25, 9, 41, 25)
    assert bill.rpu == "000111222333"
    assert bill.total == 1128.28
    assert bill.invoice_total == 449.28
    assert bill.kwh == 174
    assert bill.due_date == date(2026, 10, 9)
    assert bill.cutoff_date == date(2026, 10, 10)
    assert bill.period_start == date(2026, 8, 28)
    assert bill.period_end == date(2026, 9, 23)
    assert bill.days == 26
    assert bill.tariff == "1A"
    assert bill.daily_kwh == 6.69
    assert bill.daily_cost == 43.38
    assert bill.meter_id == "9ZZZ99"
    assert (bill.meter_previous, bill.meter_current) == (0, 174)
    assert bill.subsidy == 265.29
    assert bill.real_cost == 583.53
    assert bill.payment_reference == "01 000111222333 261009 000001128 5"
    assert (bill.summer_start, bill.summer_end) == ("mayo", "octubre")
    assert bill.history == []


def test_line_items(bill_xml: bytes) -> None:
    bill = parse_bill(bill_xml)
    assert [(i.name, i.amount) for i in bill.line_items] == [
        ("Energía", 318.24),
        ("IVA  8%", 25.46),
        ("Fac. del Periodo", 343.7),
        ("Deposito", 679.0),
        ("DSAP", 105.58),
        ("Total", 1128.28),
    ]


def test_tiers(bill_xml: bytes) -> None:
    bill = parse_bill(bill_xml)
    assert [(t.name, t.kwh, t.price) for t in bill.tiers] == [
        ("Básico", 9.68, 1.013),
        ("Intermedio", 4.84, 1.175),
        ("Excedente", 5.5571, 4.028),
    ]


def test_history_rows_skip_zero_year(bill_xml: bytes) -> None:
    xml = bill_xml.replace(
        b"<YEARH1>0000</YEARH1><MESH1>00</MESH1><CONSUMOH1>0</CONSUMOH1>",
        b"<YEARH1>2026</YEARH1><MESH1>07</MESH1><CONSUMOH1>150</CONSUMOH1>",
    )
    bill = parse_bill(xml)
    assert [(h.year, h.month, h.kwh) for h in bill.history] == [(2026, 7, 150.0)]


def test_other_issuer_ignored(bill_xml: bytes) -> None:
    assert parse_bill(bill_xml.replace(b'Rfc="CFE370814QI0"', b'Rfc="AAA010101AAA"')) is None


def test_other_rpu_is_reported(bill_xml: bytes) -> None:
    # RPU filtering happens in the coordinator; the parser reports the RPU.
    bill = parse_bill(bill_xml.replace(b"000111222333", b"999999999999"))
    assert bill.rpu == "999999999999"


def test_non_cfdi_xml_ignored() -> None:
    assert parse_bill(b"<?xml version='1.0'?><root><a>1</a></root>") is None


def test_cfdi_without_addenda_ignored() -> None:
    xml = (
        b'<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" Folio="1">'
        b'<cfdi:Emisor Rfc="CFE370814QI0"/></cfdi:Comprobante>'
    )
    assert parse_bill(xml) is None


def test_malformed_xml_raises() -> None:
    with pytest.raises(cfe_parser.BillParseError):
        parse_bill(b"<cfdi:Comprobante><unclosed>")


def test_dtd_rejected() -> None:
    xml = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
    with pytest.raises(cfe_parser.BillParseError):
        parse_bill(xml)


def test_missing_fields_give_none(bill_xml: bytes) -> None:
    for tag in (b"FECLIMITE", b"CONSUMO_R", b"TARIFA", b"CargosCreditos"):
        start = bill_xml.index(b"<" + tag + b">")
        end = bill_xml.index(b"</" + tag + b">") + len(tag) + 3
        bill_xml = bill_xml[:start] + bill_xml[end:]
    bill = parse_bill(bill_xml)
    assert bill.due_date is None
    assert bill.kwh is None
    assert bill.tariff is None
    assert bill.total == 1128  # falls back to IMPTOTAL


def test_garbage_values_give_none(bill_xml: bytes) -> None:
    bill = parse_bill(
        bill_xml.replace(b"<Dias>26</Dias>", b"<Dias>n/a</Dias>").replace(
            b"<FECORTE>10 OCT 26</FECORTE>", b"<FECORTE>31 FEB 26</FECORTE>"
        )
    )
    assert bill.days is None
    assert bill.cutoff_date is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("09 OCT 26", date(2026, 10, 9)),
        ("28 AGO 26", date(2026, 8, 28)),
        ("1 ENE 2027", date(2027, 1, 1)),
        ("15 dic 26", date(2026, 12, 15)),
        ("05 SET 26", date(2026, 9, 5)),
        ("10 ABR 26", date(2026, 4, 10)),
        ("", None),
        (None, None),
        ("2026-10-09", None),
        ("09 XYZ 26", None),
    ],
)
def test_spanish_dates(text, expected) -> None:
    assert parse_spanish_date(text) == expected


def test_round_trip(bill_xml: bytes) -> None:
    bill = parse_bill(bill_xml)
    assert cfe_parser.Bill.from_dict(bill.to_dict()) == bill


def test_mail_finds_forwarded_bill_attachments(bill_xml: bytes) -> None:
    from email.message import EmailMessage

    inner = EmailMessage()
    inner["Subject"] = "Recibo CFE"
    inner.set_content("adjunto")
    inner.add_attachment(bill_xml, maintype="application", subtype="xml", filename="AA-1.XML")
    inner.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="AA-1.pdf")
    outer = EmailMessage()
    outer["Subject"] = "Fwd: Recibo CFE"
    outer.set_content("fwd")
    outer.add_attachment(inner)
    found = cfe_mail._attachments(outer.as_bytes())
    assert found == [("xml", bill_xml), ("pdf", b"%PDF")]


def test_non_finite_numbers_give_none(bill_xml: bytes) -> None:
    bill = parse_bill(
        bill_xml.replace(b"<Dias>26</Dias>", b"<Dias>nan</Dias>").replace(
            b"<CONSUMO_R>174</CONSUMO_R>", b"<CONSUMO_R>inf</CONSUMO_R>"
        )
    )
    assert bill.days is None
    assert bill.kwh is None
