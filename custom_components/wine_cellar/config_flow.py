"""Config flow for Cork Dork."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_VIVINO_AUTO_SYNC,
    CONF_VIVINO_EMAIL,
    CONF_VIVINO_PASSWORD,
    DOMAIN,
)


class WineCellarConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Cork Dork."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(
                title="Cork Dork",
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> WineCellarOptionsFlow:
        """Get the options flow."""
        return WineCellarOptionsFlow()


class WineCellarOptionsFlow(OptionsFlow):
    """Handle options flow for Cork Dork."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "default_wine_type",
                        default=self.config_entry.options.get(
                            "default_wine_type", "red"
                        ),
                    ): vol.In(["red", "white", "rosé", "sparkling", "dessert"]),
                    vol.Optional(
                        "gemini_api_key",
                        default=self.config_entry.options.get(
                            "gemini_api_key", ""
                        ),
                    ): str,
                    vol.Optional(
                        CONF_VIVINO_EMAIL,
                        default=self.config_entry.options.get(
                            CONF_VIVINO_EMAIL, ""
                        ),
                    ): str,
                    vol.Optional(
                        CONF_VIVINO_PASSWORD,
                        default=self.config_entry.options.get(
                            CONF_VIVINO_PASSWORD, ""
                        ),
                    ): str,
                    vol.Optional(
                        CONF_VIVINO_AUTO_SYNC,
                        default=self.config_entry.options.get(
                            CONF_VIVINO_AUTO_SYNC, False
                        ),
                    ): bool,
                }
            ),
        )
