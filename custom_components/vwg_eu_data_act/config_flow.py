"""Config flow for the VW Group EU Data Act integration.

One config entry is created per vehicle. The user signs in with their brand
account, then picks a vehicle from the ones linked to it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api.brands import BRANDS, DEFAULT_BRAND, get_brand
from .api.client import EudaClient
from .api.exception import (
    EudaActionRequiredError,
    EudaAuthError,
    EudaError,
    EudaNoDataError,
)
from .const import CONF_BRAND, CONF_IDENTIFIER, CONF_NICKNAME, CONF_VIN, DOMAIN

_LOGGER = logging.getLogger(__name__)

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BRAND, default=DEFAULT_BRAND): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=brand.key, label=brand.name)
                    for brand in BRANDS.values()
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
    }
)


class EudaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in, then pick a vehicle."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow state."""
        self._credentials: dict[str, Any] = {}
        self._session: aiohttp.ClientSession | None = None
        self._client: EudaClient | None = None
        self._vehicles: dict[str, str | None] = {}

    def _make_client(self, credentials: Mapping[str, Any]) -> EudaClient:
        # One session for the whole flow, not one per attempt: sessions made
        # outside entry setup are otherwise only released at shutdown. Each
        # attempt starts from an empty cookie jar all the same.
        if self._session is None:
            self._session = async_create_clientsession(self.hass)
        self._session.cookie_jar.clear()
        return EudaClient(
            self._session,
            get_brand(credentials[CONF_BRAND]),
            credentials[CONF_EMAIL],
            credentials[CONF_PASSWORD],
        )

    @callback
    def async_remove(self) -> None:
        """Release the flow's HTTP session when the flow ends."""
        if self._session is not None:
            self._session.detach()
            self._session = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the brand and the account credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client = self._make_client(user_input)
            try:
                await client.login()
                vehicles = await client.list_vehicles()
            except EudaAuthError:
                errors["base"] = "invalid_auth"
            except EudaActionRequiredError:
                errors["base"] = "action_required"
            except EudaError:
                _LOGGER.debug("Could not reach the portal", exc_info=True)
                errors["base"] = "cannot_connect"
            else:
                if not vehicles:
                    return self.async_abort(reason="no_vehicles")
                self._credentials = user_input
                self._client = client
                self._vehicles = vehicles
                return await self.async_step_vehicle()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one of the account's vehicles."""
        assert self._client is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            vin = user_input[CONF_VIN]
            await self.async_set_unique_id(vin)
            self._abort_if_unique_id_configured()
            try:
                identifier = await self._client.get_request_identifier(vin)
            except EudaNoDataError:
                errors["base"] = "no_data_request"
            except EudaError:
                _LOGGER.debug("Could not look up the data request", exc_info=True)
                errors["base"] = "cannot_connect"
            else:
                nickname = self._vehicles.get(vin)
                return self.async_create_entry(
                    title=nickname or vin,
                    data={
                        **self._credentials,
                        CONF_VIN: vin,
                        CONF_NICKNAME: nickname,
                        CONF_IDENTIFIER: identifier,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_VIN): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                value=vin, label=f"{name} ({vin})" if name else vin
                            )
                            for vin, name in self._vehicles.items()
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="vehicle", data_schema=schema, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication after the portal rejected the credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            credentials = {**entry.data, **user_input}
            try:
                await self._make_client(credentials).login()
            except EudaAuthError:
                errors["base"] = "invalid_auth"
            except EudaActionRequiredError:
                errors["base"] = "action_required"
            except EudaError:
                _LOGGER.debug("Could not reach the portal", exc_info=True)
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data=credentials)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR}),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )
