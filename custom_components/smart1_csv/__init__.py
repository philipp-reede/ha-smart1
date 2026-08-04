import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Smart1Api
from .const import DOMAIN
from .coordinator import Smart1Coordinator
from .discovery import Smart1Discovery

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    api = Smart1Api(session, entry.data["api_key"], entry.data["device_id"])

    devices = await api.get_linear_devices()

    discovery = Smart1Discovery()
    discovery_result = discovery.analyze(devices)

    _LOGGER.info(
        "Smart1 Discovery: %s",
        discovery_result.to_dict(),
    )

    active_linear_ids = entry.options.get(
        "active_linear_ids",
        [device.id for device in devices],
    )

    coordinator = Smart1Coordinator(hass, api, devices, active_linear_ids)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "devices": devices,
        "discovery": discovery_result,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True