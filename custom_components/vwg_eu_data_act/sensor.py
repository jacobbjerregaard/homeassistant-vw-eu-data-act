"""Sensors for the VW Group EU Data Act integration.

The curated sensors (see :mod:`.descriptions`) are created once one of their
fields shows up in a dataset, so a petrol car does not get a battery sensor
it could never fill.

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
    UnitOfEnergyDistance,
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
from homeassistant.util import dt as dt_util

from .api.dataset import DataPoint
from .api.dictionary import DictionaryEntry
from .coordinator import EudaConfigEntry, EudaCoordinator, VehicleData
from .descriptions import (
    SENSORS,
    Converter,
    EudaSensorDescription,
    as_number,
    decikelvin,
    find_field,
    read_value,
)
from .entity import EudaEntity
from .vehicle_metrics import MetricsRuntime, this_and_last_month, this_and_last_week

# Entities only change when a new dataset is downloaded; parallel updates are
# coordinated centrally.
PARALLEL_UPDATES = 0


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
            if description.key not in added and find_field(description, dataset)
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
    if coordinator.metrics is not None:
        async_add_entities(
            EudaMetricSensor(coordinator, coordinator.metrics, description)
            for description in METRIC_SENSORS
        )
    async_add_entities(
        [
            EudaTimestampSensor(
                coordinator, "last_seen", lambda d: d.dataset.captured_at
            ),
            EudaTimestampSensor(
                coordinator,
                "dataset_created",
                lambda d: d.file.created if d.file is not None else None,
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
        value, unit = read_value(self.entity_description, self.coordinator.data.dataset)
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
    convert: Converter = as_number


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
    "dK": _Unit(UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE, decikelvin),
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


# -- calculated figures -------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class EudaMetricDescription(SensorEntityDescription):
    """Describes a sensor showing a figure calculated over time."""

    value: Callable[[MetricsRuntime, datetime], StateType]
    attributes: Callable[[MetricsRuntime], dict[str, Any]] | None = None


def _week(offset: int) -> Callable[[MetricsRuntime, datetime], StateType]:
    def value(metrics: MetricsRuntime, now: datetime) -> StateType:
        key = this_and_last_week(now)[offset]
        return metrics.summary.consumption(metrics.summary.weeks.get(key))

    return value


def _month(offset: int) -> Callable[[MetricsRuntime, datetime], StateType]:
    def value(metrics: MetricsRuntime, now: datetime) -> StateType:
        key = this_and_last_month(now)[offset]
        return metrics.summary.consumption(metrics.summary.months.get(key))

    return value


def _band(band: str) -> Callable[[MetricsRuntime, datetime], StateType]:
    return lambda metrics, _now: metrics.summary.band_consumption(band)


def _last_charge(field: str) -> Callable[[MetricsRuntime, datetime], StateType]:
    def value(metrics: MetricsRuntime, _now: datetime) -> StateType:
        session = metrics.summary.last_session
        if session is None:
            return None
        raw = session.get(field)
        return raw.lower() if isinstance(raw, str) else raw

    return value


_CONSUMPTION: dict[str, Any] = {
    "device_class": SensorDeviceClass.ENERGY_DISTANCE,
    "native_unit_of_measurement": UnitOfEnergyDistance.KILO_WATT_HOUR_PER_100_KM,
    "state_class": SensorStateClass.MEASUREMENT,
    "suggested_display_precision": 1,
}
_TOTAL_ENERGY: dict[str, Any] = {
    "device_class": SensorDeviceClass.ENERGY,
    "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    "suggested_display_precision": 1,
}

METRIC_SENSORS: tuple[EudaMetricDescription, ...] = (
    # Energy, for the Energy dashboard. Live only: imported history goes into
    # their statistics instead, so it does not show up as one huge charge.
    EudaMetricDescription(
        key="energy_charged",
        translation_key="energy_charged",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda m, _now: round(m.live.charged_kwh, 3),
        **_TOTAL_ENERGY,
    ),
    EudaMetricDescription(
        key="energy_used",
        translation_key="energy_used",
        # Net of regenerated energy, so it can go down a little.
        state_class=SensorStateClass.TOTAL,
        value=lambda m, _now: round(m.live.used_kwh, 3),
        **_TOTAL_ENERGY,
    ),
    EudaMetricDescription(
        key="energy_used_parked",
        translation_key="energy_used_parked",
        state_class=SensorStateClass.TOTAL,
        value=lambda m, _now: round(m.live.parked_kwh, 3),
        **_TOTAL_ENERGY,
    ),
    EudaMetricDescription(
        key="charging_sessions",
        translation_key="charging_sessions",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda m, _now: m.live.sessions,
        # The count itself is live only, like the energy totals; this shows
        # the whole count, imported history included.
        attributes=lambda m: {"including_history": m.summary.sessions},
    ),
    # Consumption per calendar week and month.
    EudaMetricDescription(
        key="consumption_this_week",
        translation_key="consumption_this_week",
        value=_week(0),
        **_CONSUMPTION,
    ),
    EudaMetricDescription(
        key="consumption_last_week",
        translation_key="consumption_last_week",
        value=_week(1),
        attributes=lambda m: {"weeks": m.summary.table(m.summary.weeks, 26)},
        **_CONSUMPTION,
    ),
    EudaMetricDescription(
        key="consumption_this_month",
        translation_key="consumption_this_month",
        value=_month(0),
        **_CONSUMPTION,
    ),
    EudaMetricDescription(
        key="consumption_last_month",
        translation_key="consumption_last_month",
        value=_month(1),
        attributes=lambda m: {"months": m.summary.table(m.summary.months, 24)},
        **_CONSUMPTION,
    ),
    # Consumption while driving, by outside temperature.
    *(
        EudaMetricDescription(
            key=f"consumption_{band}",
            translation_key=f"consumption_{band}",
            value=_band(band),
            **_CONSUMPTION,
        )
        for band in ("below_0", "0_to_10", "10_to_20", "above_20")
    ),
    # Battery health.
    EudaMetricDescription(
        key="battery_capacity",
        translation_key="battery_capacity",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=lambda m, _now: m.summary.capacity,
        attributes=lambda m: {"months": m.summary.capacity_by_month},
    ),
    EudaMetricDescription(
        key="battery_health",
        translation_key="battery_health",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        # Only with the nominal capacity set in the entry's options.
        value=lambda m, _now: m.health(m.summary.capacity),
        attributes=lambda m: {
            "nominal_capacity": m.nominal_capacity,
            "months": {
                month: m.health(capacity)
                for month, capacity in m.summary.capacity_by_month.items()
            }
            if m.nominal_capacity
            else {},
        },
    ),
    # Charging.
    EudaMetricDescription(
        key="last_charge_energy",
        translation_key="last_charge_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value=_last_charge("energy"),
        attributes=lambda m: {
            key: (m.summary.last_session or {}).get(key)
            for key in ("start", "end", "soc_start", "soc_end")
        },
    ),
    EudaMetricDescription(
        key="last_charge_average_power",
        translation_key="last_charge_average_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=1,
        value=_last_charge("average_power"),
    ),
    EudaMetricDescription(
        key="last_charge_peak_power",
        translation_key="last_charge_peak_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=1,
        value=_last_charge("peak_power"),
    ),
    EudaMetricDescription(
        key="last_charge_type",
        translation_key="last_charge_type",
        device_class=SensorDeviceClass.ENUM,
        options=["ac", "dc"],
        value=_last_charge("type"),
    ),
    EudaMetricDescription(
        key="dc_share",
        translation_key="dc_share",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda m, _now: m.summary.dc_share(),
    ),
)


class EudaMetricSensor(EudaEntity, SensorEntity):
    """A figure calculated from the vehicle's readings over time."""

    entity_description: EudaMetricDescription

    def __init__(
        self,
        coordinator: EudaCoordinator,
        metrics: MetricsRuntime,
        description: EudaMetricDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._metrics = metrics
        self._update_state()

    def _update_state(self) -> None:
        description = self.entity_description
        self._attr_native_value = description.value(self._metrics, dt_util.now())
        if description.attributes is not None:
            self._attr_extra_state_attributes = description.attributes(self._metrics)

    @property
    def available(self) -> bool:
        """Figures stay available through a failed update of the feed."""
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        super()._handle_coordinator_update()
