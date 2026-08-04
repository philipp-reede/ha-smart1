from datetime import timedelta
import logging

from aiohttp import ClientError

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import Smart1ApiError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class Smart1Coordinator(DataUpdateCoordinator):
    def __init__(self, hass, api, devices, active_linear_ids):
        self.api = api
        self.devices = devices
        self.linear_ids = active_linear_ids

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=5),
        )

    async def _async_update_data(self):
        try:
            live_values = await self.api.get_latest_linear_values(self.linear_ids)
        except (ClientError, Smart1ApiError, TimeoutError) as err:
            raise UpdateFailed(str(err)) from err

        try:
            pv_energy_today = await self.api.get_pv_cumulative_energy()
        except (ClientError, Smart1ApiError, TimeoutError) as err:
            # PV production is optional. A missing cumulative endpoint must not
            # make otherwise valid live measurements unavailable.
            _LOGGER.debug("Unable to update cumulative PV production: %s", err)
            pv_energy_today = None

        return {
            "live": live_values,
            "pv_energy_today": pv_energy_today,
        }
