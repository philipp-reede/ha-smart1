from datetime import timedelta
import logging

from aiohttp import ClientError

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import Smart1ApiError
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
    ):
        self.api = api
        self.devices = devices
        self.linear_ids = active_linear_ids
        self.inverters = list(inverters or [])

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=5),
        )

    async def _async_update_data(self):
        today = dt_util.now().date()

        try:
            live_values = await self.api.get_latest_linear_values(
                self.linear_ids,
                target_date=today,
            )
        except (ClientError, Smart1ApiError, TimeoutError) as err:
            raise UpdateFailed(str(err)) from err

        try:
            pv_energy_today = await self.api.get_pv_cumulative_energy(
                target_date=today,
            )
        except (ClientError, Smart1ApiError, TimeoutError) as err:
            # PV production is optional. A missing cumulative endpoint must not
            # make otherwise valid live measurements unavailable.
            _LOGGER.debug("Unable to update cumulative PV production: %s", err)
            pv_energy_today = None

        pv_strings = {}
        if self.inverters:
            try:
                pv_strings = await self.api.get_latest_pv_string_samples(
                    target_date=today,
                    missing_ok=True,
                )
            except (ClientError, Smart1ApiError, TimeoutError) as err:
                # Detailed inverter diagnostics are optional. Keep the latest
                # successful values instead of failing all linear entities.
                _LOGGER.debug("Unable to update inverter diagnostics: %s", err)
                previous_data = getattr(self, "data", None) or {}
                pv_strings = previous_data.get("pv_strings", {})

        return {
            "live": live_values,
            "pv_energy_today": pv_energy_today,
            "pv_strings": pv_strings,
        }
