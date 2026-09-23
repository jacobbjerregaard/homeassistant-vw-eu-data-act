"""The datasets the portal delivers, and how to read them.

A continuous data request makes the portal drop a ZIP file roughly every 15
minutes. Each holds one JSON document::

    {"vin": "...", "user_id": "...", "Data": [
        {"key": "<uuid>", "dataFieldName": "battery_state_report.soc",
         "value": "69", "timestampUtc": "2026-07-31T10:40:28.000Z"},
        ...
    ]}

Every value is a string. ``key`` identifies a data point in VW's data
dictionary and is the same for every vehicle. ``dataFieldName`` is readable,
but not unique: several report snapshots are merged into one array, so a name
such as ``car_captured_time`` can appear many times. ``timestampUtc`` is only
present on some platforms.

Field names also differ by platform: MEB/SSP vehicles (ID. family, Enyaq,
Born, Q4) use dotted names like ``battery_state_report.soc``, while older
MQB-based vehicles use flat ones like ``state_of_charge``.

A dataset is not a full snapshot. While the vehicle is parked and asleep the
portal delivers a reduced one that leaves most fields out, so the current
state of a vehicle is the delivered datasets merged in order; see
:meth:`Dataset.merged_with`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property
from typing import Any

#: Listing entries with this suffix are placeholders with no payload.
NO_CONTENT_SUFFIX = "_no_content_found.zip"

_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")
_DURATION_RE = re.compile(r"^(-?\d+(?:\.\d+)?)s$")
_FILENAME_TS_RE = re.compile(r"^\d{14}$")
#: Fractions of a second beyond microseconds, which one-off exports contain
#: ("…:38.052418548Z") and older Pythons refuse to parse.
_EXTRA_DIGITS_RE = re.compile(r"(\.\d{6})\d+")

type Value = str | int | float | bool | None


def parse_value(raw: str | None) -> Value:
    """Turn a raw dataset string into the most specific Python value.

    Booleans, integers, decimals and ``"<n>s"`` durations (returned as seconds)
    are recognised. Enumerations, timestamps and free text stay strings.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text):
        return float(text)
    if match := _DURATION_RE.match(text):
        seconds = float(match.group(1))
        return int(seconds) if seconds.is_integer() else seconds
    return text


def parse_timestamp(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 or epoch-milliseconds timestamp into UTC."""
    text = (raw or "").strip()
    if not text:
        return None
    if _INT_RE.match(text) and len(text) >= 12:
        try:
            return datetime.fromtimestamp(int(text) / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = _EXTRA_DIGITS_RE.sub(r"\1", text.replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class DataPoint:
    """One entry of a dataset's ``Data`` array."""

    key: str
    name: str
    raw: str
    timestamp: datetime | None = None

    @property
    def value(self) -> Value:
        """The parsed value."""
        return parse_value(self.raw)

    @property
    def is_duration(self) -> bool:
        """Whether the value was a ``"<n>s"`` duration, now in seconds."""
        return _DURATION_RE.match(self.raw.strip()) is not None


@dataclass
class Dataset:
    """A parsed dataset document, or several merged into one.

    ``points`` must not be changed after construction: lookups by name are
    indexed once.
    """

    vin: str
    points: dict[str, DataPoint] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Dataset:
        """Build a dataset from the JSON document inside a delivery ZIP."""
        points: dict[str, DataPoint] = {}
        for item in payload.get("Data") or []:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if not key:
                continue
            point = DataPoint(
                key=key,
                name=item.get("dataFieldName") or key,
                raw="" if item.get("value") is None else str(item["value"]),
                timestamp=parse_timestamp(item.get("timestampUtc")),
            )
            # A one-off export holds a history, many readings per key in no
            # particular order; keep the newest, by the same rule as merging.
            if _supersedes(point, points.get(key)):
                points[key] = point
        return cls(vin=str(payload.get("vin") or ""), points=points)

    def merged_with(self, newer: Dataset) -> Dataset:
        """Return this dataset updated with a newer one, point by point.

        A point missing from ``newer`` keeps its previous value. A point that
        ``newer`` carries without a value does not wipe a previous value, and
        neither does one whose own timestamp is older than the previous one's.
        """
        points = dict(self.points)
        for key, point in newer.points.items():
            if _supersedes(point, points.get(key)):
                points[key] = point
        return Dataset(vin=newer.vin or self.vin, points=points)

    @cached_property
    def _by_name(self) -> dict[str, DataPoint]:
        """One point per field name; see :meth:`get`."""
        index: dict[str, DataPoint] = {}
        for point in sorted(self.points.values(), key=lambda point: point.key):
            index.setdefault(point.name, point)
        return index

    def get(self, name: str, key: str | None = None) -> DataPoint | None:
        """Return one data point for a field name, which may be duplicated.

        :param key: The data dictionary key to prefer. The dictionary lists
            several keys for many names, and they are not interchangeable:
            three of the four ``battery_state_report.soc`` keys hold the state
            of charge when charging *started*.

        Without a known key there is no way to tell which of several
        same-named points is meant, so the one with the lowest key is chosen:
        arbitrary, but stable, so an entity keeps tracking the same point from
        one dataset to the next instead of flipping between them.
        """
        if key is not None and (point := self.points.get(key)) is not None:
            return point
        return self._by_name.get(name)

    def value(self, name: str, key: str | None = None) -> Value:
        """Return the parsed value of a field, or ``None`` when absent."""
        point = self.get(name, key)
        return None if point is None else point.value

    @property
    def captured_at(self) -> datetime | None:
        """The newest moment the vehicle reported anything in this dataset."""
        stamps = [
            stamp
            for point in self.points.values()
            if point.name == "car_captured_time"
            and (stamp := parse_timestamp(point.raw)) is not None
        ]
        stamps.extend(
            point.timestamp for point in self.points.values() if point.timestamp
        )
        return max(stamps, default=None)


def _supersedes(point: DataPoint, previous: DataPoint | None) -> bool:
    """Whether ``point``, received later, should replace ``previous``.

    It should, unless it has no value while ``previous`` has one, or its own
    timestamp says it was measured before ``previous``.
    """
    if previous is None:
        return True
    if point.value is None and previous.value is not None:
        return False
    return (
        point.timestamp is None
        or previous.timestamp is None
        or point.timestamp >= previous.timestamp
    )


@dataclass(frozen=True, slots=True)
class DatasetFile:
    """One entry of the portal's delivery listing."""

    name: str
    created: datetime | None
    size: int | None = None

    @classmethod
    def from_json(cls, entry: dict[str, Any]) -> DatasetFile:
        """Build a listing entry, falling back to the timestamp in its name."""
        name = str(entry.get("name") or "")
        created = parse_timestamp(entry.get("createdOn")) or _filename_timestamp(name)
        # The portal sends the size as a string of digits.
        size = parse_value(str(entry.get("size") or ""))
        return cls(
            name=name,
            created=created,
            size=size if isinstance(size, int) and not isinstance(size, bool) else None,
        )

    @property
    def has_content(self) -> bool:
        """Whether downloading this file can yield any data."""
        return bool(self.name) and not self.name.endswith(NO_CONTENT_SUFFIX)


def _filename_timestamp(name: str) -> datetime | None:
    """Parse the ``YYYYMMDDhhmmss`` part of a delivery file name.

    Both ``<timestamp>_<vin>.zip`` and ``<vin>_<timestamp>.zip`` occur.
    """
    stem = name.rsplit(".", 1)[0]
    for part in reversed(stem.split("_")):
        if _FILENAME_TS_RE.match(part):
            try:
                return datetime.strptime(part, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def newest_with_content(files: list[DatasetFile]) -> DatasetFile | None:
    """Return the most recent file worth downloading."""
    minimum = datetime.min.replace(tzinfo=UTC)
    candidates = [file for file in files if file.has_content]
    return max(candidates, key=lambda file: file.created or minimum, default=None)
