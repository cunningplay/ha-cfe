"""Parse CFE (Comisión Federal de Electricidad) CFDI 4.0 bill XML.

The :class:`Bill` dataclass is shared with ``pdfbill`` (bills read from the PDF).

Pure module: no Home Assistant imports, so it can be unit-tested on its own.
Every field is optional; a missing or odd value gives None, never a crash.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import math
import re
from typing import Any
import xml.etree.ElementTree as ET

CFE_RFC = "CFE370814QI0"
CFDI_NS = "http://www.sat.gob.mx/cfd/4"

_MONTHS = {
    "ENE": 1,
    "FEB": 2,
    "MAR": 3,
    "ABR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AGO": 8,
    "SEP": 9,
    "SET": 9,
    "OCT": 10,
    "NOV": 11,
    "DIC": 12,
}
_SPANISH_DATE = re.compile(r"^\s*(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{2}|\d{4})\s*$")


class BillParseError(ValueError):
    """The attachment cannot be read (malformed XML, broken or encrypted PDF)."""


@dataclass
class LineItem:
    """One row of the bill's charges table (Energía, IVA, DAP, Total...)."""

    name: str
    amount: float | None


@dataclass
class Tier:
    """One consumption tier (Básico, Intermedio, Excedente)."""

    name: str | None
    kwh: float | None
    price: float | None
    subtotal: float | None = None  # PDF only (kWh x price)


@dataclass
class HistoryPoint:
    """One row of the consumption history (CONSUMOHn)."""

    year: int
    month: int
    kwh: float | None


@dataclass
class Bill:
    """A parsed CFE bill."""

    folio: str
    serie: str | None = None
    issued: datetime | None = None
    rpu: str | None = None
    tariff: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    days: int | None = None
    kwh: float | None = None
    daily_kwh: float | None = None
    daily_cost: float | None = None
    meter_id: str | None = None
    meter_previous: int | None = None
    meter_current: int | None = None
    total: float | None = None
    invoice_total: float | None = None
    line_items: list[LineItem] = field(default_factory=list)
    tiers: list[Tier] = field(default_factory=list)
    subsidy: float | None = None
    real_cost: float | None = None
    due_date: date | None = None
    cutoff_date: date | None = None
    payment_reference: str | None = None
    summer_start: str | None = None
    summer_end: str | None = None
    history: list[HistoryPoint] = field(default_factory=list)
    source: str = "xml"  # "xml" (CFDI) or "pdf" (read from the printed bill)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-safe primitives (dates as ISO strings)."""
        data = asdict(self)
        for key in _DATE_FIELDS:
            if data[key] is not None:
                data[key] = data[key].isoformat()
        if data["issued"] is not None:
            data["issued"] = data["issued"].isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Bill:
        """Rebuild a Bill from :meth:`to_dict` output (unknown keys ignored)."""
        known = set(cls.__dataclass_fields__)
        values = {k: v for k, v in data.items() if k in known}
        for key in _DATE_FIELDS:
            values[key] = _iso_date(values.get(key))
        issued = values.get("issued")
        try:
            values["issued"] = datetime.fromisoformat(issued) if issued else None
        except (TypeError, ValueError):
            values["issued"] = None
        values["line_items"] = [LineItem(**i) for i in values.get("line_items") or []]
        values["tiers"] = [Tier(**t) for t in values.get("tiers") or []]
        if values.get("source") not in ("xml", "pdf"):
            values["source"] = "xml"  # stored before v0.3.0: always XML
        values["history"] = [HistoryPoint(**h) for h in values.get("history") or []]
        return cls(**values)


_DATE_FIELDS = ("period_start", "period_end", "due_date", "cutoff_date")


def _iso_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def parse_spanish_date(value: str | None) -> date | None:
    """Parse CFE's short Spanish dates, e.g. ``09 OCT 26`` -> 2026-10-09."""
    if not value:
        return None
    match = _SPANISH_DATE.match(value)
    if not match:
        return None
    day, mon, year = match.groups()
    month = _MONTHS.get(mon.upper())
    if month is None:
        return None
    year_num = int(year)
    if year_num < 100:
        year_num += 2000
    try:
        return date(year_num, month, int(day))
    except ValueError:
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip().replace(",", "")
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _int(value: str | None) -> int | None:
    number = _float(value)
    if number is None:
        return None
    return int(number)


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _children(elem: ET.Element | None) -> dict[str, str]:
    """Map local tag name -> text for the direct children of ``elem``."""
    if elem is None:
        return {}
    return {_local(child.tag): (child.text or "") for child in elem}


def _find_child(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem:
        if _local(child.tag) == name:
            return child
    return None


def parse_bill(data: bytes) -> Bill | None:
    """Parse a CFDI XML attachment.

    Returns None when the XML is not a CFE bill (another issuer, or no CFE
    addenda). Raises BillParseError when the bytes are not well-formed XML.
    """
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data:
        # CFDI never uses DTDs; refuse them rather than expand entities.
        raise BillParseError("XML with DTD/entities is not accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as err:
        raise BillParseError(str(err)) from err

    if _local(root.tag) != "Comprobante":
        return None
    issuer = _find_child(root, "Emisor")
    if issuer is None or (issuer.get("Rfc") or "").strip().upper() != CFE_RFC:
        return None

    addenda = _find_child(root, "Addenda")
    reg = _find_child(addenda, "clsRegArchFact") if addenda is not None else None
    if reg is None:
        return None
    fields = _children(reg)

    def get(name: str) -> str | None:
        return fields.get(name)

    folio = _text(root.get("Folio")) or _text(get("FE_FOLIO"))
    if not folio:
        return None

    rpu = _text(get("RPU"))
    if rpu is None:
        cfe = _find_child(addenda, "CFE")
        node = _find_child(cfe, "ComisionFederalElectricidad") if cfe is not None else None
        rpu = _text(node.get("RPU")) if node is not None else None

    issued: datetime | None
    try:
        issued = datetime.fromisoformat(root.get("Fecha", "").strip())
    except ValueError:
        issued = None

    total = _float(get("CargosCreditos"))
    if total is None:
        total = _float(get("IMPTOTAL"))

    # Charges table: ConceptoN (names) pairs with ImporteN (amounts).
    names = _children(_find_child(reg, "Conceptos"))
    amounts = _children(_find_child(reg, "Importes"))
    line_items = []
    for n in range(1, 100):
        name = _text(names.get(f"Concepto{n}"))
        if name is None:
            break
        line_items.append(LineItem(name=name, amount=_float(amounts.get(f"Importe{n}"))))

    # Consumption tiers: names from DetallesColumna1/DetalleColumnaN.
    tier_names = _children(_find_child(reg, "DetallesColumna1"))
    tiers = []
    for n in range(1, 100):
        kwh = get(f"KWH_ESCALON1_{n}")
        price = get(f"PRECIO_ESCALON1_{n}")
        if kwh is None and price is None:
            break
        tiers.append(
            Tier(
                name=_text(tier_names.get(f"DetalleColumna{n}")),
                kwh=_float(kwh),
                price=_float(price),
            )
        )

    history = []
    for n in range(1, 100):
        year = get(f"YEARH{n}")
        if year is None:
            break
        year_num = _int(year)
        month_num = _int(get(f"MESH{n}"))
        if not year_num or not month_num:
            continue  # empty slot, e.g. YEARH1=0000 on a new service
        history.append(
            HistoryPoint(year=year_num, month=month_num, kwh=_float(get(f"CONSUMOH{n}")))
        )

    return Bill(
        folio=folio,
        serie=_text(root.get("Serie")),
        issued=issued,
        rpu=rpu,
        tariff=_text(get("TARIFA")),
        period_start=parse_spanish_date(get("FECDESDE")),
        period_end=parse_spanish_date(get("FECHASTA")),
        days=_int(get("Dias")),
        kwh=_float(get("CONSUMO_R")),
        daily_kwh=_float(get("ConsumoDiario")),
        daily_cost=_float(get("PrecioDiario")),
        meter_id=_text(get("NUMMED1")),
        meter_previous=_int(get("LECANT1")),
        meter_current=_int(get("LECACT1")),
        total=total,
        invoice_total=_float(root.get("Total")),
        line_items=line_items,
        tiers=tiers,
        subsidy=_float(get("AportacionGub")),
        real_cost=_float(get("IMPENER_REAL")),
        due_date=parse_spanish_date(get("FECLIMITE")),
        cutoff_date=parse_spanish_date(get("FECORTE")),
        payment_reference=_text(get("LineaDeReferencia")),
        summer_start=_text(get("VERINV_INI")),
        summer_end=_text(get("VERINV_TER")),
        history=history,
    )
