"""CFE bills from emailed CFDI XML."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.config_entry_oauth2_flow import (
    OAuth2Session,
    async_get_config_entry_implementation,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    AUTH_GOOGLE,
    AUTH_IMAP,
    CONF_AUTH_TYPE,
    DOMAIN,
    SERVICE_REFRESH,
    STORAGE_VERSION,
)
from .coordinator import CfeConfigEntry, CfeCoordinator
from .gmail import GmailClient, session_get_json

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the cfe.refresh service."""

    async def _refresh(call: ServiceCall) -> None:
        failed = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                coordinator = entry.runtime_data
                await coordinator.async_refresh()
                if not coordinator.last_update_success:
                    failed.append(f"{entry.title}: {coordinator.last_exception}")
        if failed:
            raise HomeAssistantError(
                "CFE mailbox check failed (stored bills kept): " + "; ".join(failed)
            )

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, _refresh)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CfeConfigEntry) -> bool:
    """Set up one CFE service (RPU)."""
    gmail = None
    if entry.data.get(CONF_AUTH_TYPE, AUTH_IMAP) == AUTH_GOOGLE:
        # Raises ConfigEntryNotReady while the Google client is not available.
        implementation = await async_get_config_entry_implementation(hass, entry)
        gmail = GmailClient(session_get_json(OAuth2Session(hass, entry, implementation)))
    coordinator = CfeCoordinator(hass, entry, gmail)
    if await coordinator.async_load_store():
        # Stored bills are served at once; mail is checked in the background so
        # HA starts (and the sensors restore) even with no internet.
        entry.async_create_background_task(
            hass, coordinator.async_refresh(), f"{DOMAIN} initial refresh"
        )
    else:
        await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CfeConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the stored bills (they contain the customer's name and address)."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
