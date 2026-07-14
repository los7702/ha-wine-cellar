"""Config flow for Cork Dork."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_VIVINO_AUTO_SYNC,
    CONF_VIVINO_CELLAR_URL,
    CONF_VIVINO_SESSION_COOKIE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


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
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = await self._async_validate_vivino(user_input)
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        # On validation errors re-show the form with the entered values so
        # the user can correct them without retyping everything.
        current = user_input if user_input is not None else self.config_entry.options

        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "default_wine_type",
                        default=current.get("default_wine_type", "red"),
                    ): vol.In(["red", "white", "rosé", "sparkling", "dessert"]),
                    vol.Optional(
                        "gemini_api_key",
                        default=current.get("gemini_api_key", ""),
                    ): str,
                    vol.Optional(
                        CONF_VIVINO_CELLAR_URL,
                        default=current.get(CONF_VIVINO_CELLAR_URL, ""),
                    ): str,
                    vol.Optional(
                        CONF_VIVINO_SESSION_COOKIE,
                        default=current.get(CONF_VIVINO_SESSION_COOKIE, ""),
                    ): str,
                    vol.Optional(
                        CONF_VIVINO_AUTO_SYNC,
                        default=current.get(CONF_VIVINO_AUTO_SYNC, False),
                    ): bool,
                }
            ),
        )

    async def _async_validate_vivino(
        self, user_input: dict[str, Any]
    ) -> dict[str, str]:
        """Test the Vivino session cookie against the cellar. Returns errors."""
        cookie = (user_input.get(CONF_VIVINO_SESSION_COOKIE) or "").strip()
        cellar_url = (user_input.get(CONF_VIVINO_CELLAR_URL) or "").strip()

        if not cookie and not cellar_url:
            return {}  # Vivino connection intentionally not configured
        if not cookie or not cellar_url:
            return {"base": "vivino_incomplete"}

        # Only re-verify when the cookie or URL actually changed
        options = self.config_entry.options
        if (
            cookie == options.get(CONF_VIVINO_SESSION_COOKIE)
            and cellar_url == options.get(CONF_VIVINO_CELLAR_URL)
        ):
            return {}

        from .vivino_account import (
            VivinoAccountClient,
            VivinoAuthError,
            VivinoConnectionError,
        )

        client = VivinoAccountClient(self.hass, cookie, cellar_url)
        try:
            result = await client.async_verify()
        except VivinoConnectionError as err:
            _LOGGER.warning("Vivino connection test failed: %s", err)
            return {"base": "vivino_cannot_connect"}
        except VivinoAuthError as err:
            _LOGGER.warning("Vivino cookie check failed: %s", err)
            return {"base": "vivino_invalid_auth"}
        _LOGGER.debug("Vivino cookie verified; first page wines: %s",
                      result.get("first_page_wines"))
        return {}
