"""Mark paid button for the latest CFE bill."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Add the Mark paid button."""
    async_add_entities([CfeMarkPaidButton(entry.runtime_data, "mark_paid")])


class CfeMarkPaidButton(CfeEntity, ButtonEntity):
    """Marks the current bill (by folio) as paid; persisted locally."""

    async def async_press(self) -> None:
        """Flag the latest bill as paid."""
        bill = self.bill
        if bill is None:
            return
        await self.coordinator.async_mark_paid(bill.folio)
