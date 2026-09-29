"""What the curated sensors read, and how.

Each curated sensor lists the dataset fields it can be read from, in order of
preference, because the same quantity has a different name on MEB/SSP
vehicles (dotted names) and on older platforms (flat names). Both the sensors
and the metrics fed from the continuous feed read values through here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfLength,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.helpers.typing import StateType

from .api.dataset import Dataset, Value

type Converter = Callable[[Value], StateType]

#: Distance unit enumerations. Some platforms send the enumeration's name,
#: others its index, which the data dictionary documents as 0 = km, 1 = miles.
DISTANCE_UNITS: dict[str | int, str] = {
    "KM": UnitOfLength.KILOMETERS,
    "KILOMETER": UnitOfLength.KILOMETERS,
    "KILOMETERS": UnitOfLength.KILOMETERS,
    "MILES": UnitOfLength.MILES,
    "MILE": UnitOfLength.MILES,
    0: UnitOfLength.KILOMETERS,
    1: UnitOfLength.MILES,
}

#: Older platforms send this when a duration is not available.
_INVALID_MINUTES = 65535


def as_number(value: Value) -> int | float | None:
    """Pass numbers through; anything else is unknown."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def decikelvin(value: Value) -> StateType:
    """Convert the flat platforms' deci-Kelvin temperatures to Celsius."""
    number = as_number(value)
    return None if number is None else round(number / 10 - 273.15, 1)


def minutes_as_seconds(value: Value) -> StateType:
    """Convert minutes to seconds, dropping the "not available" marker."""
    number = as_number(value)
    if number is None or number >= _INVALID_MINUTES:
        return None
    return number * 60


def text(value: Value) -> StateType:
    """Return enumerations and free text lower-cased, for stable states."""
    return value.lower() if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class Field:
    """A dataset field a sensor can be read from."""

    name: str
    #: The data dictionary key the vehicles seen so far deliver this field
    #: under. Many names have several keys that mean different things, so the
    #: point is looked up by key first and by name only as a fallback.
    key: str | None = None
    convert: Converter = as_number
    #: A companion field holding the distance unit, such as ``mileage.unit``.
    unit_field: str | None = None


@dataclass(frozen=True, kw_only=True)
class EudaSensorDescription(SensorEntityDescription):
    """Describes a sensor fed from one of several dataset fields."""

    fields: tuple[Field, ...]


SENSORS: tuple[EudaSensorDescription, ...] = (
    EudaSensorDescription(
        key="battery_level",
        translation_key="battery_level",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        fields=(
            # Preferred: it is also in the reduced datasets a parked vehicle
            # delivers, where battery_state_report.soc is left out.
            Field("battery_level_HV.value", "ac1108b1-b8cc-3db9-a663-03d387e42223"),
            Field("battery_state_report.soc", "506cb83e-f99f-3af3-bbeb-0429b69a78d9"),
            Field("state_of_charge", "ae0294b4-1286-3e98-a818-1485b8d88430"),
            Field("hv_soc", "f89ed652-d104-3fa6-b7e2-ab7543309e7b"),
        ),
    ),
    EudaSensorDescription(
        key="target_battery_level",
        translation_key="target_battery_level",
        native_unit_of_measurement=PERCENTAGE,
        fields=(Field("settings.target_soc", "b3b04f31-b10e-38aa-b8ad-c0da7c06caea"),),
    ),
    EudaSensorDescription(
        key="charging_power",
        translation_key="charging_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        fields=(
            Field(
                "battery_state_report.charge_power",
                "c8cb205f-01c6-3c81-bda1-059b99ae6515",
            ),
        ),
    ),
    EudaSensorDescription(
        key="charging_time_remaining",
        translation_key="charging_time_remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        fields=(
            Field(
                "battery_state_report.remaining_charging_time_complete",
                "7405c11f-4d20-36d2-8381-18364aa1f444",
            ),
            Field(
                "remaining_charging_time",
                "cf28f7d9-6201-30b8-82e5-a461968d30dc",
                minutes_as_seconds,
            ),
        ),
    ),
    EudaSensorDescription(
        key="charging_state",
        translation_key="charging_state",
        fields=(
            Field(
                "charging_state_report.current_charge_state",
                "a08cca2b-ed42-37bc-b160-d015ce205d3d",
                text,
            ),
            Field("charging_state", "9da735bb-c5d5-39f8-bf53-0fa2a367aa8f", text),
        ),
    ),
    EudaSensorDescription(
        key="plug_state",
        translation_key="plug_state",
        fields=(Field("plug_state", "c111830c-f959-30d2-859a-ea996190d864", text),),
    ),
    EudaSensorDescription(
        key="odometer",
        translation_key="odometer",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        fields=(
            Field(
                "mileage.value",
                "75d65f00-5fa8-334a-826d-e73e91fe5c8d",
                unit_field="mileage.unit",
            ),
            Field("mileage", "41c0805c-43e5-313e-9dfb-356cb8d20f7c"),
        ),
    ),
    EudaSensorDescription(
        key="range",
        translation_key="range",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        fields=(
            Field(
                "estimatedcruisingrangeprimary.value",
                "b9c90aa6-9495-362c-99c2-1963f8bcfe7b",
                unit_field="estimatedcruisingrangeprimary.unit",
            ),
            Field("cruising_range_combined", "153e8c40-4c6c-3c17-a11b-0ecc35d55b81"),
        ),
    ),
    EudaSensorDescription(
        key="fuel_level",
        translation_key="fuel_level",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        fields=(
            Field("fuel_level_current_level", "1503760b-5570-3001-8ffc-1bb6f464948e"),
        ),
    ),
    EudaSensorDescription(
        key="outside_temperature",
        translation_key="outside_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        fields=(
            Field("outdoor_temperature", "b6ea5ae8-53ff-386d-8415-1baa0602bbb6"),
            Field(
                "outside_temperature",
                "6810b781-e54a-35e8-af98-fcdefb54bac6",
                decikelvin,
            ),
        ),
    ),
)


def find_field(description: EudaSensorDescription, dataset: Dataset) -> Field | None:
    """Return the first of a sensor's fields the dataset has a value for."""
    for candidate in description.fields:
        if dataset.value(candidate.name, candidate.key) is not None:
            return candidate
    return None


def read_value(
    description: EudaSensorDescription, dataset: Dataset
) -> tuple[StateType, str | None]:
    """Return a curated sensor's value and unit in a dataset.

    The unit is the description's, unless a companion unit field switches a
    distance to miles.
    """
    field = find_field(description, dataset)
    unit = description.native_unit_of_measurement
    if field is None:
        return None, unit
    value = field.convert(dataset.value(field.name, field.key))
    if field.unit_field:
        reported = dataset.value(field.unit_field)
        if isinstance(reported, str):
            reported = reported.upper()
        if isinstance(reported, str | int) and reported in DISTANCE_UNITS:
            unit = DISTANCE_UNITS[reported]
    return value, unit
