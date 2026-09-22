"""Constants for the VW Group EU Data Act integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform
from homeassistant.util.hass_dict import HassKey

from .api.dictionary import DataDictionary

DOMAIN = "vwg_eu_data_act"

PLATFORMS: list[Platform] = [Platform.SENSOR]

#: VW's data dictionary, loaded once and shared by every config entry.
DATA_DICTIONARY: HassKey[DataDictionary] = HassKey(f"{DOMAIN}_dictionary")

CONF_BRAND = "brand"
CONF_VIN = "vin"
CONF_IDENTIFIER = "identifier"
CONF_NICKNAME = "nickname"

#: How often the delivery listing is checked. The portal produces a dataset
#: roughly every 15 minutes; checking more often than that only shortens the
#: delay before a new one is noticed, and a listing request is cheap. The ZIP
#: itself is only downloaded when a new file has appeared.
UPDATE_INTERVAL = timedelta(minutes=5)

#: Consecutive transient failures tolerated before entities go unavailable.
#: The portal answers with a 5xx often enough that failing on the first one
#: would make every sensor flicker.
MAX_TRANSIENT_FAILURES = 3

#: How many of the newest datasets are merged when there is no state yet, at
#: start-up. A parked vehicle delivers reduced datasets, so the newest one
#: alone would leave most sensors unknown until the vehicle wakes up.
INITIAL_DATASETS = 8

#: Download attempts per delivery file before it is skipped. A file the portal
#: cannot serve would otherwise be retried on every poll, forever.
MAX_FILE_ATTEMPTS = 3
