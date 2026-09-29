"""The calculated figures of one vehicle, kept up to date and stored.

Two trackers are kept apart:

* ``history``, built from the latest one-off export imported with the
  ``import_history`` action, and replaced by the next import;
* ``live``, fed from the continuous feed as its datasets arrive.

They cover separate periods (an import only uses what came before the live
tracker's first reading), so their weeks, months and capacity estimates are
simply added up for display. Running totals for the Energy dashboard come
from the live tracker alone; the history of those totals goes into long-term
statistics instead, where a sudden jump would not count as a real charge.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api.dataset import Dataset, DatasetFile
from .api.metrics import Observation, Summary, VehicleMetrics, summarize
from .const import DOMAIN
from .descriptions import SENSORS, read_value

STORAGE_VERSION = 1
#: How long after a change the figures are written to disk.
SAVE_DELAY = 60

_KM_PER_MILE = 1.609344
_DESCRIPTIONS = {description.key: description for description in SENSORS}


def observation_from(file: DatasetFile, dataset: Dataset) -> Observation | None:
    """Return the readings a freshly delivered dataset holds for the metrics.

    Only the points in this dataset are used, never the merged state: a
    reduced dataset leaves the charging power out, and the last one seen
    would otherwise look like a charge that never ends.
    """
    when = dataset.captured_at or file.created
    if when is None:
        return None

    def read(key: str) -> float | None:
        value, unit = read_value(_DESCRIPTIONS[key], dataset)
        if not isinstance(value, int | float) or isinstance(value, bool):
            return None
        return value * _KM_PER_MILE if unit == "mi" else float(value)

    charge_type = None
    for name in ("charging_state_report.charge_type", "charge_type"):
        raw = dataset.value(name)
        if isinstance(raw, str):
            text = raw.upper()
            charge_type = "DC" if "DC" in text else "AC" if "AC" in text else None
            break

    observation = Observation(
        time=when,
        soc=read("battery_level"),
        odometer=read("odometer"),
        charge_power=read("charging_power"),
        charge_type=charge_type,
        temperature=read("outside_temperature"),
    )
    if (
        observation.soc is None
        and observation.odometer is None
        and observation.charge_power is None
        and observation.temperature is None
    ):
        return None
    return observation


class MetricsRuntime:
    """Holds a vehicle's trackers, feeds them and stores them."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Create empty trackers; call :meth:`async_load` to restore them."""
        self.hass = hass
        self._store: Store[dict] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.metrics"
        )
        self.history: VehicleMetrics | None = None
        self.live = VehicleMetrics(self._tz, capacity=self._capacity)
        self.summary: Summary = summarize(self.live)

    @property
    def _tz(self):  # noqa: ANN202 - a tzinfo
        return dt_util.get_default_time_zone()

    def _capacity(self) -> float | None:
        return self.summary.capacity

    async def async_load(self) -> None:
        """Restore the trackers stored by an earlier run."""
        data = await self._store.async_load() or {}
        if history := data.get("history"):
            self.history = VehicleMetrics.from_dict(history, self._tz)
        if live := data.get("live"):
            self.live = VehicleMetrics.from_dict(
                live, self._tz, capacity=self._capacity
            )
        self._refresh()

    @callback
    def _refresh(self) -> None:
        trackers = [t for t in (self.history, self.live) if t is not None]
        self.summary = summarize(*trackers)

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self._data, SAVE_DELAY)

    def _data(self) -> dict:
        return {
            "history": self.history.to_dict() if self.history is not None else None,
            "live": self.live.to_dict(),
        }

    @callback
    def add_datasets(self, received: list[tuple[DatasetFile, Dataset]]) -> None:
        """Feed the datasets the continuous feed just delivered."""
        added = False
        for file, dataset in received:
            if (observation := observation_from(file, dataset)) is not None:
                self.live.add(observation)
                added = True
        if added:
            self._refresh()
            self._save()

    @property
    def live_since(self) -> datetime | None:
        """When the live tracker got its first reading, if it has."""
        return self.live.first_time

    async def async_set_history(self, history: VehicleMetrics) -> None:
        """Replace the imported history, and store it straight away."""
        self.history = history
        self._refresh()
        await self._store.async_save(self._data())

    async def async_flush(self) -> None:
        """Write pending changes now, such as when the entry unloads."""
        await self._store.async_save(self._data())


def this_and_last_week(now: datetime) -> tuple[str, str]:
    """Return the keys of the current and the previous ISO week."""
    return now.strftime("%G-W%V"), (now - timedelta(days=7)).strftime("%G-W%V")


def this_and_last_month(now: datetime) -> tuple[str, str]:
    """Return the keys of the current and the previous month."""
    first = now.replace(day=1)
    return now.strftime("%Y-%m"), (first - timedelta(days=1)).strftime("%Y-%m")
