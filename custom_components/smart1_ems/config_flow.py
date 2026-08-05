from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import Smart1Api
from .const import DOMAIN
from .energy_roles import (
    ENERGY_ROLES,
    energy_candidates,
    recommend_energy_roles,
)


class Smart1ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure a smart1 EMS installation."""

    VERSION = 1

    def __init__(self):
        self._api_key = None
        self._plants = []

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> Smart1OptionsFlow:
        """Create the smart1 EMS options flow."""
        return Smart1OptionsFlow()

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            self._api_key = user_input["api_key"]

            session = async_get_clientsession(self.hass)
            api = Smart1Api(session, self._api_key, "")

            try:
                self._plants = await api.get_plants()
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                if not self._plants:
                    errors["base"] = "no_plants"
                elif len(self._plants) == 1:
                    plant = self._plants[0]
                    return self.async_create_entry(
                        title=plant.get("DeviceName", "smart1 EMS"),
                        data={
                            "api_key": self._api_key,
                            "device_id": plant["DeviceId"],
                        },
                    )
                else:
                    return await self.async_step_plant()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("api_key"): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
            }),
            errors=errors,
        )

    async def async_step_plant(self, user_input=None):
        if user_input is not None:
            device_id = user_input["device_id"]
            plant = next(
                plant for plant in self._plants
                if plant["DeviceId"] == device_id
            )

            return self.async_create_entry(
                title=plant.get("DeviceName", f"smart1 EMS {device_id}"),
                data={
                    "api_key": self._api_key,
                    "device_id": device_id,
                },
            )

        options = {
            plant["DeviceId"]: plant.get("DeviceName", plant["DeviceId"])
            for plant in self._plants
        }

        return self.async_show_form(
            step_id="plant",
            data_schema=vol.Schema({
                vol.Required("device_id"): vol.In(options),
            }),
            errors={},
        )


class Smart1OptionsFlow(OptionsFlowWithReload):
    """Select the source point for each derived energy role."""

    async def async_step_init(self, user_input=None):
        """Manage smart1 EMS options."""
        runtime_data = self.hass.data[DOMAIN][self.config_entry.entry_id]
        points = runtime_data["devices"]
        current_roles = dict(self.config_entry.options.get("energy_roles", {}))
        recommendations = recommend_energy_roles(points)
        errors = {}

        if user_input is not None:
            selected_roles = {
                role.key: user_input[role.key]
                for role in ENERGY_ROLES
                if user_input.get(role.key)
            }
            selected_ids = list(selected_roles.values())
            if len(selected_ids) != len(set(selected_ids)):
                errors["base"] = "duplicate_energy_point"
            else:
                new_options = dict(self.config_entry.options)
                new_options["energy_roles"] = selected_roles
                return self.async_create_entry(data=new_options)

        schema = {}
        for role in ENERGY_ROLES:
            candidates = energy_candidates(points, role)
            options = [SelectOptionDict(value="", label="—")]
            options.extend(
                SelectOptionDict(
                    value=point.id,
                    label=f"{point.name} ({point.hardware})",
                )
                for point in candidates
            )
            selected = current_roles.get(
                role.key,
                recommendations.get(role.key, ""),
            )
            if selected and selected not in {
                point.id for point in candidates
            }:
                selected = recommendations.get(role.key, "")
            schema[
                vol.Required(role.key, default=selected)
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors=errors,
        )
