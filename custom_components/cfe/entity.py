"""Base entity for the CFE integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import CfeCoordinator
from .parser import Bill


class CfeEntity(CoordinatorEntity[CfeCoordinator]):
    """One device per RPU; stays available while a stored bill exists."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: CfeCoordinator, key: str) -> None:
        """Attach the entity to the RPU's device."""
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.rpu}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.rpu)},
            name=f"CFE {coordinator.rpu}",
            manufacturer=MANUFACTURER,
            model="Aviso-recibo",
            serial_number=coordinator.rpu,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def bill(self) -> Bill | None:
        """The latest bill, if any."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.latest

    @property
    def available(self) -> bool:
        """Available whenever there is a bill, even if the mailbox is down."""
        return self.bill is not None
