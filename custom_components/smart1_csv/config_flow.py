from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Smart1Api
from .const import DOMAIN


class Smart1ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._api_key = None
        self._plants = []

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
                        title=plant.get("DeviceName", "Smart1 CSV"),
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
                vol.Required("api_key"): str,
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
                title=plant.get("DeviceName", f"Smart1 {device_id}"),
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