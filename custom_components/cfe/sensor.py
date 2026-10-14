"""Sensors for the latest CFE bill."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CURRENCY
from .coordinator import CfeConfigEntry, CfeCoordinator
from .entity import CfeEntity
from .parser import Bill

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class CfeSensorDescription(SensorEntityDescription):
    """Sensor description with a value getter on the bill."""

    value_fn: Callable[[Bill], float | int | str | date | None]
    attrs_fn: Callable[[Bill], dict[str, Any]] | None = None


def _money(key: str, fn: Callable[[Bill], float | None], **kw: Any) -> CfeSensorDescription:
    return CfeSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=fn,
        **kw,
    )


def _date(key: str, fn: Callable[[Bill], date | None]) -> CfeSensorDescription:
    return CfeSensorDescription(
        key=key, translation_key=key, device_class=SensorDeviceClass.DATE, value_fn=fn
    )


SENSORS: tuple[CfeSensorDescription, ...] = (
    _money(
        "amount_due",
        lambda b: b.total,
        attrs_fn=lambda b: {
            "folio": b.folio,
            "source": b.source,
            # PDF bills: the printed "Apoyo Gubernamental" (the subsidy sensor
            # stays empty; it is the XML's AportacionGub, a different figure)
            "apoyo_impreso": b.printed_subsidy,
            "payment_reference": b.payment_reference,
            "invoice_total": b.invoice_total,
            "line_items": [{"name": i.name, "amount": i.amount} for i in b.line_items],
        },
    ),
    _date("due_date", lambda b: b.due_date),
    _date("cutoff_date", lambda b: b.cutoff_date),
    _date("period_start", lambda b: b.period_start),
    _date("period_end", lambda b: b.period_end),
    _date("bill_date", lambda b: b.issued.date() if b.issued else None),
    CfeSensorDescription(
        key="energy",
        translation_key="energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda b: b.kwh,
        attrs_fn=lambda b: {
            "days": b.days,
            "tiers": [
                {"name": t.name, "kwh": t.kwh, "price": t.price, "subtotal": t.subtotal}
                for t in b.tiers
            ],
            "history": [
                {"year": h.year, "month": h.month, "kwh": h.kwh} for h in b.history
            ],
        },
    ),
    CfeSensorDescription(
        key="daily_energy",
        translation_key="daily_energy",
        native_unit_of_measurement="kWh/d",
        suggested_display_precision=2,
        value_fn=lambda b: b.daily_kwh,
    ),
    _money("daily_cost", lambda b: b.daily_cost),
    CfeSensorDescription(
        key="meter_reading",
        translation_key="meter_reading",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda b: b.meter_current,
        attrs_fn=lambda b: {
            "meter_id": b.meter_id,
            "previous_reading": b.meter_previous,
        },
    ),
    CfeSensorDescription(
        key="tariff",
        translation_key="tariff",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda b: b.tariff,
        attrs_fn=lambda b: {"summer_start": b.summer_start, "summer_end": b.summer_end},
    ),
    _money("subsidy", lambda b: b.subsidy),
    _money("real_cost", lambda b: b.real_cost),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CfeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the bill sensors."""
    coordinator = entry.runtime_data
    async_add_entities(CfeSensor(coordinator, d) for d in SENSORS)


class CfeSensor(CfeEntity, SensorEntity):
    """A value from the latest bill."""

    entity_description: CfeSensorDescription

    def __init__(self, coordinator: CfeCoordinator, description: CfeSensorDescription) -> None:
        """Create the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | str | date | None:
        """Current value."""
        bill = self.bill
        return self.entity_description.value_fn(bill) if bill else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Bill details for the attribute-bearing sensors."""
        bill = self.bill
        if bill is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(bill)
