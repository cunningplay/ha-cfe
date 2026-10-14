"""Local 'paid' state for the latest CFE bill."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CfeConfigEntry
from .entity import CfeEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CfeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the paid binary sensor."""
    async_add_entities([CfePaidSensor(entry.runtime_data, "paid")])


class CfePaidSensor(CfeEntity, BinarySensorEntity):
    """On when the latest bill has been marked paid."""

    @property
    def is_on(self) -> bool | None:
        """Paid flag of the latest bill."""
        bill = self.bill
        if bill is None:
            return None
        return self.coordinator.data.is_paid(bill.folio)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Which bill the flag belongs to."""
        bill = self.bill
        if bill is None:
            return None
        return {
            "folio": bill.folio,
            "due_date": bill.due_date.isoformat() if bill.due_date else None,
        }
