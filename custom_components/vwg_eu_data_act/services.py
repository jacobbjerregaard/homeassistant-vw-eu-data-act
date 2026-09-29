"""Actions of the VW Group EU Data Act integration.

``import_history`` adds the history in a one-off export from the portal to
Home Assistant's long-term statistics, so the statistics and history graphs
reach back to before the integration was set up.

The history goes into the curated sensor's own statistics when that sensor
exists. When it does not, for example because the continuous feed has not
delivered anything yet, it goes into an external statistic of this
integration instead, which statistics graph cards can show all the same.

Only the time before the statistic's first existing hour is filled in, so
the import never overwrites what Home Assistant recorded itself, and
running it again with the same export changes nothing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, cast

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .api.exception import EudaError
from .api.export import ExportHistory, HourlyStat, read_export_file
from .const import CONF_VIN, DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_IMPORT_HISTORY = "import_history"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_PATH = "path"

IMPORT_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_PATH): cv.string,
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's actions."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        partial(_async_import_history, hass),
        schema=IMPORT_HISTORY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _error(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


async def _async_import_history(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Import the history in a one-off export into long-term statistics."""
    if "recorder" not in hass.config.components:
        raise _error("recorder_not_loaded")

    entry = hass.config_entries.async_get_entry(call.data[ATTR_CONFIG_ENTRY_ID])
    if entry is None or entry.domain != DOMAIN:
        raise _error("entry_not_found")

    path = Path(call.data[ATTR_PATH])
    if not path.is_absolute():
        path = Path(hass.config.path(str(path)))

    def check_path() -> str | None:
        if not hass.config.is_allowed_path(str(path)):
            return "path_not_allowed"
        if not path.is_file():
            return "file_not_found"
        return None

    if problem := await hass.async_add_executor_job(check_path):
        raise _error(problem, path=str(path))

    try:
        history = await hass.async_add_executor_job(read_export_file, path)
    except EudaError as err:
        raise _error("unreadable_export", error=str(err)) from err

    vin: str = entry.data[CONF_VIN]
    if history.vin is not None and history.vin != vin:
        raise _error("wrong_vehicle")

    imported = {
        sensor: await _async_import_series(
            hass, entry.title, vin, sensor, hours, history
        )
        for sensor, hours in history.series.items()
    }
    _LOGGER.info(
        "Imported history from an export: %s",
        ", ".join(f"{key} {result['hours']} h" for key, result in imported.items()),
    )
    return cast(ServiceResponse, {"readings": history.readings, "series": imported})


async def _async_import_series(
    hass: HomeAssistant,
    title: str,
    vin: str,
    sensor: str,
    hours: list[HourlyStat],
    history: ExportHistory,
) -> dict[str, Any]:
    """Import one series; return what was done, for the action's response."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import (
        STATISTIC_UNIT_TO_UNIT_CONVERTER,
        async_add_external_statistics,
        async_import_statistics,
        get_metadata,
        statistics_during_period,
    )

    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{vin}_{sensor}"
    )
    if entity_id is not None:
        statistic_id, source, name = entity_id, "recorder", None
    else:
        statistic_id = f"{DOMAIN}:{vin.lower()}_{sensor}"
        source, name = DOMAIN, f"{title} {sensor.replace('_', ' ')}"

    recorder = get_instance(hass)
    existing_meta = await recorder.async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={statistic_id})
    )

    # Statistics must stay in the unit they are already kept in, which for a
    # sensor follows its display unit (miles, say, instead of km).
    native = history.unit(sensor)
    unit_class: str | None
    if statistic_id in existing_meta:
        meta = cast(dict[str, Any], existing_meta[statistic_id][1])
        unit = meta["unit_of_measurement"]
        # Keep the existing statistic's unit class.
        unit_class = meta.get("unit_class")
    else:
        if entity_id is not None and (state := hass.states.get(entity_id)):
            unit = state.attributes.get("unit_of_measurement", native)
        else:
            unit = native
        # Derive it the way the sensor recorder does, from the unit.
        target = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(unit)
        unit_class = target.UNIT_CLASS if target is not None else None
    converter = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(native)
    if unit != native and (converter is None or unit not in converter.VALID_UNITS):
        return {"statistic_id": statistic_id, "hours": 0, "skipped": f"unit {unit}"}

    def convert(value: float) -> float:
        if unit == native or converter is None:
            return value
        return converter.convert(value, native, unit)

    # Read the existing rows in the unit they are stored in. By default they
    # come back in the sensor's display unit, which can differ, and the
    # odometer's sum below is anchored on them.
    stored = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(unit)
    rows = (
        await recorder.async_add_executor_job(
            statistics_during_period,
            hass,
            hours[0].start,
            None,
            {statistic_id},
            "hour",
            {stored.UNIT_CLASS: unit} if stored is not None else None,
            {"state", "sum"},
        )
    ).get(statistic_id, [])
    first = rows[0] if rows else None
    first_start = _row_start(first["start"]) if first else None
    todo = [hour for hour in hours if first_start is None or hour.start < first_start]
    if not todo:
        return {"statistic_id": statistic_id, "hours": 0}

    total = history.is_total(sensor)
    statistics: list[dict[str, Any]]
    if total:
        # A running total also needs a sum that joins up with the existing
        # statistics: count back from their first hour, so the sum there is
        # unchanged and every earlier hour is lower by the distance between.
        states = [convert(hour.last) for hour in todo]
        first_state = first.get("state") if first is not None else None
        first_sum = first.get("sum") if first is not None else None
        if first_state is not None and first_sum is not None:
            anchor_state, anchor_sum = float(first_state), float(first_sum)
        else:
            anchor_state, anchor_sum = states[0], 0.0
        statistics = [
            {
                "start": hour.start,
                "state": state,
                "sum": anchor_sum - (anchor_state - state),
            }
            for hour, state in zip(todo, states, strict=True)
        ]
    else:
        statistics = [
            {
                "start": hour.start,
                "mean": convert(hour.mean),
                "min": convert(hour.min),
                "max": convert(hour.max),
            }
            for hour in todo
        ]

    metadata = _metadata(
        statistic_id,
        source,
        name,
        unit,
        has_mean=not total,
        has_sum=total,
        unit_class=unit_class,
    )
    if source == "recorder":
        async_import_statistics(hass, cast(Any, metadata), cast(Any, statistics))
    else:
        async_add_external_statistics(hass, cast(Any, metadata), cast(Any, statistics))
    return {
        "statistic_id": statistic_id,
        "hours": len(todo),
        "from": todo[0].start.isoformat(),
        "to": todo[-1].start.isoformat(),
    }


def _metadata(
    statistic_id: str,
    source: str,
    name: str | None,
    unit: str | None,
    *,
    has_mean: bool,
    has_sum: bool,
    unit_class: str | None,
) -> dict[str, Any]:
    """Build statistics metadata for the running Home Assistant version.

    ``mean_type`` and ``unit_class`` replaced ``has_mean`` in 2025, and are
    required from 2026.11; versions from before them only know ``has_mean``.
    """
    metadata: dict[str, Any] = {
        "has_sum": has_sum,
        "name": name,
        "source": source,
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
    }
    try:
        from homeassistant.components.recorder.models import StatisticMeanType
    except ImportError:  # pragma: no cover - older Home Assistant
        metadata["has_mean"] = has_mean
        return metadata
    metadata["mean_type"] = (
        StatisticMeanType.ARITHMETIC if has_mean else StatisticMeanType.NONE
    )
    metadata["unit_class"] = unit_class
    return metadata


def _row_start(start: float | datetime) -> datetime:
    """Return a statistics row's start, which is a timestamp in current versions."""
    if isinstance(start, datetime):
        return start
    return datetime.fromtimestamp(start, UTC)
