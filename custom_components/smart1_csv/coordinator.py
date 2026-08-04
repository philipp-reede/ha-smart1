from datetime import timedelta
import logging

from aiohttp import ClientError

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

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
            energy_today = await self.api.get_cumulative_values("day", self.linear_ids)

            return {
                "live": live_values,
                "energy_today": energy_today,
            }

        except ClientError as err:
            raise UpdateFailed(str(err)) from err