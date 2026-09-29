"""Tests for the calculated figures: live, imported and stored."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.vwg_eu_data_act.api.dataset import Dataset, DatasetFile
from custom_components.vwg_eu_data_act.const import DOMAIN

from .conftest import VIN

SOC = "ac1108b1-b8cc-3db9-a663-03d387e42223"  # battery_level_HV.value
POWER = "c8cb205f-01c6-3c81-bda1-059b99ae6515"  # charge_power
MILEAGE = "75d65f00-5fa8-334a-826d-e73e91fe5c8d"  # mileage.value
OUTSIDE = "b6ea5ae8-53ff-386d-8415-1baa0602bbb6"  # outdoor_temperature

#: Monday 14 September 2026, 06:00 UTC: ISO week 38.
MONDAY = datetime(2026, 9, 14, 6, tzinfo=UTC)


def snapshot(minutes, *, soc=None, power=None, km=None, outside=None):
    """A continuous-feed dataset captured ``minutes`` after MONDAY."""
    when = MONDAY + timedelta(minutes=minutes)
    points = [
        {"key": "cap", "dataFieldName": "car_captured_time", "value": when.isoformat()}
    ]
    for key, name, value in (
        (SOC, "battery_level_HV.value", soc),
        (POWER, "battery_state_report.charge_power", power),
        (MILEAGE, "mileage.value", km),
        (OUTSIDE, "outdoor_temperature", outside),
    ):
        if value is not None:
            points.append({"key": key, "dataFieldName": name, "value": str(value)})
    file = DatasetFile(f"{when:%Y%m%d%H%M%S}_{VIN}.zip", when + timedelta(minutes=1))
    return file, Dataset.from_json({"vin": VIN, "Data": points})


#: A 7.4 kW home charge from 40 to 60 % in 15-minute snapshots, a pause, and
#: a 120 km drive on 16 % at 8 °C.
FEED = [
    snapshot(0, soc=40, power=7.4, km=10_000, outside=8),
    *(snapshot(15 * i, soc=40 + i, power=7.4) for i in range(1, 16)),
    snapshot(240, soc=60, power=7.4),
    snapshot(255, soc=60, power=0.0),
    snapshot(330, soc=60),
    snapshot(600, soc=60, km=10_000, outside=8),
    snapshot(700, soc=44, km=10_120, outside=8),
]


async def _setup(hass, config_entry):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def _deliver(hass, client, config_entry, feed):
    """Hand the snapshots over one poll at a time, as the portal would."""
    for file, dataset in feed:
        client.files.append(file)
        client.datasets[file.name] = dataset
        await config_entry.runtime_data.async_refresh()
        await hass.async_block_till_done()


def _state(hass, key):
    return hass.states.get(f"sensor.id_4_{key}")


async def test_figures_from_the_live_feed(hass, client, config_entry, freezer):
    freezer.move_to(MONDAY + timedelta(hours=14))
    client.files = []
    await _setup(hass, config_entry)
    await _deliver(hass, client, config_entry, FEED)

    # 7.4 kW for four hours, then the ramp down: about 30 kWh for 20 %.
    charged = float(_state(hass, "energy_charged").state)
    assert charged == pytest.approx(7.4 * 4 + 7.4 / 2 / 4, abs=0.01)
    capacity = float(_state(hass, "usable_battery_capacity").state)
    assert capacity == pytest.approx(charged / 20 * 100, abs=0.1)
    assert _state(hass, "charging_sessions").state == "1"

    # The drive: 16 % of that over 120 km.
    expected = round(capacity * 0.16 / 120 * 100, 1)
    assert float(_state(hass, "consumption_this_week").state) == pytest.approx(
        expected, abs=0.1
    )
    assert float(_state(hass, "consumption_this_month").state) == pytest.approx(
        expected, abs=0.1
    )
    assert float(_state(hass, "consumption_0_10_degc").state) == pytest.approx(
        expected, abs=0.1
    )
    assert _state(hass, "consumption_last_week").state == "unknown"
    assert _state(hass, "consumption_below_0_degc").state == "unknown"

    assert float(_state(hass, "last_charge_energy").state) == pytest.approx(
        charged, abs=0.01
    )
    assert _state(hass, "last_charge_energy").attributes["soc_start"] == 40
    assert _state(hass, "last_charge_type").state == "ac"
    assert float(_state(hass, "last_charge_peak_power").state) == 7.4
    assert _state(hass, "dc_charging_share").state == "0.0"
    assert float(_state(hass, "energy_used").state) == pytest.approx(
        capacity * 0.16, abs=0.05
    )


async def test_figures_survive_a_restart(hass, client, config_entry, freezer):
    freezer.move_to(MONDAY + timedelta(hours=14))
    client.files = []
    await _setup(hass, config_entry)
    await _deliver(hass, client, config_entry, FEED)
    capacity = _state(hass, "usable_battery_capacity").state

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert _state(hass, "usable_battery_capacity").state == capacity
    assert _state(hass, "charging_sessions").state == "1"


def _export(tmp_path, entries):
    www = tmp_path / "www"
    www.mkdir(exist_ok=True)
    path = www / "export.json"
    path.write_text(json.dumps({"vin": VIN, "Data": entries}), encoding="utf-8")
    return path


def _history_entries():
    """An export: a home charge on 1 August and a drive on 3 August."""
    start = datetime(2026, 8, 1, 20, tzinfo=UTC)
    entries = [
        {"dataFieldName": "currentSOCInPct", "value": "30", "timestampUtc": start},
        {
            "dataFieldName": "temperatureOutsideVehicle",
            "value": "288.15",
            "timestampUtc": start,
        },
        {"dataFieldName": "mileage", "value": "39000", "timestampUtc": start},
    ]
    for minute in range(0, 241, 5):
        entries.append(
            {
                "dataFieldName": "chargePowerInKW",
                "value": "11.0",
                "timestampUtc": start + timedelta(minutes=minute),
            }
        )
    end = start + timedelta(minutes=245)
    entries += [
        {"dataFieldName": "chargePowerInKW", "value": "0.0", "timestampUtc": end},
        {
            "dataFieldName": "currentSOCInPct",
            "value": "90",
            "timestampUtc": end + timedelta(minutes=20),
        },
    ]
    drive = datetime(2026, 8, 3, 8, tzinfo=UTC)
    entries += [
        {"dataFieldName": "mileage", "value": "39000", "timestampUtc": drive},
        {"dataFieldName": "currentSOCInPct", "value": "90", "timestampUtc": drive},
        {
            "dataFieldName": "mileage",
            "value": "39200",
            "timestampUtc": drive + timedelta(hours=3),
        },
        {
            "dataFieldName": "currentSOCInPct",
            "value": "50",
            "timestampUtc": drive + timedelta(hours=3, minutes=1),
        },
    ]
    for entry in entries:
        entry["key"] = "k"
        entry["timestampUtc"] = entry["timestampUtc"].isoformat()
    return entries


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url):
    """Prepare the recorder's database before hass is created."""


async def test_import_builds_the_figures_and_their_statistics(
    recorder_mock: Recorder, hass, client, config_entry, tmp_path, freezer
):
    freezer.move_to(datetime(2026, 9, 2, 12, tzinfo=UTC))
    hass.config.config_dir = str(tmp_path)
    hass.config.allowlist_external_dirs = {str(tmp_path / "www")}
    _export(tmp_path, _history_entries())
    client.files = []  # The continuous feed has nothing, as in real life.
    await _setup(hass, config_entry)

    response = await hass.services.async_call(
        DOMAIN,
        "import_history",
        {"config_entry_id": config_entry.entry_id, "path": "www/export.json"},
        blocking=True,
        return_response=True,
    )
    await async_wait_recording_done(hass)

    # 11 kW for four hours and the ramp down, for 60 %.
    charged = 11 * 4 + 11 / 2 * 5 / 60
    assert response["figures"]["charging_sessions"] == 1
    assert response["figures"]["capacity"] == pytest.approx(charged / 60 * 100, abs=0.1)

    # August is "last month" on 2 September; 40 % over 200 km.
    capacity = float(_state(hass, "usable_battery_capacity").state)
    last_month = _state(hass, "consumption_last_month")
    assert float(last_month.state) == pytest.approx(capacity * 0.4 / 200 * 100, abs=0.1)
    assert last_month.attributes["months"]["2026-08"]["distance"] == 200
    # The live totals stay at zero: the history is in their statistics.
    assert _state(hass, "energy_charged").state == "0.0"

    rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 8, 1, tzinfo=UTC),
        None,
        {"sensor.id_4_energy_charged"},
        "hour",
        None,
        {"state", "sum"},
    )
    sums = [row["sum"] for row in rows["sensor.id_4_energy_charged"]]
    # Counting up to zero, where the live sensor takes over: the hours'
    # differences are what the Energy dashboard shows.
    assert sums[-1] == pytest.approx(0)
    assert sums[-1] - sums[0] == pytest.approx(charged, abs=0.01)
    assert response["series"]["energy_charged"]["hours"] == len(sums)


async def test_import_only_uses_the_time_before_the_live_feed(
    recorder_mock: Recorder, hass, client, config_entry, tmp_path, freezer
):
    """History from after the live feed started would be counted twice."""
    freezer.move_to(MONDAY + timedelta(hours=14))
    hass.config.config_dir = str(tmp_path)
    hass.config.allowlist_external_dirs = {str(tmp_path / "www")}
    client.files = []
    await _setup(hass, config_entry)
    await _deliver(hass, client, config_entry, FEED)

    # An export that also covers the live period, with a second charge.
    entries = _history_entries()
    later = MONDAY + timedelta(hours=1)
    entries += [
        {
            "key": "k",
            "dataFieldName": "chargePowerInKW",
            "value": "50",
            "timestampUtc": (later + timedelta(minutes=m)).isoformat(),
        }
        for m in range(0, 30, 5)
    ]
    _export(tmp_path, entries)

    response = await hass.services.async_call(
        DOMAIN,
        "import_history",
        {"config_entry_id": config_entry.entry_id, "path": "www/export.json"},
        blocking=True,
        return_response=True,
    )
    await async_wait_recording_done(hass)

    assert response["figures"]["charging_sessions"] == 1
    # One imported charge and one live one.
    assert config_entry.runtime_data.metrics.summary.sessions == 2


async def test_battery_health_from_the_nominal_capacity(
    hass, client, config_entry, freezer
):
    freezer.move_to(MONDAY + timedelta(hours=14))
    client.files = []
    await _setup(hass, config_entry)
    await _deliver(hass, client, config_entry, FEED)
    capacity = float(_state(hass, "usable_battery_capacity").state)

    # Not set: no health.
    assert _state(hass, "battery_health").state == "unknown"
    coordinator = config_entry.runtime_data

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"nominal_capacity": 40}
    )
    await hass.async_block_till_done()

    health = _state(hass, "battery_health")
    assert float(health.state) == pytest.approx(capacity / 40 * 100, abs=0.1)
    assert health.attributes["nominal_capacity"] == 40
    # Applied without reloading the entry.
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data is coordinator
