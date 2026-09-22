"""Tests for how the vehicle's state is built from datasets over time."""

from homeassistant.config_entries import ConfigEntryState

from custom_components.vwg_eu_data_act.api.dataset import Dataset
from custom_components.vwg_eu_data_act.api.dictionary import load_data_dictionary
from custom_components.vwg_eu_data_act.api.exception import (
    EudaActionRequiredError,
    EudaError,
)
from custom_components.vwg_eu_data_act.const import MAX_FILE_ATTEMPTS
from custom_components.vwg_eu_data_act.sensor import SENSORS
from tests.conftest import load_fixture

from .conftest import delivery


async def _setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _refresh(hass, entry):
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


def _state(hass, name):
    return hass.states.get(f"sensor.id_4_{name}").state


def _parked(client, name, minute):
    client.files.append(delivery(name, minute))
    client.datasets[name] = Dataset.from_json(load_fixture("meb_reduced_dataset.json"))


async def test_parked_vehicle_keeps_its_last_known_values(hass, client, config_entry):
    await _setup(hass, config_entry)
    _parked(client, "b.zip", 15)
    await _refresh(hass, config_entry)

    # Left out of the reduced dataset: still the last known values.
    assert _state(hass, "odometer") == "10256"
    assert _state(hass, "charging_power") == "9.899994"
    # Carried by the reduced dataset: updated.
    assert _state(hass, "charging_state") == "charge_state_not_ready_for_charging"
    # battery_level_HV is the only state of charge a parked vehicle sends.
    assert _state(hass, "battery_level") == "67.0"
    assert _state(hass, "last_reported") == "2026-09-20T11:30:00+00:00"


async def test_pinned_key_ignores_the_charge_start_duplicate(
    hass, client, config_entry
):
    await _setup(hass, config_entry)
    # The fixture also holds a "SoC when charging started" of 50 under a
    # lower key; the pinned key's 72 is the one shown.
    assert _state(hass, "battery_level") == "72"


async def test_startup_merges_recent_datasets_oldest_first(hass, client, config_entry):
    # A restart while parked: the newest dataset alone is the reduced one.
    _parked(client, "b.zip", 15)
    await _setup(hass, config_entry)

    assert client.downloads == ["a.zip", "b.zip"]
    assert _state(hass, "odometer") == "10256"
    assert _state(hass, "battery_level") == "67.0"


async def test_unreadable_newest_file_falls_back_and_is_eventually_skipped(
    hass, client, config_entry
):
    await _setup(hass, config_entry)
    _parked(client, "b.zip", 15)
    _parked(client, "c.zip", 30)
    client.download_errors["c.zip"] = EudaError("corrupt")

    await _refresh(hass, config_entry)
    # b.zip got through; the sensors stay available and current.
    assert _state(hass, "battery_level") == "67.0"
    assert config_entry.runtime_data.data.file.name == "b.zip"

    for _ in range(MAX_FILE_ATTEMPTS + 2):
        await _refresh(hass, config_entry)
    assert client.downloads.count("c.zip") == MAX_FILE_ATTEMPTS
    assert _state(hass, "battery_level") == "67.0"


async def test_first_load_fails_when_nothing_can_be_read(hass, client, config_entry):
    client.download_errors["a.zip"] = EudaError("down", status=503)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_failed_request_lookup_keeps_state(hass, client, config_entry):
    await _setup(hass, config_entry)
    client.files = []
    client.identifier_error = EudaError("down", status=503)
    await _refresh(hass, config_entry)
    assert _state(hass, "battery_level") == "72"


async def test_account_needing_attention(hass, client, config_entry):
    client.login_error = EudaActionRequiredError("terms")
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert "sign in once in a browser" in config_entry.reason
    # Not a password problem, so no reauthentication is started.
    assert not hass.config_entries.flow.async_progress()


def test_pinned_keys_match_the_dictionary():
    dictionary = load_data_dictionary()
    for description in SENSORS:
        for field in description.fields:
            assert field.key is not None, field.name
            assert dictionary[field.key].name == field.name
