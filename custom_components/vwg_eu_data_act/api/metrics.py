"""Figures calculated from a vehicle's readings over time.

The portal reports raw readings: state of charge, odometer, charging power,
outside temperature. This module turns a time-ordered stream of them into the
figures an owner actually asks about:

* energy charged into the battery, and energy used from it;
* consumption (kWh/100 km) per week and per month;
* the battery's usable capacity, estimated from charging sessions;
* energy used while parked (preconditioning, standby);
* consumption per outside temperature band;
* charging sessions: count, energy, average and peak power, AC or DC.

All energy figures are battery-side. What the charger draws from the grid is
higher by the charging losses, which the data does not show.

State of charge is reported in whole percent and jitters, so energy used is
taken from the *net* change between readings: counting only the drops
overstates it by about a fifth. It is converted to kWh with the estimated
capacity, which comes from the charging sessions: energy integrated from the
charging power, divided by the percentage gained.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

#: Charging power readings further apart than this are not integrated.
INTEGRATION_GAP = timedelta(minutes=20)
#: A pause in charging longer than this ends the session.
SESSION_GAP = timedelta(minutes=30)
#: A session must add this much charge to estimate the capacity from it.
CAPACITY_MIN_GAIN = 20.0
#: ... and have no hole in its power readings longer than this. One-off
#: exports report power every 5 to 10 minutes while charging, the continuous
#: feed every 15; allowing up to 20 minutes barely moves the estimates of a
#: real vehicle's history (a median of 76.3 kWh rather than 75.2).
CAPACITY_MAX_GAP = INTEGRATION_GAP
#: After a session, the state of charge takes a while to settle; the first
#: reading within this time is taken as the charge it ended at.
SETTLE_TIME = timedelta(hours=2)
#: Sessions adding less than this are power blips, not charging.
MIN_SESSION_KWH = 0.5
#: A rise of more than this many percent between two readings, with no
#: charging power seen and no session just ended, is a charge the readings
#: missed, as can happen with the continuous feed's 15-minute snapshots.
UNSEEN_CHARGE = 3.0
#: Capacity estimates kept for the current figure, and for the trend.
CAPACITY_WINDOW = 20
CAPACITY_HISTORY = 400
#: A month needs this many estimates to appear in the capacity trend.
CAPACITY_MONTH_MIN = 3
#: Weeks and months need this much driving before consumption is given.
MIN_DISTANCE_KM = 20.0
#: AC charging stops at 22 kW; anything above is DC fast charging.
AC_LIMIT_KW = 22.0

#: Outside temperature bands for consumption, as (key, upper bound in °C).
TEMPERATURE_BANDS: tuple[tuple[str, float], ...] = (
    ("below_0", 0.0),
    ("0_to_10", 10.0),
    ("10_to_20", 20.0),
    ("above_20", float("inf")),
)


@dataclass(frozen=True, slots=True)
class Observation:
    """Readings taken at one moment; any of them may be missing."""

    time: datetime
    soc: float | None = None
    odometer: float | None = None
    charge_power: float | None = None
    #: "AC" or "DC", when the vehicle says.
    charge_type: str | None = None
    temperature: float | None = None


def _band(temperature: float | None) -> str | None:
    if temperature is None:
        return None
    return next(key for key, upper in TEMPERATURE_BANDS if temperature < upper)


def _new_bucket() -> dict[str, float]:
    return {
        "km": 0.0,
        "drive": 0.0,
        "parked": 0.0,
        "charged": 0.0,
        "dc": 0.0,
        "temp_sum": 0.0,
        "temp_n": 0.0,
    }


class VehicleMetrics:
    """Accumulates figures from observations fed to it in time order.

    ``drive`` and ``parked`` are percentage points of charge used; they are
    converted to kWh when reported, with the capacity estimate of that time.
    Energy totals meant for sensors (``charged_kwh``, ``used_kwh`` and
    ``parked_kwh``) are converted as they accrue, so they never jump when the
    estimate moves.
    """

    def __init__(
        self,
        tz: tzinfo = UTC,
        *,
        capacity: Callable[[], float | None] | None = None,
        record_hours: bool = False,
    ) -> None:
        """Start empty.

        :param tz: Time zone weeks and months are counted in.
        :param capacity: Where to get the capacity for converting charge
            used to kWh. Defaults to this tracker's own estimate.
        :param record_hours: Keep the running totals at the end of every
            hour, for importing them as statistics.
        """
        self.tz = tz
        self._capacity_source = capacity
        self.record_hours = record_hours

        self.first_time: datetime | None = None
        self.last_time: datetime | None = None
        self.weeks: dict[str, dict[str, float]] = {}
        self.months: dict[str, dict[str, float]] = {}
        self.bands: dict[str, dict[str, float]] = {}
        self.capacity_estimates: list[tuple[datetime, float]] = []

        self.charged_kwh = 0.0
        self.used_kwh = 0.0
        self.parked_kwh = 0.0
        self.sessions = 0
        self.last_session: dict[str, Any] | None = None
        #: Charge used before any capacity was known, in percentage points.
        self._pending_drive = 0.0
        self._pending_parked = 0.0

        self._soc: float | None = None
        self._soc_time: datetime | None = None
        self._odometer: float | None = None
        self._odometer_at_soc: float | None = None
        self._temperature: float | None = None
        self._power: float | None = None
        self._power_time: datetime | None = None
        self._charge_type: str | None = None
        self._session: dict[str, Any] | None = None
        #: A finished session waiting for the settled state of charge.
        self._settling: dict[str, Any] | None = None
        self._last_session_end: datetime | None = None

        #: Running totals at the end of each hour: (charged, used, parked,
        #: sessions), filled only with ``record_hours``.
        self.hours: dict[datetime, tuple[float, float, float, int]] = {}

    # -- feeding -------------------------------------------------------------

    def add_all(self, observations: Iterable[Observation]) -> None:
        """Feed observations, which must be in time order."""
        for observation in observations:
            self.add(observation)

    def add(self, obs: Observation) -> None:
        """Feed one observation; it must not be older than the last one."""
        if self.last_time is not None and obs.time < self.last_time:
            return
        if self.first_time is None:
            self.first_time = obs.time
        self.last_time = obs.time

        # A charging pause longer than this, whatever the reading, ends it.
        if self._session is not None and obs.time - self._session["last"] > SESSION_GAP:
            self._end_session()
        if (
            self._settling is not None
            and obs.time - self._settling["end"] > SETTLE_TIME
        ):
            self._finish_settling(None)

        if obs.temperature is not None:
            self._temperature = obs.temperature
            for bucket in self._buckets(obs.time):
                bucket["temp_sum"] += obs.temperature
                bucket["temp_n"] += 1
        if obs.charge_type in ("AC", "DC"):
            self._charge_type = obs.charge_type
        if obs.odometer is not None:
            self._add_odometer(obs.time, obs.odometer)
        # The charge first: a snapshot that starts a session holds the state
        # of charge it starts from.
        if obs.soc is not None:
            self._add_soc(obs.time, obs.soc)
        if obs.charge_power is not None:
            self._add_power(obs.time, obs.charge_power)
        if self.record_hours:
            hour = obs.time.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
            if not self.hours:
                # A zero hour before the first, so statistics show the first
                # hour's change too: they are the difference between hours.
                self.hours[hour - timedelta(hours=1)] = (0.0, 0.0, 0.0, 0)
            self.hours[hour] = (
                self.charged_kwh,
                self.used_kwh,
                self.parked_kwh,
                self.sessions,
            )

    def _buckets(self, when: datetime) -> tuple[dict[str, float], dict[str, float]]:
        local = when.astimezone(self.tz)
        week = local.strftime("%G-W%V")
        month = local.strftime("%Y-%m")
        return (
            self.weeks.setdefault(week, _new_bucket()),
            self.months.setdefault(month, _new_bucket()),
        )

    def _add_odometer(self, when: datetime, odometer: float) -> None:
        if self._odometer is not None and odometer > self._odometer:
            distance = odometer - self._odometer
            for bucket in self._buckets(when):
                bucket["km"] += distance
            if (band := _band(self._temperature)) is not None:
                self.bands.setdefault(band, {"km": 0.0, "drive": 0.0})["km"] += distance
        if self._odometer is None or odometer >= self._odometer:
            self._odometer = odometer

    def _add_power(self, when: datetime, power: float) -> None:
        previous, previous_time = self._power, self._power_time
        self._power, self._power_time = power, when
        session = self._session

        if power > 0 and session is None:
            session = self._session = {
                "start": when,
                "last": when,
                "kwh": 0.0,
                "peak": power,
                "soc_start": self._soc,
                "max_gap": 0.0,
                "dc": False,
            }
            previous = None  # Nothing before the session is integrated.
        if session is None:
            return

        if previous is not None and previous_time is not None:
            gap = when - previous_time
            session["max_gap"] = max(session["max_gap"], gap.total_seconds())
            if gap <= INTEGRATION_GAP and (previous > 0 or power > 0):
                kwh = (previous + power) / 2 * gap.total_seconds() / 3600
                session["kwh"] += kwh
                self._add_charged(when, kwh, dc=session["dc"])
        session["peak"] = max(session["peak"], power)
        if self._charge_type == "DC" or power > AC_LIMIT_KW:
            session["dc"] = True
        # A zero reading is often a pause within the session; it only ends
        # once charging has not resumed for SESSION_GAP.
        if power > 0:
            session["last"] = when

    def _add_charged(self, when: datetime, kwh: float, *, dc: bool) -> None:
        self.charged_kwh += kwh
        for bucket in self._buckets(when):
            bucket["charged"] += kwh
            if dc:
                bucket["dc"] += kwh

    def _end_session(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        self._last_session_end = session["last"]
        if session["kwh"] < MIN_SESSION_KWH:
            return
        self.sessions += 1
        hours = (session["last"] - session["start"]).total_seconds() / 3600
        self.last_session = {
            "start": session["start"].isoformat(),
            "end": session["last"].isoformat(),
            "energy": round(session["kwh"], 2),
            "average_power": round(session["kwh"] / hours, 1) if hours > 0 else None,
            "peak_power": round(session["peak"], 1),
            "type": "DC" if session["dc"] else "AC",
            "soc_start": session["soc_start"],
            "soc_end": self._soc,
        }
        self._settling = {
            "end": session["last"],
            "kwh": session["kwh"],
            "soc_start": session["soc_start"],
            "soc_last": self._soc,
            "dense": session["max_gap"] <= CAPACITY_MAX_GAP.total_seconds(),
        }
        # A reading taken after charging stopped is the settled charge.
        if self._soc_time is not None and self._soc_time > session["last"]:
            self._finish_settling(self._soc)

    def _finish_settling(self, soc: float | None) -> None:
        """Estimate the capacity from a finished session, if it allows."""
        settling, self._settling = self._settling, None
        if settling is None:
            return
        end = soc if soc is not None else settling["soc_last"]
        if end is None or settling["soc_start"] is None or not settling["dense"]:
            return
        gain = end - settling["soc_start"]
        if self.last_session is not None:
            self.last_session["soc_end"] = end
        if gain < CAPACITY_MIN_GAIN:
            return
        self.capacity_estimates.append((settling["end"], settling["kwh"] / gain * 100))
        del self.capacity_estimates[:-CAPACITY_HISTORY]
        self._convert_pending()

    def _add_soc(self, when: datetime, soc: float) -> None:
        previous, self._soc = self._soc, soc
        previous_time, self._soc_time = self._soc_time, when
        odometer_before, self._odometer_at_soc = self._odometer_at_soc, self._odometer
        if self._session is not None:
            return
        if self._settling is not None:
            self._finish_settling(soc)
        if previous is None or previous_time is None:
            return

        change = soc - previous
        if change > 0:
            recent_session = (
                self._last_session_end is not None
                and when - self._last_session_end <= SETTLE_TIME
            )
            if change > UNSEEN_CHARGE and not recent_session:
                # A charge between readings that never showed its power.
                capacity = self.capacity
                if capacity is not None:
                    self._add_charged(when, change / 100 * capacity, dc=False)
                self.sessions += 1
                return
            if recent_session:
                return  # The charge settling, not energy coming back.

        used = -change
        driving = (
            odometer_before is not None
            and self._odometer is not None
            and self._odometer > odometer_before
        )
        for bucket in self._buckets(when):
            bucket["drive" if driving else "parked"] += used
        if driving and (band := _band(self._temperature)) is not None:
            self.bands.setdefault(band, {"km": 0.0, "drive": 0.0})["drive"] += used

        capacity = self.capacity
        if capacity is None:
            if driving:
                self._pending_drive += used
            else:
                self._pending_parked += used
            return
        self.used_kwh += used / 100 * capacity
        if not driving:
            self.parked_kwh += used / 100 * capacity

    def _convert_pending(self) -> None:
        capacity = self.capacity
        if capacity is None:
            return
        pending = self._pending_drive + self._pending_parked
        self.used_kwh += pending / 100 * capacity
        self.parked_kwh += self._pending_parked / 100 * capacity
        self._pending_drive = self._pending_parked = 0.0

    # -- figures -------------------------------------------------------------

    @property
    def own_capacity(self) -> float | None:
        """The median of this tracker's recent capacity estimates, in kWh."""
        recent = [kwh for _when, kwh in self.capacity_estimates[-CAPACITY_WINDOW:]]
        return round(statistics.median(recent), 1) if recent else None

    @property
    def capacity(self) -> float | None:
        """The capacity used for converting charge to kWh."""
        if self._capacity_source is not None:
            return self._capacity_source()
        return self.own_capacity

    def to_dict(self) -> dict[str, Any]:
        """Return the state, for storing between restarts."""
        return {
            "first_time": _iso(self.first_time),
            "last_time": _iso(self.last_time),
            "weeks": self.weeks,
            "months": self.months,
            "bands": self.bands,
            "capacity_estimates": [
                [when.isoformat(), kwh] for when, kwh in self.capacity_estimates
            ],
            "charged_kwh": self.charged_kwh,
            "used_kwh": self.used_kwh,
            "parked_kwh": self.parked_kwh,
            "sessions": self.sessions,
            "last_session": self.last_session,
            "pending": [self._pending_drive, self._pending_parked],
            "soc": [self._soc, _iso(self._soc_time)],
            "odometer": [self._odometer, self._odometer_at_soc],
            "temperature": self._temperature,
            "power": [self._power, _iso(self._power_time)],
            "charge_type": self._charge_type,
            "last_session_end": _iso(self._last_session_end),
            "settling": None
            if self._settling is None
            else {**self._settling, "end": self._settling["end"].isoformat()},
            "session": None
            if self._session is None
            else {
                **self._session,
                "start": self._session["start"].isoformat(),
                "last": self._session["last"].isoformat(),
            },
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        tz: tzinfo = UTC,
        *,
        capacity: Callable[[], float | None] | None = None,
    ) -> VehicleMetrics:
        """Restore a tracker stored with :meth:`to_dict`."""
        metrics = cls(tz, capacity=capacity)
        metrics.first_time = _from_iso(data.get("first_time"))
        metrics.last_time = _from_iso(data.get("last_time"))
        metrics.weeks = data.get("weeks") or {}
        metrics.months = data.get("months") or {}
        metrics.bands = data.get("bands") or {}
        metrics.capacity_estimates = [
            (when, float(kwh))
            for raw, kwh in data.get("capacity_estimates") or []
            if (when := _from_iso(raw)) is not None
        ]
        metrics.charged_kwh = float(data.get("charged_kwh") or 0)
        metrics.used_kwh = float(data.get("used_kwh") or 0)
        metrics.parked_kwh = float(data.get("parked_kwh") or 0)
        metrics.sessions = int(data.get("sessions") or 0)
        metrics.last_session = data.get("last_session")
        metrics._pending_drive, metrics._pending_parked = data.get("pending") or [0, 0]
        soc, soc_time = data.get("soc") or [None, None]
        metrics._soc, metrics._soc_time = soc, _from_iso(soc_time)
        metrics._odometer, metrics._odometer_at_soc = data.get("odometer") or [
            None,
            None,
        ]
        metrics._temperature = data.get("temperature")
        power, power_time = data.get("power") or [None, None]
        metrics._power, metrics._power_time = power, _from_iso(power_time)
        metrics._charge_type = data.get("charge_type")
        metrics._last_session_end = _from_iso(data.get("last_session_end"))
        if (settling := data.get("settling")) and (
            end := _from_iso(settling.get("end"))
        ):
            metrics._settling = {**settling, "end": end}
        if session := data.get("session"):
            start, last = _from_iso(session["start"]), _from_iso(session["last"])
            if start is not None and last is not None:
                metrics._session = {**session, "start": start, "last": last}
        return metrics


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# -- combining a history import with the live feed ---------------------------


@dataclass
class Summary:
    """The figures of one or more trackers, combined for display."""

    capacity: float | None
    weeks: dict[str, dict[str, float]]
    months: dict[str, dict[str, float]]
    bands: dict[str, dict[str, float]]
    capacity_by_month: dict[str, float]
    sessions: int
    last_session: dict[str, Any] | None

    def consumption(self, bucket: dict[str, float] | None) -> float | None:
        """Return kWh/100 km for a week or month, or ``None``."""
        if bucket is None or self.capacity is None or bucket["km"] < MIN_DISTANCE_KM:
            return None
        used = (bucket["drive"] + bucket["parked"]) / 100 * self.capacity
        return round(used / bucket["km"] * 100, 1)

    def band_consumption(self, band: str) -> float | None:
        """Return kWh/100 km while driving in a temperature band."""
        data = self.bands.get(band)
        if data is None or self.capacity is None or data["km"] < MIN_DISTANCE_KM:
            return None
        return round(data["drive"] / 100 * self.capacity / data["km"] * 100, 1)

    def dc_share(self) -> float | None:
        """Return the share of energy charged by DC, in percent."""
        charged = sum(bucket["charged"] for bucket in self.months.values())
        if charged <= 0:
            return None
        dc = sum(bucket["dc"] for bucket in self.months.values())
        return round(dc / charged * 100, 1)

    def table(self, buckets: dict[str, dict[str, float]], last: int) -> dict[str, Any]:
        """Return the most recent periods' figures, for sensor attributes."""
        result: dict[str, Any] = {}
        for key in sorted(buckets)[-last:]:
            bucket = buckets[key]
            capacity = self.capacity
            result[key] = {
                "consumption": self.consumption(bucket),
                "distance": round(bucket["km"]),
                "charged": round(bucket["charged"], 1),
                "parked": round(bucket["parked"] / 100 * capacity, 1)
                if capacity is not None
                else None,
                "temperature": round(bucket["temp_sum"] / bucket["temp_n"], 1)
                if bucket["temp_n"]
                else None,
            }
        return result


def summarize(*trackers: VehicleMetrics) -> Summary:
    """Combine trackers covering different, non-overlapping periods."""
    weeks: dict[str, dict[str, float]] = {}
    months: dict[str, dict[str, float]] = {}
    bands: dict[str, dict[str, float]] = {}
    estimates: list[tuple[datetime, float]] = []
    sessions = 0
    last_session: dict[str, Any] | None = None
    for tracker in trackers:
        for target, source in ((weeks, tracker.weeks), (months, tracker.months)):
            for key, bucket in source.items():
                merged = target.setdefault(key, _new_bucket())
                for field, value in bucket.items():
                    merged[field] = merged.get(field, 0.0) + value
        for key, band in tracker.bands.items():
            merged = bands.setdefault(key, {"km": 0.0, "drive": 0.0})
            merged["km"] += band["km"]
            merged["drive"] += band["drive"]
        estimates.extend(tracker.capacity_estimates)
        sessions += tracker.sessions
        if tracker.last_session is not None and (
            last_session is None or tracker.last_session["end"] > last_session["end"]
        ):
            last_session = tracker.last_session

    estimates.sort()
    recent = [kwh for _when, kwh in estimates[-CAPACITY_WINDOW:]]
    by_month: dict[str, list[float]] = {}
    for when, kwh in estimates:
        by_month.setdefault(when.strftime("%Y-%m"), []).append(kwh)
    return Summary(
        capacity=round(statistics.median(recent), 1) if recent else None,
        weeks=weeks,
        months=months,
        bands=bands,
        capacity_by_month={
            month: round(statistics.median(values), 1)
            for month, values in sorted(by_month.items())
            if len(values) >= CAPACITY_MONTH_MIN
        },
        sessions=sessions,
        last_session=last_session,
    )
