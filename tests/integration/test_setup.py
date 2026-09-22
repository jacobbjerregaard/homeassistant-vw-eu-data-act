"""Tests for entry setup, the coordinator and the sensors."""

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er

from custom_components.vwg_eu_data_act.api.dataset import Dataset
from custom_components.vwg_eu_data_act.api.exception import EudaAuthError, EudaError
from custom_components.vwg_eu_data_act.const import (
    CONF_IDENTIFIER,
    DOMAIN,
    MAX_TRANSIENT_FAILURES,
)
from custom_components.vwg_eu_data_act.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.conftest import load_fixture

from .conftest import VIN, delivery


async def _setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _refresh(hass, entry):
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_meb_sensors(hass, client, config_entry):
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    assert hass.states.get("sensor.id_4_battery_level").state == "72"
    assert hass.states.get("sensor.id_4_charging_power").state == "9.899994"
    assert hass.states.get("sensor.id_4_charging_state").state == (
        "charge_state_charging"
    )
    assert hass.states.get("sensor.id_4_odometer").state == "10256"
    assert hass.states.get("sensor.id_4_outside_temperature").state == "26.5"
    assert hass.states.get("sensor.id_4_charging_time_remaining").state == "40.0"
    assert hass.states.get("sensor.id_4_last_reported").state == (
        "2026-09-20T10:05:30+00:00"
    )
    # Fields this vehicle does not report get no entity at all.
    assert hass.states.get("sensor.id_4_fuel_level") is None


async def test_flat_platform_sensors(hass, client, config_entry):
    client.datasets["a.zip"] = Dataset.from_json(load_fixture("flat_dataset.json"))
    await _setup(hass, config_entry)

    assert hass.states.get("sensor.id_4_battery_level").state == "25"
    assert hass.states.get("sensor.id_4_fuel_level").state == "37"
    assert hass.states.get("sensor.id_4_outside_temperature").state == "20.0"
    assert hass.states.get("sensor.id_4_plug_state").state == "disconnected"
    # Minutes on this platform; 95 min shown in the suggested unit.
    assert hass.states.get("sensor.id_4_charging_time_remaining").state == "95.0"
    # A field that is present but empty does not create an entity.
    assert hass.states.get("sensor.id_4_range") is None


@pytest.mark.parametrize("unit", ["MILES", "1"])
async def test_distance_unit_follows_companion_field(hass, client, config_entry, unit):
    document = load_fixture("meb_dataset.json")
    for item in document["Data"]:
        if item["dataFieldName"] == "mileage.unit":
            item["value"] = unit
    client.datasets["a.zip"] = Dataset.from_json(document)
    await _setup(hass, config_entry)

    # Home Assistant converts to the metric default: 10256 mi is 16505 km.
    assert round(float(hass.states.get("sensor.id_4_odometer").state)) == 16505


async def test_downloads_only_new_files(hass, client, config_entry):
    await _setup(hass, config_entry)
    await _refresh(hass, config_entry)
    assert client.downloads == ["a.zip"]

    client.files.append(delivery("b.zip", 15))
    client.datasets["b.zip"] = Dataset.from_json(load_fixture("flat_dataset.json"))
    await _refresh(hass, config_entry)
    assert client.downloads == ["a.zip", "b.zip"]
    # A sensor first seen in the later dataset is added on the fly.
    assert hass.states.get("sensor.id_4_fuel_level").state == "37"


async def test_transient_errors_keep_data_for_a_while(hass, client, config_entry):
    await _setup(hass, config_entry)
    client.list_error = EudaError("bad gateway", status=502)

    for _ in range(MAX_TRANSIENT_FAILURES - 1):
        await _refresh(hass, config_entry)
        assert hass.states.get("sensor.id_4_battery_level").state == "72"

    await _refresh(hass, config_entry)
    assert hass.states.get("sensor.id_4_battery_level").state == STATE_UNAVAILABLE


async def test_replaced_data_request_is_followed(hass, client, config_entry):
    await _setup(hass, config_entry)
    client.identifier = "req-2"
    client.files = [delivery("b.zip", 15)]
    client.datasets["b.zip"] = Dataset.from_json(load_fixture("flat_dataset.json"))

    async def list_datasets(vin, identifier):
        # The replaced request's identifier no longer lists anything.
        return client.files if identifier == "req-2" else []

    client.list_datasets = list_datasets
    await _refresh(hass, config_entry)

    assert config_entry.data[CONF_IDENTIFIER] == "req-2"
    assert client.downloads == ["a.zip", "b.zip"]
    assert hass.states.get("sensor.id_4_fuel_level").state == "37"


@pytest.mark.parametrize("files", [[], [delivery("x_no_content_found.zip", 0)]])
async def test_no_data_yet_retries_setup(hass, client, config_entry, files):
    client.files = files
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert "has no data for this vehicle yet" in config_entry.reason


async def test_empty_listing_keeps_previous_data(hass, client, config_entry):
    await _setup(hass, config_entry)
    client.files = []
    await _refresh(hass, config_entry)
    assert hass.states.get("sensor.id_4_battery_level").state == "72"


async def test_auth_failure_starts_reauth(hass, client, config_entry):
    client.login_error = EudaAuthError("expired")
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_unload(hass, client, config_entry):
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_unique_ids_are_namespaced_by_vin(hass, client, config_entry):
    await _setup(hass, config_entry)
    registry = er.async_get(hass)
    entry = registry.async_get("sensor.id_4_battery_level")
    assert entry.unique_id == "WVWZZZE1ZTEST0001_battery_level"


async def test_diagnostics_redact(hass, client, config_entry):
    await _setup(hass, config_entry)
    result = await async_get_config_entry_diagnostics(hass, config_entry)

    assert result["entry"]["password"] == "**REDACTED**"
    assert result["entry"]["vin"] == "**REDACTED**"
    names = [field["name"] for field in result["dataset"]["fields"]]
    assert "battery_state_report.soc" in names


async def test_diagnostics_redact_by_name_segment_and_vin(hass, client, config_entry):
    client.datasets["a.zip"] = Dataset.from_json(
        {
            "vin": VIN,
            "Data": [
                {"key": "1", "dataFieldName": "vehicle.vin", "value": "x"},
                {"key": "2", "dataFieldName": "position.latitude", "value": "55.6"},
                {"key": "3", "dataFieldName": "note", "value": f"car {VIN}"},
                {
                    "key": "4",
                    "dataFieldName": "cso_v1_drivingenvironment_outdoorTemperature",
                    "value": "21",
                },
                {"key": "5", "dataFieldName": "state_of_charge", "value": "25"},
            ],
        }
    )
    await _setup(hass, config_entry)
    result = await async_get_config_entry_diagnostics(hass, config_entry)

    values = {f["name"]: f["value"] for f in result["dataset"]["fields"]}
    assert values["vehicle.vin"] == "**REDACTED**"
    assert values["position.latitude"] == "**REDACTED**"
    assert values["note"] == "**REDACTED**"
    assert values["cso_v1_drivingenvironment_outdoorTemperature"] == "21"
    assert values["state_of_charge"] == "25"
    assert VIN not in str(result)


def _enable(hass, config_entry, key):
    """Pre-register a data point sensor as enabled, as a user would."""
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{VIN}_{key}",
        config_entry=config_entry,
        suggested_object_id=key,
    )


async def test_data_point_sensors_are_disabled_by_default(hass, client, config_entry):
    client.datasets["a.zip"] = Dataset.from_json(load_fixture("flat_dataset.json"))
    await _setup(hass, config_entry)

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, config_entry.entry_id)
    points = [
        e for e in entries if e.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    ]
    assert len(points) == 8
    assert hass.states.get("sensor.id_4_outside_temperature_2") is None


async def test_data_point_sensor_uses_dictionary(hass, client, config_entry):
    client.datasets["a.zip"] = Dataset.from_json(load_fixture("flat_dataset.json"))
    temperature = "6810b781-e54a-35e8-af98-fcdefb54bac6"
    plug = "c111830c-f959-30d2-859a-ea996190d864"
    _enable(hass, config_entry, temperature)
    _enable(hass, config_entry, plug)
    await _setup(hass, config_entry)

    state = hass.states.get(f"sensor.{temperature.replace('-', '_')}")
    # 2931 dK, converted with the unit the dictionary documents.
    assert state.state == "20.0"
    assert state.attributes["unit_of_measurement"] == "°C"
    assert state.attributes["key"] == temperature
    assert state.attributes["description"] == (
        "Temperature outside the vehicle in deci-Kelvin"
    )
    assert state.attributes["measured"] == "2026-09-20T09:39:34+00:00"

    state = hass.states.get(f"sensor.{plug.replace('-', '_')}")
    assert state.state == "disconnected"
    assert "unit_of_measurement" not in state.attributes


async def test_data_point_duration_and_boolean(hass, client, config_entry):
    duration = "7405c11f-4d20-36d2-8381-18364aa1f444"
    locked = "00000000-0000-3000-8000-000000000009"
    _enable(hass, config_entry, duration)
    _enable(hass, config_entry, locked)
    await _setup(hass, config_entry)

    state = hass.states.get(f"sensor.{duration.replace('-', '_')}")
    assert state.name == "ID.4 battery_state_report.remaining_charging_time_complete"
    # "2400s": no unit in the dictionary, but the value says it is seconds.
    assert state.state == "2400"
    assert state.attributes["unit_of_measurement"] == "s"
    assert state.attributes["description"].startswith("The string value of")

    state = hass.states.get(f"sensor.{locked.replace('-', '_')}")
    assert state.state == "true"
    # Not in the dictionary.
    assert state.attributes["description"] is None


async def test_data_point_missing_from_newer_dataset_keeps_value(
    hass, client, config_entry
):
    key = "506cb83e-f99f-3af3-bbeb-0429b69a78d9"
    _enable(hass, config_entry, key)
    await _setup(hass, config_entry)
    entity_id = f"sensor.{key.replace('-', '_')}"
    assert hass.states.get(entity_id).state == "72"

    client.files.append(delivery("b.zip", 15))
    client.datasets["b.zip"] = Dataset.from_json(load_fixture("flat_dataset.json"))
    await _refresh(hass, config_entry)
    assert hass.states.get(entity_id).state == "72"
