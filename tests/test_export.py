"""Tests for reading the history in a one-off export."""

import io
import zipfile
from datetime import UTC, datetime

import pytest
from euda_api import export
from euda_api.exception import EudaError
from euda_api.export import read_export, read_export_file

from tests.conftest import FIXTURES

EXPORT = FIXTURES / "one_off_export.json"
TEN = datetime(2026, 9, 1, 10, tzinfo=UTC)
ELEVEN = datetime(2026, 9, 1, 11, tzinfo=UTC)


def _check(history):
    assert history.vin == "WVWZZZE1ZTEST0001"
    assert history.entries == 15
    # 3 SoC, 3 mileage, 2 temperature, 1 range: "N/A" and "unknown" skipped.
    assert history.readings == 9
    assert set(history.series) == {
        "battery_level",
        "odometer",
        "outside_temperature",
        "range",
    }

    soc = history.series["battery_level"]
    assert [hour.start for hour in soc] == [TEN, ELEVEN]
    assert (soc[0].mean, soc[0].min, soc[0].max, soc[0].last) == (71.0, 70, 72, 72)
    assert soc[1].mean == 75.0

    odometer = history.series["odometer"]
    # The last reading of the hour by time, not by position in the file.
    assert [hour.last for hour in odometer] == [40030, 40058]

    temperature = history.series["outside_temperature"]
    assert [hour.mean for hour in temperature] == [10.0, 12.0]
    assert history.unit("outside_temperature") == "°C"
    assert history.is_total("odometer")
    assert not history.is_total("battery_level")


def test_read_export():
    _check(read_export_file(EXPORT))


def test_entries_cut_at_chunk_boundaries(monkeypatch):
    # Seven characters at a time cuts nearly every entry in two.
    monkeypatch.setattr(export, "_CHUNK", 7)
    _check(read_export_file(EXPORT))


def test_read_export_from_zip(tmp_path):
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("WVWZZZE1ZTEST0001_20260923023339.json", EXPORT.read_bytes())
    _check(read_export_file(archive))


def test_whitespace_and_key_order():
    history = read_export(
        io.StringIO(
            ' {\n "Data" : [\n  {"dataFieldName": "currentSOCInPct", "value": "5",'
            ' "timestampUtc": "2026-09-01T10:00:00Z"} ,\n ] ,\n "vin": "x"\n}'
        )
    )
    # The VIN comes after the Data array here, so it is not known.
    assert history.vin is None
    assert history.series["battery_level"][0].mean == 5.0


@pytest.mark.parametrize(
    "text",
    [
        '{"vin": "x"}',
        '{"Data": {"a": 1}}',
        '{"Data": [{"key": "a", "value": "1"',
        '{"Data": [{"key": "a", "value": oops}]}',
    ],
)
def test_not_an_export(text):
    with pytest.raises(EudaError):
        read_export(io.StringIO(text))


def test_unreadable_file(tmp_path):
    with pytest.raises(EudaError):
        read_export_file(tmp_path / "missing.json")
    empty_zip = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty_zip, "w") as zipped:
        zipped.writestr("readme.txt", "hello")
    with pytest.raises(EudaError):
        read_export_file(empty_zip)
