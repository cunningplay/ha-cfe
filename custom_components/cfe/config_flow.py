"""Config, options and reauth flows for CFE."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_FOLDER,
    CONF_RPU,
    CONF_SCAN_INTERVAL,
    DEFAULT_FOLDER,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import CfeConfigEntry
from .mail import MailAuthError, MailError, check_login

_LOGGER = logging.getLogger(__name__)

_RPU_RE = re.compile(r"^\d{12}$")


def _user_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, DEFAULT_HOST)): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
            ),
            vol.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(
                    type=TextSelectorType.PASSWORD, autocomplete="current-password"
                )
            ),
            vol.Required(
                CONF_FOLDER, default=defaults.get(CONF_FOLDER, DEFAULT_FOLDER)
            ): str,
            vol.Required(CONF_RPU, default=defaults.get(CONF_RPU, "")): str,
        }
    )


async def _validate(hass: HomeAssistant, data: Mapping[str, Any]) -> str | None:
    """Return an error key, or None when the mailbox opens."""
    try:
        await hass.async_add_executor_job(
            check_login,
            data[CONF_HOST],
            data[CONF_PORT],
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
            data.get(CONF_FOLDER, DEFAULT_FOLDER),
        )
    except MailAuthError:
        return "invalid_auth"
    except MailError as err:
        _LOGGER.debug("IMAP check failed: %s", err)
        return "cannot_connect"
    return None


class CfeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a CFE service from a mailbox."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the mailbox and the service number (RPU)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_RPU] = re.sub(r"\s", "", user_input[CONF_RPU])
            user_input[CONF_HOST] = user_input[CONF_HOST].strip()
            if not _RPU_RE.match(user_input[CONF_RPU]):
                errors[CONF_RPU] = "invalid_rpu"
            else:
                await self.async_set_unique_id(user_input[CONF_RPU])
                self._abort_if_unique_id_configured()
                if error := await _validate(self.hass, user_input):
                    errors["base"] = error
                else:
                    return self.async_create_entry(
                        title=f"CFE {user_input[CONF_RPU]}", data=user_input
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Mailbox login was rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new app password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            if error := await _validate(self.hass, data):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD,
                            autocomplete="current-password",
                        )
                    )
                }
            ),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: CfeConfigEntry) -> CfeOptionsFlow:
        """Options: polling interval."""
        return CfeOptionsFlow()


class CfeOptionsFlow(OptionsFlowWithReload):
    """Change how often the mailbox is checked."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Polling interval in hours."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="h",
                        )
                    )
                }
            ),
        )
