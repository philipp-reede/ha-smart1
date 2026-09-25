from datetime import timedelta
import logging

from aiohttp import ClientError

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import Smart1ApiError, describe_api_error, is_auth_error
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class Smart1Coordinator(DataUpdateCoordinator):
    def __init__(
        self,
        hass,
        api,
        devices,
        active_linear_ids,
        inverters=None,
        config_entry=None,
    ):
        self.api = api
        self.devices = devices
        self.linear_ids = active_linear_ids
        self.inverters = list(inverters or [])

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=5),
        )

    async def _async_update_data(self):
        today = dt_util.now().date()

        try:
            if self.linear_ids:
                live_values = await self.api.get_latest_linear_values(
                    self.linear_ids,
                    target_date=today,
                )
            else:
                # An empty point list would otherwise skip the only required
                # request and allow authentication failures from optional PV
                # endpoints to go unnoticed indefinitely.
                plants = await self.api.get_plants()
                expected_device_id = str(self.api.device_id).strip()
                if not expected_device_id or not any(
                    isinstance(plant.get("DeviceId"), str)
                    and plant["DeviceId"].strip() == expected_device_id
                    for plant in plants
                ):
                    # A still-valid key can remain usable for another account
                    # or installation after access to this entry was revoked.
                    # Treat that exactly like rejected credentials so Home
                    # Assistant starts the existing reauthentication flow.
                    raise ConfigEntryAuthFailed(
                        "smart1 installation is no longer accessible"
                    ) from None
                live_values = {}
        except (ClientError, Smart1ApiError, TimeoutError) as err:
            if is_auth_error(err):
                raise ConfigEntryAuthFailed(describe_api_error(err)) from None
            # Client exceptions can contain the request URL and API key. Do
            # not retain them as a visible chained cause in Home Assistant.
            raise UpdateFailed(describe_api_error(err)) from None

        pv_cumulative_authoritative = False
        try:
            pv_energy_today = await self.api.get_pv_cumulative_energy(
                target_date=today,
                missing_ok=True,
            )
            pv_cumulative_authoritative = True
        except (ClientError, Smart1ApiError, TimeoutError) as err:
            # PV production is optional. A missing cumulative endpoint must not
            # make otherwise valid live measurements unavailable.
            _LOGGER.debug(
                "Unable to update cumulative PV production: %s",
                describe_api_error(err),
            )
            pv_energy_today = None

        pv_strings = {}
        pv_strings_authoritative = False
        if self.inverters:
            try:
                pv_strings = await self.api.get_latest_pv_string_samples(
                    target_date=today,
                    missing_ok=True,
                )
                # A successful but empty current-day response, for example
                # before sunrise, is not proof that registered strings are
                # obsolete. Non-empty rows can still refine initial topology.
                pv_strings_authoritative = bool(pv_strings)
            except (ClientError, Smart1ApiError, TimeoutError) as err:
                # Detailed inverter diagnostics are optional. Keep the latest
                # successful values instead of failing all linear entities.
                _LOGGER.debug(
                    "Unable to update inverter diagnostics: %s",
                    describe_api_error(err),
                )
                previous_data = getattr(self, "data", None) or {}
                pv_strings = previous_data.get("pv_strings", {})
                pv_strings_authoritative = previous_data.get(
                    "pv_strings_authoritative",
                    False,
                )

        return {
            "live": live_values,
            "pv_energy_today": pv_energy_today,
            "pv_cumulative_authoritative": pv_cumulative_authoritative,
            "pv_strings": pv_strings,
            "pv_strings_authoritative": pv_strings_authoritative,
        }
