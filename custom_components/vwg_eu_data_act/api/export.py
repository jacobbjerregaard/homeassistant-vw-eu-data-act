"""History from the portal's one-off exports.

Besides the continuous feed, the portal can produce a one-off export of
everything the backend holds for a vehicle. It uses the same document shape
as a continuous dataset, but it is a history rather than a snapshot: months
of readings, tens of thousands per field, not in time order. Its field names
are also a vocabulary of their own (``currentSOCInPct`` rather than
``battery_state_report.soc``) and its keys are not in the data dictionary.

A real export runs to hundreds of megabytes, which takes over a gigabyte of
memory to load in one go, more than a Raspberry Pi running Home Assistant can
spare. :func:`read_export` therefore decodes the ``Data`` array one entry at a
time and keeps only the hourly aggregates of the series it knows.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from .dataset import parse_timestamp
from .exception import EudaError

_CHUNK = 1 << 20
_VIN_RE = re.compile(r'"vin"\s*:\s*"([A-Za-z0-9]{17})"')
_WHITESPACE = " \t\r\n"


def _kelvin_to_celsius(value: float) -> float:
    return round(value - 273.15, 2)


@dataclass(frozen=True, slots=True)
class ExportSeries:
    """An export field that has a curated sensor to import into."""

    #: The ``dataFieldName`` in the export.
    field: str
    #: The key of the sensor description the history belongs to.
    sensor: str
    #: The sensor's native unit, which the converted values are in.
    unit: str
    convert: Callable[[float], float] = float
    #: A running total, such as the odometer, rather than a measurement.
    total: bool = False


#: The history series seen in a real ID.7 export, and where they go. Only
#: numeric series whose sensor keeps long-term statistics are useful here.
EXPORT_SERIES: tuple[ExportSeries, ...] = (
    ExportSeries("currentSOCInPct", "battery_level", "%"),
    ExportSeries("cruisingRangeElectricInKm", "range", "km"),
    ExportSeries("chargePowerInKW", "charging_power", "kW"),
    ExportSeries(
        "temperatureOutsideVehicle", "outside_temperature", "°C", _kelvin_to_celsius
    ),
    ExportSeries("mileage", "odometer", "km", total=True),
)

_BY_FIELD = {series.field: series for series in EXPORT_SERIES}


@dataclass(slots=True)
class HourlyStat:
    """The readings of one series within one hour."""

    start: datetime
    mean: float
    min: float
    max: float
    #: The last reading of the hour: the state for a running total.
    last: float


@dataclass(slots=True)
class _Accumulator:
    count: int = 0
    total: float = 0.0
    low: float = float("inf")
    high: float = float("-inf")
    last_at: datetime | None = None
    last: float = 0.0

    def add(self, when: datetime, value: float) -> None:
        self.count += 1
        self.total += value
        self.low = min(self.low, value)
        self.high = max(self.high, value)
        if self.last_at is None or when >= self.last_at:
            self.last_at, self.last = when, value


@dataclass
class ExportHistory:
    """The hourly history of the known series in one export."""

    vin: str | None
    #: Hourly statistics by sensor key, oldest first.
    series: dict[str, list[HourlyStat]] = field(default_factory=dict)
    #: Every entry in the export, used or not.
    entries: int = 0
    #: Entries of a known series that were used.
    readings: int = 0

    def unit(self, sensor: str) -> str:
        """Return the unit a series' values are in."""
        return next(s.unit for s in EXPORT_SERIES if s.sensor == sensor)

    def is_total(self, sensor: str) -> bool:
        """Return whether a series is a running total."""
        return next(s.total for s in EXPORT_SERIES if s.sensor == sensor)


def read_export(stream: IO[str]) -> ExportHistory:
    """Aggregate the known series of an export into hourly statistics.

    Entries of other fields, without a usable timestamp or without a numeric
    value are skipped. This does blocking I/O.
    """
    buckets: dict[str, dict[datetime, _Accumulator]] = {}
    vin: str | None = None
    entries = readings = 0

    for header, item in _iter_entries(stream):
        if header is not None:
            vin = header
            continue
        entries += 1
        series = _BY_FIELD.get(item.get("dataFieldName"))  # type: ignore[arg-type]
        if series is None:
            continue
        when = parse_timestamp(item.get("timestampUtc"))
        try:
            value = series.convert(float(item.get("value")))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if when is None or value != value:  # NaN
            continue
        when = when.astimezone(UTC)
        hour = when.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(series.sensor, {}).setdefault(hour, _Accumulator()).add(
            when, value
        )
        readings += 1

    return ExportHistory(
        vin=vin,
        series={
            sensor: [
                HourlyStat(
                    start=hour,
                    mean=round(acc.total / acc.count, 3),
                    min=acc.low,
                    max=acc.high,
                    last=acc.last,
                )
                for hour, acc in sorted(hours.items())
            ]
            for sensor, hours in buckets.items()
        },
        entries=entries,
        readings=readings,
    )


def read_export_file(path: Path) -> ExportHistory:
    """Read an export as downloaded: a JSON document, or a ZIP holding one."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                members = [n for n in archive.namelist() if n.lower().endswith(".json")]
                if not members:
                    raise EudaError("The ZIP file contains no JSON document")
                with archive.open(members[0]) as raw:
                    return read_export(io.TextIOWrapper(raw, encoding="utf-8-sig"))
        with path.open(encoding="utf-8-sig") as stream:
            return read_export(stream)
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as err:
        raise EudaError(f"The export could not be read: {err}") from err


def _iter_entries(
    stream: IO[str],
) -> Iterator[tuple[str | None, dict[str, Any]]]:
    """Yield ``(vin, {})`` once if the header has one, then each ``Data`` entry.

    The document is ``{"vin": ..., "userId": ..., "Data": [{...}, ...]}``.
    Everything before the ``Data`` array is only searched for the VIN; the
    array is decoded entry by entry, reading more of the stream as needed.
    """
    decoder = json.JSONDecoder()
    buffer = ""
    while (start := buffer.find('"Data"')) == -1:
        chunk = stream.read(_CHUNK)
        if not chunk:
            raise EudaError("The file is not an export: it has no Data array")
        buffer += chunk
    if match := _VIN_RE.search(buffer, 0, start):
        yield match.group(1).upper(), {}

    position = start + len('"Data"')
    eof = False

    def fill() -> bool:
        """Read another chunk; return whether there was one."""
        nonlocal buffer, position, eof
        chunk = stream.read(_CHUNK)
        if not chunk:
            eof = True
            return False
        buffer = buffer[position:] + chunk
        position = 0
        return True

    # Skip to the opening bracket of the array.
    while True:
        while position < len(buffer) and buffer[position] in _WHITESPACE + ":":
            position += 1
        if position < len(buffer):
            break
        if not fill():
            raise EudaError("The export ended before its Data array")
    if buffer[position] != "[":
        raise EudaError("The export's Data is not an array")
    position += 1

    while True:
        while position < len(buffer) and buffer[position] in _WHITESPACE + ",":
            position += 1
        if position >= len(buffer):
            if not fill():
                raise EudaError("The export ended inside its Data array")
            continue
        if buffer[position] == "]":
            return
        try:
            item, end = decoder.raw_decode(buffer, position)
        except json.JSONDecodeError as err:
            # Most likely an entry cut in half by the chunk boundary.
            if not eof and fill():
                continue
            raise EudaError(f"The export is not valid JSON: {err.msg}") from err
        position = end
        if isinstance(item, dict):
            yield None, item
