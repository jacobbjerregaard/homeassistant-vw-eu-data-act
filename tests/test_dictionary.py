"""Tests for the shipped data dictionary."""

import pytest
from euda_api.dataset import DataPoint
from euda_api.dictionary import load_data_dictionary

from tests.conftest import load_fixture


@pytest.fixture(scope="module")
def dictionary():
    return load_data_dictionary()


def test_every_documented_point_is_loaded(dictionary):
    assert len(dictionary) == 1141
    assert all(entry.name for entry in dictionary.values())


def test_entry_contents(dictionary):
    entry = dictionary["6810b781-e54a-35e8-af98-fcdefb54bac6"]
    assert entry.name == "outside_temperature"
    assert entry.unit == "dK"
    assert entry.type == "int"
    assert entry.description == "Temperature outside the vehicle in deci-Kelvin"


def test_parser_repaired_wrapping_and_encoding(dictionary):
    entry = dictionary["19ec0834-f150-3c73-b8c4-7069e2f79df1"]
    # A friendly name wrapped over two lines in the PDF...
    assert entry.name == "Remaining Charge Time Complete"
    assert entry.clusters == ("Charging",)
    # ...an identifier wrapped mid-word...
    assert dictionary["1c62bf0a-8088-332c-9399-3b8cb0573099"].name == (
        "remaining_climatisation_time"
    )
    # ...and no UTF-8-read-as-Latin-1 damage left anywhere.
    for entry in dictionary.values():
        assert "Â" not in (entry.description or "")
        assert "ï¿½" not in (entry.description or "")


def test_fixture_keys_are_documented(dictionary):
    for item in load_fixture("flat_dataset.json")["Data"]:
        assert dictionary[item["key"]].name == item["dataFieldName"]


@pytest.mark.parametrize(
    ("raw", "expected"), [("2400s", True), ("0s", True), ("2400", False), ("", False)]
)
def test_is_duration(raw, expected):
    assert DataPoint("k", "n", raw).is_duration is expected
