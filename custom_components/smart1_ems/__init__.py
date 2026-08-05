from datetime import timedelta
import logging

from aiohttp import ClientError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .api import Smart1Api, Smart1ApiError
from .const import DOMAIN
from .coordinator import Smart1Coordinator
from .derived_history import Smart1DerivedEnergyImporter
from .discovery import Smart1Discovery
from .energy_roles import ENERGY_ROLES_BY_KEY, energy_candidates
from .history import Smart1PvHistoryImporter

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]
HISTORY_UPDATE_INTERVAL = timedelta(hours=6)
CURRENT_DAY_UPDATE_INTERVAL = timedelta(minutes=15)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    api = Smart1Api(session, entry.data["api_key"], entry.data["device_id"])

    devices = await api.get_linear_devices()

    try:
        inverters = await api.get_inverters(missing_ok=True)
    except (ClientError, Smart1ApiError, TimeoutError) as err:
        # Inverter metadata and string diagnostics are optional. Installations
        # without these endpoints must retain all existing linear entities.
        _LOGGER.debug("Unable to discover smart1 inverters: %s", err)
        inverters = []

    try:
        module_fields = await api.get_module_fields(missing_ok=True)
    except (ClientError, Smart1ApiError, TimeoutError) as err:
        # Module-field configuration is optional and must not affect existing
        # live values or Energy Dashboard statistics.
        _LOGGER.debug("Unable to discover smart1 module fields: %s", err)
        module_fields = []

    discovery = Smart1Discovery()
    discovery_result = discovery.analyze(devices)
    discovery_result.inverter_count = len(inverters)
    discovery_result.module_field_count = len(module_fields)

    _LOGGER.info(
        "Smart1 Discovery: %s",
        discovery_result.to_dict(),
    )

    active_linear_ids = entry.options.get(
        "active_linear_ids",
        [device.id for device in devices],
    )

    coordinator = Smart1Coordinator(
        hass,
        api,
        devices,
        active_linear_ids,
        inverters,
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "devices": devices,
        "discovery": discovery_result,
        "inverters": inverters,
        "module_fields": module_fields,
    }

    history_importers = []
    if discovery_result.has_pv:
        pv_power_point = next(
            (
                point
                for point in devices
                if point.source == "counter"
                and point.type.lower() == "energy"
                and point.hardware.lower() == "pv_global"
            ),
            None,
        )
        history_importers.append(
            Smart1PvHistoryImporter(hass, api, pv_power_point)
        )

    points_by_id = {point.id: point for point in devices}
    selected_roles = entry.options.get("energy_roles", {})
    role_points = {}
    for role_key, point_id in selected_roles.items():
        role = ENERGY_ROLES_BY_KEY.get(role_key)
        point = points_by_id.get(point_id)
        if role is None or point is None:
            continue
        if point.id not in {
            candidate.id for candidate in energy_candidates(devices, role)
        }:
            _LOGGER.warning(
                "Ignoring ineligible smart1 derived energy role %s",
                role_key,
            )
            continue
        role_points[role_key] = point
    if role_points:
        history_importers.append(
            Smart1DerivedEnergyImporter(hass, api, role_points)
        )

    if history_importers:
        hass.data[DOMAIN][entry.entry_id]["history_importers"] = history_importers

        async def _async_refresh_history(_now=None) -> None:
            for history_importer in history_importers:
                await history_importer.async_import()

        async def _async_refresh_current_day(_now=None) -> None:
            for history_importer in history_importers:
                await history_importer.async_import(1, repair=False)

        entry.async_create_background_task(
            hass,
            _async_refresh_history(),
            "smart1 EMS history import",
        )
        entry.async_on_unload(
            async_track_time_interval(
                hass,
                _async_refresh_history,
                HISTORY_UPDATE_INTERVAL,
            )
        )
        entry.async_on_unload(
            async_track_time_interval(
                hass,
                _async_refresh_current_day,
                CURRENT_DAY_UPDATE_INTERVAL,
            )
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a smart1 EMS config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    hass.data[DOMAIN].pop(entry.entry_id)
    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)

    return True
