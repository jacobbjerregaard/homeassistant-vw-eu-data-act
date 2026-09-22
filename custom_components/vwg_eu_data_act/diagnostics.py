"""Diagnostics for the VW Group EU Data Act integration.

The portal's field names are undocumented and vary by vehicle platform, so the
most useful thing a bug report can carry is the raw data. Everything that
identifies the owner or the vehicle is redacted.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_IDENTIFIER, CONF_NICKNAME, CONF_VIN
from .coordinator import EudaConfigEntry

TO_REDACT = {CONF_EMAIL, CONF_PASSWORD, CONF_VIN, CONF_IDENTIFIER, CONF_NICKNAME}

#: Name segments of fields whose values identify or locate the vehicle.
#: Matched against whole segments, so ``drivingenvironment`` is not a VIN.
_SENSITIVE_SEGMENTS = frozenset(
    {"vin", "latitude", "longitude", "lat", "lon", "gps", "coordinates"}
)
_SEGMENT_RE = re.compile(r"[^a-z0-9]+")


def _is_sensitive(name: str, raw: str, vin: str) -> bool:
    segments = set(_SEGMENT_RE.split(name.lower()))
    return bool(segments & _SENSITIVE_SEGMENTS) or (bool(vin) and vin in raw)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EudaConfigEntry
) -> dict[str, Any]:
    """Return the config entry and the vehicle's state, redacted."""
    data = entry.runtime_data.data
    vin = entry.data.get(CONF_VIN, "")
    dataset: dict[str, Any] | None = None
    if data is not None:
        dataset = {
            "file_created": data.file.created,
            "captured_at": data.dataset.captured_at,
            "fields": sorted(
                (
                    {
                        "key": point.key,
                        "name": point.name,
                        "value": REDACTED
                        if _is_sensitive(point.name, point.raw, vin)
                        else point.raw,
                        "timestamp": point.timestamp,
                    }
                    for point in data.dataset.points.values()
                ),
                key=lambda item: (item["name"], item["key"]),
            ),
        }
    return {"entry": async_redact_data(dict(entry.data), TO_REDACT), "dataset": dataset}
