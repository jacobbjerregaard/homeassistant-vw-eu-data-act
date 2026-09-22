"""Shared entity base for the VW Group EU Data Act integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.brands import get_brand
from .const import CONF_BRAND, CONF_NICKNAME, DOMAIN
from .coordinator import EudaCoordinator


class EudaEntity(CoordinatorEntity[EudaCoordinator]):
    """An entity belonging to one vehicle."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EudaCoordinator, key: str) -> None:
        """Attach the entity to the vehicle's device."""
        super().__init__(coordinator)
        data = coordinator.config_entry.data
        vin = coordinator.vin
        self._attr_unique_id = f"{vin}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)},
            manufacturer=get_brand(data[CONF_BRAND]).name,
            name=data.get(CONF_NICKNAME) or vin,
            serial_number=vin,
        )
