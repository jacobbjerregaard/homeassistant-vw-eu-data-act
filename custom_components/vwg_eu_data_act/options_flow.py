"""Options flow for the VW Group EU Data Act integration.

The one option is the battery's nominal usable capacity, from the vehicle's
specification. The integration cannot know it (the VIN does not say which
battery a vehicle has), and it is only used to show the capacity estimated
from charging sessions as a percentage: battery health. Consumption and the
other figures keep using the estimate, which is measured on the vehicle.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.helpers import selector

from .const import CONF_NOMINAL_CAPACITY

MIN_CAPACITY = 10.0
MAX_CAPACITY = 200.0


class EudaOptionsFlow(OptionsFlow):
    """Edit the nominal battery capacity of an existing entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the nominal capacity; leaving it empty clears it."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(CONF_NOMINAL_CAPACITY)
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_NOMINAL_CAPACITY,
                    description={"suggested_value": current},
                ): vol.All(
                    selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_CAPACITY,
                            max=MAX_CAPACITY,
                            step=0.1,
                            unit_of_measurement="kWh",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Coerce(float),
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
