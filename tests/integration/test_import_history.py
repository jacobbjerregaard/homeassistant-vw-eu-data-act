"""Tests for the import_history action, against a real (in-memory) recorder."""

import shutil
from datetime import UTC, datetime

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.vwg_eu_data_act.const import DOMAIN
from tests.conftest import FIXTURES

from .conftest import VIN

TEN = datetime(2026, 9, 1, 10, tzinfo=UTC)
ELEVEN = datetime(2026, 9, 1, 11, tzinfo=UTC)
TWELVE = datetime(2026, 9, 1, 12, tzinfo=UTC)


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url):
    """Prepare the recorder's database before hass is created.

    The recorder fixture has to be set up ahead of hass, which the autouse
    fixture enabling custom integrations would otherwise create first.
    """


@pytest.fixture
def export_path(hass, tmp_path):
    """The fixture export, in the www folder of a temporary config folder.

    www is one of the folders Home Assistant lets actions read by default.
    """
    hass.config.config_dir = str(tmp_path)
    www = tmp_path / "www"
    www.mkdir()
    hass.config.allowlist_external_dirs = {str(www)}
    path = www / "export.json"
    shutil.copy(FIXTURES / "one_off_export.json", path)
    return path


async def _import(hass, config_entry, path="www/export.json"):
    response = await hass.services.async_call(
        DOMAIN,
        "import_history",
        {"config_entry_id": config_entry.entry_id, "path": path},
        blocking=True,
        return_response=True,
    )
    await async_wait_recording_done(hass)
    return response


async def _rows(hass, statistic_id, units=None):
    """Read statistics back; ``units`` overrides the sensor's display unit."""
    rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        TEN,
        None,
        {statistic_id},
        "hour",
        units,
        {"mean", "min", "max", "state", "sum"},
    )
    return rows.get(statistic_id, [])


async def _setup_domain(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()


async def test_import_into_the_sensors_statistics(
    recorder_mock: Recorder, hass, client, config_entry, export_path
):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    response = await _import(hass, config_entry)

    series = response["series"]
    assert series["battery_level"] == {
        "statistic_id": "sensor.id_4_battery_level",
        "hours": 2,
        "from": TEN.isoformat(),
        "to": ELEVEN.isoformat(),
    }
    rows = await _rows(hass, "sensor.id_4_battery_level")
    assert [(r["mean"], r["min"], r["max"]) for r in rows] == [
        (71.0, 70.0, 72.0),
        (75.0, 75.0, 75.0),
    ]
    temperature = await _rows(hass, "sensor.id_4_outside_temperature")
    assert [r["mean"] for r in temperature] == [10.0, 12.0]

    # Without earlier statistics the odometer's sum starts at zero.
    odometer = await _rows(hass, "sensor.id_4_odometer")
    assert [(r["state"], r["sum"]) for r in odometer] == [(40030, 0), (40058, 28)]

    # Running it again adds nothing: those hours are there now.
    again = await _import(hass, config_entry)
    assert {key: s["hours"] for key, s in again["series"].items()} == dict.fromkeys(
        series, 0
    )


async def test_odometer_joins_up_with_existing_statistics(
    recorder_mock: Recorder, hass, client, config_entry, export_path
):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    # What Home Assistant recorded itself, from 12:00 on.
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": None,
            "source": "recorder",
            "statistic_id": "sensor.id_4_odometer",
            "unit_class": "distance",
            "unit_of_measurement": "km",
        },
        [{"start": TWELVE, "state": 40100, "sum": 0}],
    )
    await async_wait_recording_done(hass)

    await _import(hass, config_entry)

    rows = await _rows(hass, "sensor.id_4_odometer")
    # The existing hour is untouched, and the imported ones count back from it.
    assert [(r["state"], r["sum"]) for r in rows] == [
        (40030, -70),
        (40058, -42),
        (40100, 0),
    ]


async def test_existing_hours_are_not_overwritten(
    recorder_mock: Recorder, hass, client, config_entry, export_path
):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.ARITHMETIC,
            "has_sum": False,
            "name": None,
            "source": "recorder",
            "statistic_id": "sensor.id_4_battery_level",
            "unit_class": None,
            "unit_of_measurement": "%",
        },
        [{"start": ELEVEN, "mean": 60, "min": 60, "max": 60}],
    )
    await async_wait_recording_done(hass)

    response = await _import(hass, config_entry)

    assert response["series"]["battery_level"]["hours"] == 1
    rows = await _rows(hass, "sensor.id_4_battery_level")
    assert [r["mean"] for r in rows] == [71.0, 60.0]


async def test_without_sensors_the_history_goes_to_external_statistics(
    recorder_mock: Recorder, hass, client, config_entry, export_path
):
    # The entry cannot be set up (nothing delivered), so there are no sensors.
    client.files = []
    await _setup_domain(hass)

    response = await _import(hass, config_entry)

    statistic_id = f"{DOMAIN}:{VIN.lower()}_battery_level"
    assert response["series"]["battery_level"]["statistic_id"] == statistic_id
    rows = await _rows(hass, statistic_id)
    assert [r["mean"] for r in rows] == [71.0, 75.0]


async def test_relative_and_absolute_paths(
    recorder_mock: Recorder, hass, client, config_entry, export_path
):
    await _setup_domain(hass)
    response = await _import(hass, config_entry, path=str(export_path))
    assert response["readings"] == 9


@pytest.mark.parametrize(
    ("path", "key"),
    [
        ("/etc/passwd", "path_not_allowed"),
        # The configuration folder itself is not readable by default.
        ("secrets.yaml", "path_not_allowed"),
        ("www/missing.json", "file_not_found"),
    ],
)
async def test_bad_paths(
    recorder_mock: Recorder, hass, client, config_entry, export_path, path, key
):
    await _setup_domain(hass)
    with pytest.raises(ServiceValidationError) as info:
        await _import(hass, config_entry, path=path)
    assert info.value.translation_key == key


async def test_not_an_export(
    recorder_mock: Recorder, hass, client, config_entry, export_path
):
    await _setup_domain(hass)
    export_path.write_text('{"hello": "world"}', encoding="utf-8")
    with pytest.raises(ServiceValidationError) as info:
        await _import(hass, config_entry)
    assert info.value.translation_key == "unreadable_export"


async def test_export_of_another_vehicle(
    recorder_mock: Recorder, hass, client, config_entry, export_path
):
    await _setup_domain(hass)
    text = export_path.read_text(encoding="utf-8").replace(VIN, "WVWZZZE1ZOTHER002")
    export_path.write_text(text, encoding="utf-8")
    with pytest.raises(ServiceValidationError) as info:
        await _import(hass, config_entry)
    assert info.value.translation_key == "wrong_vehicle"


async def test_unknown_entry(recorder_mock: Recorder, hass, client, export_path):
    await _setup_domain(hass)
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(
            DOMAIN,
            "import_history",
            {"config_entry_id": "nope", "path": "www/export.json"},
            blocking=True,
            return_response=True,
        )
    assert info.value.translation_key == "entry_not_found"


async def test_without_recorder(hass, client, config_entry, export_path):
    await _setup_domain(hass)
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(
            DOMAIN,
            "import_history",
            {"config_entry_id": config_entry.entry_id, "path": "www/export.json"},
            blocking=True,
            return_response=True,
        )
    assert info.value.translation_key == "recorder_not_loaded"


async def test_history_follows_the_statistics_unit(
    recorder_mock: Recorder, hass, client, config_entry, export_path
):
    """An odometer kept in miles gets its history in miles too."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": None,
            "source": "recorder",
            "statistic_id": "sensor.id_4_odometer",
            "unit_class": "distance",
            "unit_of_measurement": "mi",
        },
        [{"start": TWELVE, "state": 24900, "sum": 0}],
    )
    await async_wait_recording_done(hass)

    await _import(hass, config_entry)

    # Read back in miles: by default statistics come back in the sensor's
    # display unit, which is km here.
    rows = await _rows(hass, "sensor.id_4_odometer", units={"distance": "mi"})
    # 40030 and 40058 km, in miles.
    assert [round(r["state"]) for r in rows[:2]] == [24873, 24891]
    # And joined up with the existing hour: 24900 - 24891 miles earlier.
    assert round(rows[1]["sum"]) == -9
