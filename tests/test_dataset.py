"""Tests for dataset parsing and delivery-file selection."""

from datetime import UTC, datetime

import pytest
from euda_api.dataset import (
    Dataset,
    DatasetFile,
    newest_with_content,
    parse_timestamp,
    parse_value,
)

from tests.conftest import load_fixture

SOC_KEY = "506cb83e-f99f-3af3-bbeb-0429b69a78d9"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("72", 72),
        ("-457", -457),
        ("9.899994", 9.899994),
        ("true", True),
        ("FALSE", False),
        ("2400s", 2400),
        ("1.5s", 1.5),
        ("CHARGE_STATE_CHARGING", "CHARGE_STATE_CHARGING"),
        ("  ", None),
        (None, None),
    ],
)
def test_parse_value(raw, expected):
    assert parse_value(raw) == expected


def test_parse_timestamp_formats():
    expected = datetime(2026, 9, 20, 10, 5, 30, tzinfo=UTC)
    assert parse_timestamp("2026-09-20T10:05:30Z") == expected
    assert parse_timestamp("2026-09-20T10:05:30.000Z") == expected
    assert parse_timestamp("1789898730000") == expected
    assert parse_timestamp("not a time") is None
    assert parse_timestamp("N/A") is None
    assert parse_timestamp("") is None


def test_meb_dataset():
    dataset = Dataset.from_json(load_fixture("meb_dataset.json"))

    assert dataset.vin == "WVWZZZE1ZTEST0001"
    assert dataset.value("battery_state_report.soc", SOC_KEY) == 72
    assert (
        dataset.value("battery_state_report.remaining_charging_time_complete") == 2400
    )
    assert dataset.value("locked") is True
    # An entry without a value is kept, but reads as unknown.
    assert dataset.get("charging_state_report.charge_mode") is not None
    assert dataset.value("charging_state_report.charge_mode") is None
    assert dataset.value("does.not.exist") is None
    assert dataset.captured_at == datetime(2026, 9, 20, 10, 5, 30, tzinfo=UTC)


def test_flat_dataset_uses_timestamp_utc():
    dataset = Dataset.from_json(load_fixture("flat_dataset.json"))

    assert dataset.value("state_of_charge") == 25
    assert dataset.captured_at == datetime(2026, 9, 20, 9, 40, 28, tzinfo=UTC)


def test_duplicate_field_names_resolve_to_lowest_key():
    dataset = Dataset.from_json(
        {
            "vin": "X",
            "Data": [
                {"key": "b", "dataFieldName": "state", "value": "second"},
                {"key": "a", "dataFieldName": "state", "value": "first"},
            ],
        }
    )
    assert dataset.value("state") == "first"


def test_malformed_entries_are_skipped():
    dataset = Dataset.from_json({"Data": [None, "x", {"value": "1"}, {"key": "k"}]})
    assert list(dataset.points) == ["k"]
    assert dataset.points["k"].name == "k"


def test_dataset_file_created_falls_back_to_name():
    for name in ("20260920100530_WVWZZZE1ZTEST0001.zip", "WVW_20260920100530.zip"):
        file = DatasetFile.from_json({"name": name})
        assert file.created == datetime(2026, 9, 20, 10, 5, 30, tzinfo=UTC)


def test_dataset_file_as_the_portal_sends_it():
    file = DatasetFile.from_json(
        {
            "name": "20260922192741_WVWZZZE1ZTEST0001_no_content_found.zip",
            "createdOn": "2026-09-22T19:28:09Z",
            "size": "264",
        }
    )
    assert file.created == datetime(2026, 9, 22, 19, 28, 9, tzinfo=UTC)
    assert file.size == 264
    assert not file.has_content


def test_newest_with_content_skips_placeholders():
    files = [
        DatasetFile.from_json(
            {"name": "a.zip", "createdOn": "2026-09-20T10:00:00Z", "size": 10}
        ),
        DatasetFile.from_json(
            {"name": "c.zip", "createdOn": "2026-09-20T10:15:00Z", "size": 10}
        ),
        DatasetFile.from_json(
            {"name": "d_no_content_found.zip", "createdOn": "2026-09-20T10:30:00Z"}
        ),
    ]
    newest = newest_with_content(files)
    assert newest is not None
    assert newest.name == "c.zip"
    assert newest_with_content(files[2:]) is None
    assert newest_with_content([]) is None


def test_pinned_key_beats_same_named_duplicates():
    dataset = Dataset.from_json(load_fixture("meb_dataset.json"))
    # By name alone the lowest key wins, which here is the state of charge
    # when charging started: the reason curated sensors pin their key.
    assert dataset.value("battery_state_report.soc") == 50
    assert dataset.value("battery_state_report.soc", SOC_KEY) == 72
    # A pinned key the vehicle does not send falls back to the name.
    assert dataset.value("battery_state_report.soc", "not-sent") == 50


def _points(*items):
    return Dataset.from_json(
        {
            "vin": "V",
            "Data": [
                {"key": key, "dataFieldName": key, "value": value, **extra}
                for key, value, extra in items
            ],
        }
    )


def test_merge_keeps_points_the_newer_dataset_leaves_out():
    old = _points(("soc", "72", {}), ("mileage", "10256", {}))
    new = _points(("soc", "67", {}))
    merged = old.merged_with(new)
    assert merged.value("soc") == 67
    assert merged.value("mileage") == 10256
    # The inputs are left alone.
    assert old.value("soc") == 72


def test_merge_does_not_wipe_a_value_with_an_empty_one():
    merged = _points(("range", "300", {})).merged_with(_points(("range", None, {})))
    assert merged.value("range") == 300


def test_merge_does_not_regress_to_an_older_measurement():
    old = _points(("soc", "72", {"timestampUtc": "2026-09-20T10:00:00Z"}))
    older = _points(("soc", "60", {"timestampUtc": "2026-09-20T09:00:00Z"}))
    newer = _points(("soc", "65", {"timestampUtc": "2026-09-20T11:00:00Z"}))
    assert old.merged_with(older).value("soc") == 72
    assert old.merged_with(newer).value("soc") == 65


def test_merge_rebuilds_the_name_index():
    old = _points(("soc", "72", {}))
    assert old.value("soc") == 72  # builds the index
    merged = old.merged_with(_points(("range", "300", {})))
    assert merged.value("range") == 300
    assert merged.vin == "V"


def test_captured_at_of_merged_state_is_the_newest():
    full = Dataset.from_json(load_fixture("meb_dataset.json"))
    parked = Dataset.from_json(load_fixture("meb_reduced_dataset.json"))
    merged = full.merged_with(parked)
    assert merged.captured_at == datetime(2026, 9, 20, 11, 30, tzinfo=UTC)
    assert merged.value("battery_state_report.soc", SOC_KEY) == 72
    assert merged.value("battery_level_HV.value") == 67.0


def test_timestamp_formats_of_one_off_exports():
    # Nanoseconds, a space instead of the T, and a local offset.
    assert parse_timestamp("2026-09-22T19:03:38.052418548Z") == datetime(
        2026, 9, 22, 19, 3, 38, 52418, tzinfo=UTC
    )
    assert parse_timestamp("2026-09-13 13:52:26") == datetime(
        2026, 9, 13, 13, 52, 26, tzinfo=UTC
    )
    assert parse_timestamp("2026-09-22T20:49:29.000+02:00") == datetime(
        2026, 9, 22, 18, 49, 29, tzinfo=UTC
    )


def test_history_in_one_document_keeps_the_newest_reading():
    """A one-off export lists many readings per key, not in time order."""
    dataset = _points(
        ("soc", "70", {"timestampUtc": "2026-09-20T05:00:00Z"}),
        ("soc", "78", {"timestampUtc": "2026-09-22T05:03:21Z"}),
        ("soc", "50", {"timestampUtc": "2026-09-18T19:57:32Z"}),
        ("soc", None, {"timestampUtc": "2026-09-23T00:00:00Z"}),
    )
    assert dataset.value("soc") == 78
