"""The VW Group EU Data Act integration."""

from __future__ import annotations

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api.brands import get_brand
from .api.client import EudaClient
from .api.dictionary import DataDictionary, load_data_dictionary
from .const import CONF_BRAND, DATA_DICTIONARY, PLATFORMS
from .coordinator import EudaConfigEntry, EudaCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: EudaConfigEntry) -> bool:
    """Set up one vehicle from a config entry."""
    client = EudaClient(
        # A session of its own: the portal is cookie-authenticated, and a
        # shared jar would mix up accounts configured side by side.
        async_create_clientsession(hass),
        get_brand(entry.data[CONF_BRAND]),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
    )
    coordinator = EudaCoordinator(
        hass, entry, client, await _async_get_dictionary(hass)
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_get_dictionary(hass: HomeAssistant) -> DataDictionary:
    """Return VW's data dictionary, reading it from disk the first time."""
    if (dictionary := hass.data.get(DATA_DICTIONARY)) is None:
        dictionary = await hass.async_add_executor_job(load_data_dictionary)
        hass.data[DATA_DICTIONARY] = dictionary
    return dictionary


async def async_unload_entry(hass: HomeAssistant, entry: EudaConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
