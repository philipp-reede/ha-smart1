from collections.abc import Mapping
from datetime import timedelta
import logging

from aiohttp import ClientError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .api import (
    Smart1Api,
    Smart1ApiError,
    describe_api_error,
    is_auth_error,
    sanitize_api_error_code,
)
from .const import DOMAIN
from .coordinator import Smart1Coordinator
from .derived_history import Smart1DerivedEnergyImporter
from .discovery import Smart1Discovery
from .energy_roles import (
    ENERGY_ROLES_BY_KEY,
    energy_candidates,
    statistic_id_for_role,
)
from .history import PV_STATISTIC_ID, Smart1PvHistoryImporter
from .history_state import (
    HISTORY_SCHEMA_VERSIONS_KEY,
    LEGACY_HISTORY_REBUILD_KEY,
    STATISTICS_NAMESPACE_KEY,
    Smart1HistoryState,
    statistics_namespace_for_device,
)
from .recorder_helpers import async_clear_statistics

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]
HISTORY_UPDATE_INTERVAL = timedelta(hours=6)
CURRENT_DAY_UPDATE_INTERVAL = timedelta(minutes=15)
TOPOLOGY_RETRY_INTERVAL = timedelta(minutes=15)
TOPOLOGY_RECOVERY_CACHE_KEY = f"{DOMAIN}_topology_recovery"
LEGACY_ORPHAN_CLEANUP_KEY = "legacy_orphan_cleanup_ids"
PV_CAPABILITY_CONFIRMED_KEY = "pv_capability_confirmed"

_INVERTER_ID_COLUMNS = {"Inverter Id", "InverterId", '"Inverter Id"'}
_MODULE_FIELD_ID_COLUMNS = {
    "ModulfieldId",
    "Modulfield Id",
    '"ModulfieldId"',
}
_BUS_ID_COLUMNS = {"BusId", "Bus Id", '"BusId"'}


def _discovery_probe_is_authoritative(
    probe: dict[str, object],
    id_columns: set[str],
) -> bool:
    """Return whether optional topology was fetched with a known schema."""
    columns = probe.get("response_columns", [])
    return (
        probe.get("endpoint_result") in {"data_returned", "empty_response"}
        and isinstance(columns, list)
        and bool(id_columns.intersection(columns))
    )


def _topology_response_is_conclusive(
    values: object,
    probe: Mapping[str, object],
    id_columns: set[str],
) -> bool:
    """Return whether an optional topology response can end recovery."""
    return bool(values) or probe.get("endpoint_result") == "not_found" or (
        _discovery_probe_is_authoritative(dict(probe), id_columns)
    )


def _topology_probe_needs_retry(
    values: object,
    probe: Mapping[str, object],
    id_columns: set[str],
) -> bool:
    """Return whether optional topology remains transient or ambiguous."""
    return not _topology_response_is_conclusive(values, probe, id_columns)


async def _async_retry_failed_topology(
    hass: HomeAssistant,
    entry: ConfigEntry,
    api: Smart1Api,
    coordinator: Smart1Coordinator,
    runtime_data: dict[str, object],
    pending_endpoints: set[str],
) -> bool:
    """Retry failed optional topology requests and report reload necessity.

    A normal response, including an optional 404, resolves that endpoint. The
    caller reloads the entry only when the sensor platform can add recovered
    devices or safely clean up topology that is now authoritatively absent.
    """
    reload_needed = False
    endpoint_specs = {
        "inverters": (
            api.get_inverters_with_probe,
            _INVERTER_ID_COLUMNS,
            "inverter_discovery_authoritative",
        ),
        "module_fields": (
            api.get_module_fields_with_probe,
            _MODULE_FIELD_ID_COLUMNS,
            "module_field_discovery_authoritative",
        ),
        "buses": (
            api.get_buses_with_probe,
            _BUS_ID_COLUMNS,
            "bus_discovery_authoritative",
        ),
    }

    for endpoint in tuple(pending_endpoints):
        fetch, id_columns, authoritative_key = endpoint_specs[endpoint]
        try:
            values, probe = await fetch(missing_ok=True)
        except (ClientError, Smart1ApiError, TimeoutError) as err:
            _LOGGER.debug(
                "Unable to retry smart1 %s topology: %s",
                endpoint,
                describe_api_error(err),
            )
            continue

        if not _topology_response_is_conclusive(
            values,
            probe,
            id_columns,
        ):
            # A blank HTTP-200 response without the documented ID column is
            # not proof of an empty topology. Keep retrying until the portal
            # returns a known schema, actual parsed objects, or an explicit
            # optional 404.
            continue

        pending_endpoints.remove(endpoint)
        runtime_data[endpoint] = values
        authoritative = _discovery_probe_is_authoritative(
            probe,
            id_columns,
        )
        runtime_data[authoritative_key] = authoritative
        endpoint_requires_reload = authoritative or bool(values)
        reload_needed = reload_needed or endpoint_requires_reload
        if endpoint_requires_reload:
            # Carry the successful result across the recovery reload. Without
            # this handoff, an alternating success/failure portal could lose
            # the recovered data and start a new reload cycle every interval.
            recovery_cache = hass.data.setdefault(
                TOPOLOGY_RECOVERY_CACHE_KEY,
                {},
            ).setdefault(entry.entry_id, {})
            recovery_cache[endpoint] = (list(values), dict(probe))
        if endpoint == "inverters":
            # Keep optional detailed polling useful until the reload runs.
            coordinator.inverters = list(values)
        elif endpoint == "buses":
            runtime_data["bus_probe"] = probe

    return reload_needed


def _is_unscoped_legacy_statistic_id(statistic_id: object) -> bool:
    """Return whether an ID belongs to the shared pre-migration namespace."""
    if statistic_id == PV_STATISTIC_ID:
        return True
    if not isinstance(statistic_id, str):
        return False

    for role_key in ENERGY_ROLES_BY_KEY:
        prefix = f"{DOMAIN}:{role_key}_"
        source_hash = statistic_id.removeprefix(prefix)
        if (
            statistic_id.startswith(prefix)
            and len(source_hash) == 8
            and all(
                character in "0123456789abcdef"
                for character in source_hash
            )
        ):
            return True
    return False


def _legacy_statistic_ids(entries: list[ConfigEntry]) -> set[str]:
    """Collect every discoverable statistic ID from the shared namespace."""
    statistic_ids = {PV_STATISTIC_ID}
    for candidate in entries:
        selected_roles = candidate.options.get("energy_roles", {})
        if isinstance(selected_roles, Mapping):
            statistic_ids.update(
                statistic_id_for_role(role_key, point_id)
                for role_key, point_id in selected_roles.items()
                if role_key in ENERGY_ROLES_BY_KEY
                and isinstance(point_id, str)
                and point_id
            )

        # Retain coverage for a role that was deselected after it had already
        # written legacy statistics. Scoped IDs are deliberately ignored.
        raw_versions = candidate.data.get(HISTORY_SCHEMA_VERSIONS_KEY, {})
        if isinstance(raw_versions, Mapping):
            statistic_ids.update(
                statistic_id
                for statistic_id in raw_versions
                if _is_unscoped_legacy_statistic_id(statistic_id)
            )
    return statistic_ids


def _entry_proves_legacy_shared_pv(entry: ConfigEntry) -> bool:
    """Return whether a scoped entry demonstrably wrote legacy PV history."""
    raw_versions = entry.data.get(HISTORY_SCHEMA_VERSIONS_KEY, {})
    return bool(
        entry.data.get(STATISTICS_NAMESPACE_KEY)
        and isinstance(raw_versions, Mapping)
        and PV_STATISTIC_ID in raw_versions
    )


def _stored_orphan_cleanup_ids(data: Mapping[str, object]) -> set[str]:
    """Return previously cleared legacy statistic IDs."""
    raw_ids = data.get(LEGACY_ORPHAN_CLEANUP_KEY, [])
    if not isinstance(raw_ids, (list, tuple)):
        return set()
    return {
        statistic_id
        for statistic_id in raw_ids
        if isinstance(statistic_id, str)
    }


async def _async_recorder_legacy_statistic_ids(
    hass: HomeAssistant,
) -> set[str]:
    """Discover pre-marker statistics that config-entry state cannot name."""
    from homeassistant.components.recorder.statistics import (
        async_list_statistic_ids,
    )

    metadata = await async_list_statistic_ids(hass, None)
    statistic_ids: set[str] = set()
    for item in metadata:
        statistic_id = item.get("statistic_id")
        if (
            item.get("source") == DOMAIN
            and isinstance(statistic_id, str)
            and _is_unscoped_legacy_statistic_id(statistic_id)
        ):
            statistic_ids.add(statistic_id)
    return statistic_ids


async def _async_clear_orphaned_legacy_statistics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    history_state: Smart1HistoryState,
    statistic_ids: set[str],
) -> None:
    """Delete shared statistics that the selected legacy owner cannot rebuild."""
    if not statistic_ids:
        return

    # Import lazily so config-entry migration remains recorder-independent.
    from homeassistant.components.recorder import get_instance

    cleanup_ids = set(statistic_ids)
    recorder = get_instance(hass)

    def _invalidate_queued_statistics() -> None:
        # Once Recorder accepted the clear it can no longer be cancelled.
        # Invalidate completion state before the helper's first await so an
        # options reload cannot select the role with a stale short-refresh
        # marker while the clear is still waiting in Recorder's queue.
        history_state.forget_statistics(cleanup_ids)

    def _record_completed_cleanup() -> None:
        # Read entry.data only when the callback runs. A delayed callback may
        # outlive an options reload, so merging current data avoids restoring
        # a stale snapshot. It deliberately does not touch history markers:
        # a replacement import queued after this clear may already have
        # completed by then.
        updated_data = dict(entry.data)
        cleaned_ids = _stored_orphan_cleanup_ids(updated_data)
        cleaned_ids.update(cleanup_ids)
        updated_data[LEGACY_ORPHAN_CLEANUP_KEY] = sorted(cleaned_ids)
        hass.config_entries.async_update_entry(entry, data=updated_data)

        _LOGGER.warning(
            "Removed %d unscoped smart1 history statistics that cannot be "
            "attributed to the selected legacy installation",
            len(cleanup_ids),
        )

    if not await async_clear_statistics(
        recorder,
        cleanup_ids,
        enqueue_followup=_invalidate_queued_statistics,
        on_done=_record_completed_cleanup,
    ):
        _LOGGER.warning(
            "Timed out while removing unscoped smart1 history statistics"
        )
        return


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    api = Smart1Api(session, entry.data["api_key"], entry.data["device_id"])
    statistics_namespace = entry.data.get(STATISTICS_NAMESPACE_KEY, "")
    force_legacy_history_rebuild = (
        entry.data.get(LEGACY_HISTORY_REBUILD_KEY) is True
    )
    history_state = Smart1HistoryState(hass, entry)

    try:
        devices = await api.get_linear_devices()
    except (ClientError, Smart1ApiError, TimeoutError) as err:
        if is_auth_error(err):
            raise ConfigEntryAuthFailed(describe_api_error(err)) from None
        # Initial discovery is required. Ask Home Assistant to retry without
        # retaining a client exception that may contain the API-key URL.
        raise ConfigEntryNotReady(describe_api_error(err)) from None

    topology_cache_by_entry = hass.data.get(TOPOLOGY_RECOVERY_CACHE_KEY, {})
    topology_recovery_cache = topology_cache_by_entry.get(entry.entry_id, {})
    cached_topology_endpoints: set[str] = set()

    if "inverters" in topology_recovery_cache:
        inverters, inverter_probe = topology_recovery_cache["inverters"]
        cached_topology_endpoints.add("inverters")
    else:
        try:
            inverters, inverter_probe = await api.get_inverters_with_probe(
                missing_ok=True
            )
        except (ClientError, Smart1ApiError, TimeoutError) as err:
            # Inverter metadata and string diagnostics are optional.
            _LOGGER.debug(
                "Unable to discover smart1 inverters: %s",
                describe_api_error(err),
            )
            inverters = []
            inverter_probe = {
                "endpoint_result": "request_failed",
                "response_columns": [],
            }

    if "module_fields" in topology_recovery_cache:
        module_fields, module_field_probe = topology_recovery_cache[
            "module_fields"
        ]
        cached_topology_endpoints.add("module_fields")
    else:
        try:
            module_fields, module_field_probe = (
                await api.get_module_fields_with_probe(missing_ok=True)
            )
        except (ClientError, Smart1ApiError, TimeoutError) as err:
            # Module fields are optional and cannot block live values.
            _LOGGER.debug(
                "Unable to discover smart1 module fields: %s",
                describe_api_error(err),
            )
            module_fields = []
            module_field_probe = {
                "endpoint_result": "request_failed",
                "response_columns": [],
            }

    if "buses" in topology_recovery_cache:
        buses, bus_probe = topology_recovery_cache["buses"]
        cached_topology_endpoints.add("buses")
    else:
        try:
            buses, bus_probe = await api.get_buses_with_probe(missing_ok=True)
        except Smart1ApiError as err:
            # Inverter-bus metadata is optional and cannot block live values.
            _LOGGER.debug(
                "Unable to discover smart1 inverter buses: %s",
                describe_api_error(err),
            )
            buses = []
            bus_probe = {
                "endpoint_result": "api_error",
                "error_code": sanitize_api_error_code(err.code),
            }
        except (ClientError, TimeoutError) as err:
            # Client exceptions can contain the API-key-bearing request URL.
            _LOGGER.debug(
                "Unable to discover smart1 inverter buses (%s)",
                describe_api_error(err),
            )
            buses = []
            bus_probe = {
                "endpoint_result": "request_failed",
                "error_type": describe_api_error(err),
            }

    discovery = Smart1Discovery()
    discovery_result = discovery.analyze(devices)
    discovery_result.bus_count = len(buses)
    inverter_discovery_authoritative = _discovery_probe_is_authoritative(
        inverter_probe,
        _INVERTER_ID_COLUMNS,
    )
    module_field_discovery_authoritative = (
        _discovery_probe_is_authoritative(
            module_field_probe,
            _MODULE_FIELD_ID_COLUMNS,
        )
    )
    bus_discovery_authoritative = _discovery_probe_is_authoritative(
        bus_probe,
        _BUS_ID_COLUMNS,
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
        config_entry=entry,
    )
    await coordinator.async_config_entry_first_refresh()

    # Linear points are not the only reliable proof of photovoltaic support.
    # Some portals expose only inverter/module topology or the dedicated
    # cumulative endpoint.  Retain the point-based discovery result, then add
    # this optional endpoint evidence before sensor, history and legacy-cleanup
    # gates inspect ``has_pv``.
    discovery_result.add_pv_evidence(
        inverter_count=len(inverters),
        module_field_count=len(module_fields),
        cumulative_energy=coordinator.data.get("pv_energy_today"),
    )
    if entry.data.get(PV_CAPABILITY_CONFIRMED_KEY) is True:
        discovery_result.has_pv = True
    elif discovery_result.has_pv:
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                PV_CAPABILITY_CONFIRMED_KEY: True,
            },
        )

    _LOGGER.info(
        "Smart1 Discovery: %s",
        discovery_result.to_dict(),
    )

    runtime_data = {
        "api": api,
        "coordinator": coordinator,
        "history_state": history_state,
        "devices": devices,
        "discovery": discovery_result,
        "inverters": inverters,
        "module_fields": module_fields,
        "buses": buses,
        "bus_probe": bus_probe,
        "inverter_discovery_authoritative": (
            inverter_discovery_authoritative
        ),
        "module_field_discovery_authoritative": (
            module_field_discovery_authoritative
        ),
        "bus_discovery_authoritative": bus_discovery_authoritative,
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime_data

    pending_topology_endpoints = {
        endpoint
        for endpoint, values, probe, id_columns in (
            (
                "inverters",
                inverters,
                inverter_probe,
                _INVERTER_ID_COLUMNS,
            ),
            (
                "module_fields",
                module_fields,
                module_field_probe,
                _MODULE_FIELD_ID_COLUMNS,
            ),
            ("buses", buses, bus_probe, _BUS_ID_COLUMNS),
        )
        if endpoint not in cached_topology_endpoints
        and _topology_probe_needs_retry(values, probe, id_columns)
    }
    if pending_topology_endpoints:
        topology_reload_requested = False
        topology_retry_in_progress = False

        async def _async_retry_topology(_now=None) -> None:
            """Recover optional static topology without a reload loop."""
            nonlocal topology_reload_requested, topology_retry_in_progress
            if (
                topology_reload_requested
                or topology_retry_in_progress
                or not pending_topology_endpoints
                or hass.data.get(DOMAIN, {}).get(entry.entry_id)
                is not runtime_data
            ):
                return
            topology_retry_in_progress = True
            try:
                reload_needed = await _async_retry_failed_topology(
                    hass,
                    entry,
                    api,
                    coordinator,
                    runtime_data,
                    pending_topology_endpoints,
                )
            finally:
                topology_retry_in_progress = False
            if (
                not reload_needed
                or hass.data.get(DOMAIN, {}).get(entry.entry_id)
                is not runtime_data
            ):
                return

            # One recovered endpoint is enough to reload: waiting for every
            # independent optional endpoint would let one persistent failure
            # suppress valid inverter, module-field or bus entities forever.
            topology_reload_requested = True
            hass.async_create_task(
                hass.config_entries.async_reload(entry.entry_id),
                "smart1 EMS recovered topology reload",
            )

        entry.async_on_unload(
            async_track_time_interval(
                hass,
                _async_retry_topology,
                TOPOLOGY_RETRY_INTERVAL,
            )
        )

    if not discovery_result.has_pv:
        pv_reload_requested = False

        def _promote_late_pv_capability() -> None:
            """Reload once when a later poll first proves PV support."""
            nonlocal pv_reload_requested
            if (
                pv_reload_requested
                or coordinator.data.get("pv_energy_today") is None
            ):
                return
            pv_reload_requested = True
            discovery_result.has_pv = True
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    PV_CAPABILITY_CONFIRMED_KEY: True,
                },
            )
            hass.async_create_task(
                hass.config_entries.async_reload(entry.entry_id),
                "smart1 EMS late PV capability reload",
            )

        entry.async_on_unload(
            coordinator.async_add_listener(_promote_late_pv_capability)
        )

    history_importers = []
    managed_legacy_statistic_ids: set[str] = set()
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
        pv_importer = Smart1PvHistoryImporter(
            hass,
            api,
            pv_power_point,
            statistics_namespace=statistics_namespace,
            history_state=history_state,
            force_initial_rebuild=force_legacy_history_rebuild,
        )
        history_importers.append(pv_importer)
        if not statistics_namespace:
            managed_legacy_statistic_ids.add(pv_importer.statistic_id)
    elif not statistics_namespace:
        # An empty current-day PV response is not proof that this installation
        # has no PV: it can also occur before first production. Keep the legacy
        # statistic unless another configured installation has independently
        # confirmed PV capability, in which case the unscoped rows cannot be
        # attributed safely to this legacy owner and isolation may remove them.
        raw_history_versions = entry.data.get(HISTORY_SCHEMA_VERSIONS_KEY, {})
        current_device_id = _normalized_device_id(entry.data.get("device_id"))
        other_entry_confirms_shared_pv = any(
            candidate.entry_id != entry.entry_id
            and _normalized_device_id(candidate.data.get("device_id"))
            not in {"", current_device_id}
            and _entry_proves_legacy_shared_pv(candidate)
            for candidate in hass.config_entries.async_entries(DOMAIN)
        )
        if not other_entry_confirms_shared_pv or (
            isinstance(raw_history_versions, Mapping)
            and PV_STATISTIC_ID in raw_history_versions
        ):
            managed_legacy_statistic_ids.add(PV_STATISTIC_ID)

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
            Smart1DerivedEnergyImporter(
                hass,
                api,
                role_points,
                statistics_namespace=statistics_namespace,
                history_state=history_state,
                force_initial_rebuild=force_legacy_history_rebuild,
            )
        )
        if not statistics_namespace:
            managed_legacy_statistic_ids.update(
                statistic_id_for_role(role_key, point.id)
                for role_key, point in role_points.items()
            )

    legacy_cleanup_required = (
        force_legacy_history_rebuild and not statistics_namespace
    )

    if history_importers:
        hass.data[DOMAIN][entry.entry_id]["history_importers"] = history_importers

    async def _async_refresh_history(_now=None) -> None:
        for history_importer in history_importers:
            await history_importer.async_import()

    async def _async_refresh_current_day(_now=None) -> None:
        for history_importer in history_importers:
            await history_importer.async_import(1, repair=False)

    async def _async_initial_history_import() -> None:
        if legacy_cleanup_required:
            candidate_ids = _legacy_statistic_ids(
                list(hass.config_entries.async_entries(DOMAIN))
            )
            recorder_ids: set[str] = set()
            try:
                recorder_ids = await _async_recorder_legacy_statistic_ids(
                    hass
                )
            except Exception as err:  # noqa: BLE001
                # Cleanup is best effort and must never prevent live sensors
                # from loading. A later reload retries the metadata scan.
                _LOGGER.warning(
                    "Unable to inspect unscoped smart1 history statistics "
                    "(%s)",
                    type(err).__name__,
                )
            candidate_ids.update(recorder_ids)

            cleaned_ids = _stored_orphan_cleanup_ids(entry.data)
            raw_versions = entry.data.get(HISTORY_SCHEMA_VERSIONS_KEY, {})
            rebuilt_ids = (
                set(raw_versions).intersection(cleaned_ids)
                if isinstance(raw_versions, Mapping)
                else set()
            )
            # Recorder metadata is authoritative for whether a previously
            # cleared ID is still empty. This also rearms cleanup when a
            # cancelled rebuild queued rows before persisting schema state.
            still_clean_ids = cleaned_ids - rebuilt_ids - recorder_ids
            orphaned_legacy_statistic_ids = (
                candidate_ids
                - managed_legacy_statistic_ids
                - still_clean_ids
            )
            await _async_clear_orphaned_legacy_statistics(
                hass,
                entry,
                history_state,
                orphaned_legacy_statistic_ids,
            )
        await _async_refresh_history()

    if history_importers or legacy_cleanup_required:
        entry.async_create_background_task(
            hass,
            _async_initial_history_import(),
            "smart1 EMS history import",
        )

    if history_importers:
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

    # Consume a recovery handoff only after the platform setup succeeded. If
    # the reload fails earlier, the next Home Assistant retry must still use
    # the topology that already succeeded instead of re-requesting it.
    if cached_topology_endpoints:
        current_cache_by_entry = hass.data.get(
            TOPOLOGY_RECOVERY_CACHE_KEY,
            {},
        )
        current_entry_cache = current_cache_by_entry.get(entry.entry_id, {})
        for endpoint in cached_topology_endpoints:
            cached_result = topology_recovery_cache.get(endpoint)
            if current_entry_cache.get(endpoint) is cached_result:
                current_entry_cache.pop(endpoint, None)
        if not current_entry_cache:
            current_cache_by_entry.pop(entry.entry_id, None)
        if not current_cache_by_entry:
            hass.data.pop(TOPOLOGY_RECOVERY_CACHE_KEY, None)
    return True


def _normalized_device_id(value: object) -> str:
    """Normalize the stable installation ID stored in a config entry."""
    return value.strip() if isinstance(value, str) else ""


async def async_migrate_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Add stable installation and statistics identity metadata."""
    if entry.version != 1:
        _LOGGER.error("Unsupported smart1 config-entry migration version")
        return False

    if entry.minor_version >= 3:
        return True

    device_id = _normalized_device_id(entry.data.get("device_id"))
    if not device_id:
        _LOGGER.error(
            "Cannot migrate smart1 config entry without an installation ID"
        )
        return False

    existing_unique_id = _normalized_device_id(entry.unique_id)
    if existing_unique_id and existing_unique_id != device_id:
        _LOGGER.error(
            "Cannot migrate smart1 config entry with conflicting identity"
        )
        return False

    entries = list(hass.config_entries.async_entries(DOMAIN))
    if entry.minor_version < 2:
        same_device_entries = [
            candidate
            for candidate in entries
            if _normalized_device_id(candidate.data.get("device_id"))
            == device_id
        ]
        unique_id_owners = [
            candidate
            for candidate in entries
            if _normalized_device_id(candidate.unique_id) == device_id
        ]
        active_same_device_entries = [
            candidate
            for candidate in same_device_entries
            if getattr(candidate, "disabled_by", None) is None
        ]
        candidates = (
            unique_id_owners
            or active_same_device_entries
            or same_device_entries
        )
        if candidates:
            winner = min(candidates, key=lambda candidate: candidate.entry_id)
            if winner.entry_id != entry.entry_id:
                _LOGGER.error(
                    "Cannot migrate duplicate smart1 config entries "
                    "automatically"
                )
                return False

    # Preserve existing Energy Dashboard selections for exactly one legacy
    # entry. Every other existing entry, and every newly created entry, gets a
    # stable installation scope so statistics can never be mixed again.
    explicit_legacy_owners = [
        candidate
        for candidate in entries
        if candidate.data.get(STATISTICS_NAMESPACE_KEY) == ""
        and STATISTICS_NAMESPACE_KEY in candidate.data
    ]
    unmigrated_entries = [
        candidate
        for candidate in entries
        if STATISTICS_NAMESPACE_KEY not in candidate.data
        and getattr(candidate, "minor_version", 0) < 3
        and _normalized_device_id(candidate.data.get("device_id"))
    ]
    representatives = []
    for candidate_device_id in {
        _normalized_device_id(candidate.data.get("device_id"))
        for candidate in unmigrated_entries
    }:
        same_device = [
            candidate
            for candidate in unmigrated_entries
            if _normalized_device_id(candidate.data.get("device_id"))
            == candidate_device_id
        ]
        unique_id_owners = [
            candidate
            for candidate in same_device
            if _normalized_device_id(candidate.unique_id)
            == candidate_device_id
        ]
        active_entries = [
            candidate
            for candidate in same_device
            if getattr(candidate, "disabled_by", None) is None
        ]
        representatives.append(
            min(
                unique_id_owners or active_entries or same_device,
                key=lambda candidate: candidate.entry_id,
            )
        )
    legacy_candidates = explicit_legacy_owners or representatives
    legacy_owner = (
        min(legacy_candidates, key=lambda candidate: candidate.entry_id)
        if legacy_candidates
        else None
    )
    statistics_namespace = (
        ""
        if legacy_owner is not None
        and legacy_owner.entry_id == entry.entry_id
        else statistics_namespace_for_device(device_id)
    )
    legacy_installation_ids = {
        candidate_device_id
        for candidate in [*explicit_legacy_owners, *unmigrated_entries]
        if (candidate_device_id := _normalized_device_id(
            candidate.data.get("device_id")
        ))
    }
    owner_requires_history_rebuild = bool(
        legacy_owner
        and (
            legacy_owner.data.get(LEGACY_HISTORY_REBUILD_KEY) is True
            or len(legacy_installation_ids) > 1
        )
    )
    # Migration order must not decide whether the legacy-ID owner is cleaned.
    # If a scoped sibling migrates first, persist the decision on the owner
    # immediately; once the sibling is current it is intentionally no longer
    # distinguishable from an entry created with installation-scoped IDs.
    if (
        owner_requires_history_rebuild
        and legacy_owner is not None
        and legacy_owner.entry_id != entry.entry_id
        and legacy_owner.data.get(LEGACY_HISTORY_REBUILD_KEY) is not True
    ):
        hass.config_entries.async_update_entry(
            legacy_owner,
            data={
                **legacy_owner.data,
                LEGACY_HISTORY_REBUILD_KEY: True,
            },
        )
    if statistics_namespace:
        _LOGGER.warning(
            "Assigning installation-scoped smart1 energy statistics. "
            "Existing Energy Dashboard selections for this additional "
            "installation must be selected again; previously shared history "
            "cannot be separated automatically"
        )

    updated_data = {
        **entry.data,
        "device_id": device_id,
        STATISTICS_NAMESPACE_KEY: statistics_namespace,
    }
    if (
        owner_requires_history_rebuild
        and legacy_owner is not None
        and legacy_owner.entry_id == entry.entry_id
    ):
        updated_data[LEGACY_HISTORY_REBUILD_KEY] = True

    hass.config_entries.async_update_entry(
        entry,
        data=updated_data,
        unique_id=device_id,
        version=1,
        minor_version=3,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a smart1 EMS config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    runtime_data = hass.data[DOMAIN][entry.entry_id]
    runtime_data["history_state"].deactivate()
    hass.data[DOMAIN].pop(entry.entry_id)
    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)

    return True
