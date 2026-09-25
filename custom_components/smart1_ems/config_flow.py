from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiohttp import ClientError
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

from .api import Smart1Api, Smart1ApiError, is_auth_error
from .const import DOMAIN
from .energy_roles import (
    ENERGY_ROLES,
    energy_candidates,
    recommend_energy_roles,
)
from .history_state import (
    STATISTICS_NAMESPACE_KEY,
    statistics_namespace_for_device,
)

ENERGY_ROLES_OPTION = "energy_roles"
ENERGY_ROLES_CONFIGURED_OPTION = "energy_roles_configured"


class Smart1ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure a smart1 EMS installation."""

    VERSION = 1
    MINOR_VERSION = 3

    def __init__(self):
        self._api_key = None
        self._plants = []

    @staticmethod
    def _device_id(plant: dict[str, Any]) -> str:
        """Return a normalized installation identifier."""
        value = plant.get("DeviceId")
        return value.strip() if isinstance(value, str) else ""

    @classmethod
    def _plant_title(cls, plant: dict[str, Any]) -> str:
        """Return a user-facing installation name."""
        name = str(plant.get("DeviceName") or "").strip()
        return name or f"smart1 EMS {cls._device_id(plant)}"

    async def _async_get_plants(
        self,
        api_key: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Fetch installations and map failures to config-flow errors."""
        session = async_get_clientsession(self.hass)
        api = Smart1Api(session, api_key, "")

        try:
            plants = await api.get_plants()
        except Smart1ApiError as err:
            return [], "invalid_auth" if is_auth_error(err) else "cannot_connect"
        except (ClientError, TimeoutError):
            return [], "cannot_connect"

        valid_plants = [plant for plant in plants if self._device_id(plant)]
        if not valid_plants:
            return [], "no_plants"
        return valid_plants, None

    async def _async_create_plant_entry(
        self,
        plant: dict[str, Any],
    ):
        """Create one entry while protecting new and legacy installs."""
        device_id = self._device_id(plant)

        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured()

        # Entries created before config-entry unique IDs were introduced still
        # need to block a second entry for the same installation. Compare the
        # normalized value instead of relying on an exact data-dict match.
        if any(
            isinstance(entry.data.get("device_id"), str)
            and entry.data["device_id"].strip() == device_id
            for entry in self._async_current_entries()
        ):
            return self.async_abort(reason="already_configured")

        return self.async_create_entry(
            title=self._plant_title(plant),
            data={
                "api_key": self._api_key,
                "device_id": device_id,
                STATISTICS_NAMESPACE_KEY: (
                    statistics_namespace_for_device(device_id)
                ),
            },
        )

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
            self._plants, error = await self._async_get_plants(self._api_key)
            if error is not None:
                errors["base"] = error
            elif len(self._plants) == 1:
                return await self._async_create_plant_entry(self._plants[0])
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
                if self._device_id(plant) == device_id
            )
            return await self._async_create_plant_entry(plant)

        options = {
            self._device_id(plant): self._plant_title(plant)
            for plant in self._plants
        }

        return self.async_show_form(
            step_id="plant",
            data_schema=vol.Schema({
                vol.Required("device_id"): vol.In(options),
            }),
            errors={},
        )

    async def async_step_reauth(self, entry_data):
        """Request a replacement API key for an existing installation."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Validate and save a replacement API key."""
        errors = {}

        if user_input is not None:
            api_key = user_input["api_key"]
            plants, error = await self._async_get_plants(api_key)
            if error is not None:
                errors["base"] = error
            else:
                entry = self._get_reauth_entry()
                stored_device_id = entry.data.get("device_id")
                expected_device_id = (
                    stored_device_id.strip()
                    if isinstance(stored_device_id, str)
                    else ""
                )
                if not any(
                    self._device_id(plant) == expected_device_id
                    for plant in plants
                ):
                    errors["base"] = "wrong_account"
                else:
                    await self.async_set_unique_id(expected_device_id)
                    self._abort_if_unique_id_mismatch(
                        reason="wrong_account"
                    )
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={"api_key": api_key},
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
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


class Smart1OptionsFlow(OptionsFlowWithReload):
    """Select the source point for each derived energy role."""

    def _configured_energy_role_keys(self) -> set[str]:
        """Return roles whose selection was explicitly saved before."""
        role_keys = {role.key for role in ENERGY_ROLES}
        raw_configured = self.config_entry.options.get(
            ENERGY_ROLES_CONFIGURED_OPTION
        )
        if isinstance(raw_configured, (list, tuple)):
            return {
                role_key
                for role_key in raw_configured
                if isinstance(role_key, str) and role_key in role_keys
            }

        # Older options flows only persisted non-empty selections. The
        # presence of the mapping nevertheless proves that the user saved the
        # complete form, so every omitted role was explicitly left disabled.
        if ENERGY_ROLES_OPTION in self.config_entry.options:
            return role_keys
        return set()

    async def async_step_init(self, user_input=None):
        """Manage smart1 EMS options."""
        runtime_data = self.hass.data.get(DOMAIN, {}).get(
            self.config_entry.entry_id
        )
        if runtime_data is None:
            return self.async_abort(reason="not_loaded")
        points = runtime_data["devices"]
        raw_current_roles = self.config_entry.options.get(
            ENERGY_ROLES_OPTION,
            {},
        )
        current_roles = (
            dict(raw_current_roles)
            if isinstance(raw_current_roles, Mapping)
            else {}
        )
        configured_roles = self._configured_energy_role_keys()
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
                new_options[ENERGY_ROLES_OPTION] = selected_roles
                new_options[ENERGY_ROLES_CONFIGURED_OPTION] = [
                    role.key for role in ENERGY_ROLES
                ]
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
            selected = current_roles.get(role.key, "")
            if not selected and role.key not in configured_roles:
                selected = recommendations.get(role.key, "")
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
