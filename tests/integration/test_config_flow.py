"""Tests for the config flow."""

from unittest.mock import MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType

from custom_components.vwg_eu_data_act.api.exception import (
    EudaActionRequiredError,
    EudaAuthError,
    EudaError,
)
from custom_components.vwg_eu_data_act.const import CONF_BRAND, CONF_VIN, DOMAIN

from .conftest import ENTRY_DATA, VIN

USER_INPUT = {
    CONF_BRAND: "volkswagen",
    CONF_EMAIL: "owner@example.com",
    CONF_PASSWORD: "secret",
}


async def _start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


async def test_full_flow(hass, client):
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "vehicle"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_VIN: VIN}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "ID.4"
    assert result["data"] == ENTRY_DATA
    assert result["result"].unique_id == VIN


async def test_invalid_auth_then_recover(hass, client):
    client.login_error = EudaAuthError("nope")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "invalid_auth"}

    client.login_error = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["step_id"] == "vehicle"


async def test_cannot_connect(hass, client):
    client.login_error = EudaError("down", status=503)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_no_vehicles(hass, client):
    client.vehicles = {}
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_vehicles"


async def test_vehicle_without_data_request(hass, client):
    client.identifier = None
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_VIN: VIN}
    )
    assert result["errors"] == {"base": "no_data_request"}


async def test_already_configured(hass, client, config_entry):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_VIN: VIN}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass, client, config_entry):
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    client.login_error = EudaAuthError("nope")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    client.login_error = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-secret"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "new-secret"
    # A successful reauth reloads the entry; let that finish before teardown.
    await hass.async_block_till_done()


async def test_account_needing_attention(hass, client):
    client.login_error = EudaActionRequiredError("terms")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "action_required"}


async def test_one_session_per_flow_released_at_the_end(hass, client):
    session = MagicMock()
    with patch(
        "custom_components.vwg_eu_data_act.config_flow.async_create_clientsession",
        return_value=session,
    ) as create:
        client.login_error = EudaAuthError("nope")
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        client.login_error = None
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        # Each attempt starts from an empty cookie jar, on the same session.
        assert create.call_count == 1
        assert session.cookie_jar.clear.call_count == 2
        session.detach.assert_not_called()

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VIN: VIN}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    session.detach.assert_called_once()
