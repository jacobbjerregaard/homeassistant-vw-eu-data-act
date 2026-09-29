"""The VW Group EU Data Act integration."""

from __future__ import annotations

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .api.brands import get_brand
from .api.client import EudaClient
from .api.dictionary import DataDictionary, load_data_dictionary
from .const import CONF_BRAND, CONF_NOMINAL_CAPACITY, DATA_DICTIONARY, DOMAIN, PLATFORMS
from .coordinator import EudaConfigEntry, EudaCoordinator
from .services import async_setup_services
from .vehicle_metrics import MetricsRuntime

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions.

    They are registered here rather than per entry so they exist even while
    an entry cannot be set up, such as before the portal delivers any data.
    """
    async_setup_services(hass)
    return True


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
    metrics = MetricsRuntime(hass, entry.entry_id)
    await metrics.async_load()
    metrics.nominal_capacity = entry.options.get(CONF_NOMINAL_CAPACITY)
    coordinator.metrics = metrics

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    @callback
    def _async_feed_metrics() -> None:
        metrics.add_datasets(coordinator.received)
        coordinator.received = []

    # Feed what the first refresh brought, then every later update. This
    # listener is added before the platforms', so the figures are updated
    # before the sensors read them.
    _async_feed_metrics()
    entry.async_on_unload(coordinator.async_add_listener(_async_feed_metrics))
    entry.async_on_unload(metrics.async_flush)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: EudaConfigEntry) -> None:
    """Apply a changed nominal capacity.

    No reload: this also runs when the coordinator stores a replaced data
    request's identifier, and the option only changes one sensor's value.
    """
    coordinator = entry.runtime_data
    if coordinator.metrics is not None:
        coordinator.metrics.nominal_capacity = entry.options.get(CONF_NOMINAL_CAPACITY)
        coordinator.async_update_listeners()


async def _async_get_dictionary(hass: HomeAssistant) -> DataDictionary:
    """Return VW's data dictionary, reading it from disk the first time."""
    if (dictionary := hass.data.get(DATA_DICTIONARY)) is None:
        dictionary = await hass.async_add_executor_job(load_data_dictionary)
        hass.data[DATA_DICTIONARY] = dictionary
    return dictionary


async def async_unload_entry(hass: HomeAssistant, entry: EudaConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
