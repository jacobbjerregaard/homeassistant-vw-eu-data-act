"""Sensors for the VW Group EU Data Act integration.

Each sensor lists the dataset fields it can be read from, in order of
preference, because the same quantity has a different name on MEB/SSP
vehicles (dotted names) and on older platforms (flat names). A sensor is
created once one of its fields shows up in a dataset, so a petrol car does not
get a battery sensor it could never fill.

Besides those curated sensors, every data point in a dataset gets a sensor of
its own, disabled by default and named after the point in VW's data
dictionary. Units come from the dictionary where it states one that can be
mapped with confidence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .api.dataset import DataPoint, Dataset, Value
from .api.dictionary import DictionaryEntry
from .coordinator import EudaConfigEntry, EudaCoordinator, VehicleData
from .entity import EudaEntity

# Entities only change when a new dataset is downloaded; parallel updates are
# coordinated centrally.
PARALLEL_UPDATES = 0

type Converter = Callable[[Value], StateType]

#: Distance unit enumerations. Some platforms send the enumeration's name,
#: others its index, which the data dictionary documents as 0 = km, 1 = miles.
_DISTANCE_UNITS: dict[str | int, str] = {
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


def _number(value: Value) -> int | float | None:
    """Pass numbers through; anything else is unknown."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _decikelvin(value: Value) -> StateType:
    """Convert the flat platforms' deci-Kelvin temperatures to Celsius."""
    number = _number(value)
    return None if number is None else round(number / 10 - 273.15, 1)


def _minutes_as_seconds(value: Value) -> StateType:
    """Convert minutes to seconds, dropping the "not available" marker."""
    number = _number(value)
    if number is None or number >= _INVALID_MINUTES:
        return None
    return number * 60


def _text(value: Value) -> StateType:
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
    convert: Converter = _number
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
                _minutes_as_seconds,
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
                _text,
            ),
            Field("charging_state", "9da735bb-c5d5-39f8-bf53-0fa2a367aa8f", _text),
        ),
    ),
    EudaSensorDescription(
        key="plug_state",
        translation_key="plug_state",
        fields=(Field("plug_state", "c111830c-f959-30d2-859a-ea996190d864", _text),),
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
                _decikelvin,
            ),
        ),
    ),
)


def _find_field(description: EudaSensorDescription, dataset: Dataset) -> Field | None:
    """Return the first of a sensor's fields the dataset has a value for."""
    for candidate in description.fields:
        if dataset.value(candidate.name, candidate.key) is not None:
            return candidate
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EudaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors, adding more as new fields appear in later datasets."""
    coordinator = entry.runtime_data
    added: set[str] = set()
    added_points: set[str] = set()

    @callback
    def _async_add_new() -> None:
        if coordinator.data is None:
            return
        dataset = coordinator.data.dataset
        new: list[SensorEntity] = [
            EudaSensor(coordinator, description)
            for description in SENSORS
            if description.key not in added and _find_field(description, dataset)
        ]
        added.update(entity.entity_description.key for entity in new)
        for key, point in dataset.points.items():
            if key not in added_points:
                added_points.add(key)
                new.append(
                    EudaDataPointSensor(
                        coordinator, point, coordinator.dictionary.get(key)
                    )
                )
        async_add_entities(new)

    _async_add_new()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new))
    async_add_entities(
        [
            EudaTimestampSensor(
                coordinator, "last_seen", lambda d: d.dataset.captured_at
            ),
            EudaTimestampSensor(
                coordinator, "dataset_created", lambda d: d.file.created
            ),
        ]
    )


class EudaSensor(EudaEntity, SensorEntity):
    """A vehicle value read from the newest dataset."""

    entity_description: EudaSensorDescription

    def __init__(
        self, coordinator: EudaCoordinator, description: EudaSensorDescription
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._update_state()

    def _update_state(self) -> None:
        """Read the value and unit from the current state, once per update."""
        dataset = self.coordinator.data.dataset
        field = _find_field(self.entity_description, dataset)
        unit = self.entity_description.native_unit_of_measurement
        value: StateType = None
        if field is not None:
            value = field.convert(dataset.value(field.name, field.key))
            if field.unit_field:
                # A companion unit field can switch a distance to miles.
                reported = dataset.value(field.unit_field)
                if isinstance(reported, str):
                    reported = reported.upper()
                if isinstance(reported, str | int) and reported in _DISTANCE_UNITS:
                    unit = _DISTANCE_UNITS[reported]
        self._attr_native_value = value
        self._attr_native_unit_of_measurement = unit

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        super()._handle_coordinator_update()


class EudaTimestampSensor(EudaEntity, SensorEntity):
    """How fresh the data is; diagnostic."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: EudaCoordinator,
        key: str,
        getter: Callable[[VehicleData], datetime | None],
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._getter = getter

    @property
    def native_value(self) -> Any:
        """Return the timestamp."""
        return self._getter(self.coordinator.data)


@dataclass(frozen=True, slots=True)
class _Unit:
    """How a unit named in the data dictionary maps onto Home Assistant."""

    unit: str
    device_class: SensorDeviceClass | None = None
    convert: Converter = _number


#: Units from the data dictionary that can be mapped with confidence. Ones it
#: states ambiguously, such as "10kPA / Bar / PSI/ kPA", are left out.
_DICTIONARY_UNITS: dict[str, _Unit] = {
    "%": _Unit(PERCENTAGE),
    "km": _Unit(UnitOfLength.KILOMETERS, SensorDeviceClass.DISTANCE),
    "min": _Unit(UnitOfTime.MINUTES, SensorDeviceClass.DURATION),
    "day": _Unit(UnitOfTime.DAYS, SensorDeviceClass.DURATION),
    "days": _Unit(UnitOfTime.DAYS, SensorDeviceClass.DURATION),
    "kW": _Unit(UnitOfPower.KILO_WATT, SensorDeviceClass.POWER),
    "kWh": _Unit(UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY_STORAGE),
    "km/h": _Unit(UnitOfSpeed.KILOMETERS_PER_HOUR, SensorDeviceClass.SPEED),
    "kmPerHour": _Unit(UnitOfSpeed.KILOMETERS_PER_HOUR, SensorDeviceClass.SPEED),
    "bar": _Unit(UnitOfPressure.BAR, SensorDeviceClass.PRESSURE),
    "°C": _Unit(UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE),
    "dK": _Unit(UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE, _decikelvin),
    "l": _Unit(UnitOfVolume.LITERS, SensorDeviceClass.VOLUME_STORAGE),
    "V": _Unit(UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE),
    "1/min": _Unit(REVOLUTIONS_PER_MINUTE),
}

_DURATION = _Unit(UnitOfTime.SECONDS, SensorDeviceClass.DURATION)

#: Home Assistant rejects longer states.
_MAX_STATE_LENGTH = 255


class EudaDataPointSensor(EudaEntity, SensorEntity):
    """One data point, exactly as the vehicle reports it.

    These are disabled by default: a dataset holds a hundred or more points,
    most of them only interesting when working out what a vehicle sends.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: EudaCoordinator,
        point: DataPoint,
        entry: DictionaryEntry | None,
    ) -> None:
        """Initialise the sensor from the first dataset the point appeared in."""
        super().__init__(coordinator, point.key)
        self._key = point.key
        # The dataset's name is used even when the dictionary has one, as it
        # is what the portal and the diagnostics show. The dictionary uses a
        # friendlier name for a few points, but also a "[*]" placeholder
        # where the dataset has the actual array index.
        self._attr_name = point.name
        self._attr_extra_state_attributes = {
            "key": point.key,
            "description": entry.description if entry else None,
            "clusters": list(entry.clusters) if entry else [],
        }

        unit = _DICTIONARY_UNITS.get(entry.unit or "") if entry else None
        if unit is None and point.is_duration:
            unit = _DURATION
        self._unit = unit
        if unit is not None:
            self._attr_native_unit_of_measurement = unit.unit
            self._attr_device_class = unit.device_class
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def _point(self) -> DataPoint | None:
        return self.coordinator.data.dataset.points.get(self._key)

    @property
    def available(self) -> bool:
        """Unavailable while the newest dataset does not include this point."""
        return super().available and self._point is not None

    @property
    def native_value(self) -> StateType:
        """Return the value, converted when it has a unit."""
        point = self._point
        if point is None:
            return None
        value = point.value
        if self._unit is not None:
            return self._unit.convert(value)
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, str):
            return value[:_MAX_STATE_LENGTH]
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Add when the vehicle measured the value, where it says."""
        attributes = dict(self._attr_extra_state_attributes)
        if (point := self._point) is not None and point.timestamp is not None:
            attributes["measured"] = point.timestamp.isoformat()
        return attributes
