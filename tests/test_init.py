from __future__ import annotations

import asyncio
from datetime import timedelta
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import ANY, AsyncMock, Mock, call, patch


ROOT = Path(__file__).parents[1]


def _module(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


class Smart1SetupTest(unittest.TestCase):
    def test_history_refresh_schedules_repair_and_current_day_separately(
        self,
    ) -> None:
        """Wire startup, repair and current-day imports with distinct modes."""

        class ClientError(Exception):
            pass

        class Smart1ApiError(Exception):
            def __init__(self, code: object) -> None:
                match = re.match(r"^\s*(\d{3})(?:\D|$)", str(code))
                self.code = match.group(1) if match else "unknown"
                super().__init__(f"smart1 API error {self.code}")

        class ConfigEntryNotReady(Exception):
            pass

        class ConfigEntryAuthFailed(Exception):
            pass

        def describe_api_error(error: BaseException) -> str:
            if isinstance(error, Smart1ApiError):
                return f"smart1 API error {error.code}"
            return type(error).__name__

        def is_auth_error(error: BaseException) -> bool:
            return (
                isinstance(error, Smart1ApiError)
                and error.code in {"401", "403"}
            )

        point = types.SimpleNamespace(
            id="pv-power",
            source="counter",
            type="Energy",
            hardware="pv_global",
        )

        class FakeApi:
            instance = None
            linear_error = None
            linear_devices = [point]
            inverter_error = None
            module_field_error = None
            bus_error = None

            def __init__(self, session, api_key, device_id) -> None:
                type(self).instance = self
                self.get_linear_devices = AsyncMock(
                    return_value=list(type(self).linear_devices),
                    side_effect=type(self).linear_error,
                )
                self.get_inverters_with_probe = AsyncMock(
                    return_value=(
                        [],
                        {
                            "endpoint_result": "empty_response",
                            "response_columns": ["Inverter Id"],
                        },
                    ),
                    side_effect=type(self).inverter_error,
                )
                self.get_module_fields_with_probe = AsyncMock(
                    return_value=(
                        [],
                        {
                            "endpoint_result": "empty_response",
                            "response_columns": ["ModulfieldId"],
                        },
                    ),
                    side_effect=type(self).module_field_error,
                )
                self.get_buses_with_probe = AsyncMock(
                    return_value=(
                        [],
                        {
                            "endpoint_result": "empty_response",
                            "response_columns": ["BusId"],
                        },
                    ),
                    side_effect=type(self).bus_error,
                )

        class FakeCoordinator:
            instance = None
            pv_energy_today = None
            pv_cumulative_authoritative = False

            def __init__(self, *args, **kwargs) -> None:
                type(self).instance = self
                self.config_entry = kwargs.get("config_entry")
                self.listeners = []
                self.data = {
                    "pv_energy_today": type(self).pv_energy_today,
                    "pv_cumulative_authoritative": (
                        type(self).pv_cumulative_authoritative
                    ),
                }
                self.async_config_entry_first_refresh = AsyncMock()

            def async_add_listener(self, listener):
                self.listeners.append(listener)
                return Mock(name="remove_coordinator_listener")

        class FakeHistoryImporter:
            instance = None

            def __init__(self, *args, **kwargs) -> None:
                type(self).instance = self
                self.pv_power_point = args[2]
                self.statistic_id = "smart1_ems:pv_production"
                self.async_import = AsyncMock()

        class FakeDerivedImporter:
            def __init__(self, *args, **kwargs) -> None:
                self.async_import = AsyncMock()

        class FakeDiscoveryResult:
            has_pv = True
            inverter_count = 0
            module_field_count = 0
            bus_count = 0

            def add_pv_evidence(
                self,
                *,
                inverter_count,
                module_field_count,
                cumulative_energy,
            ) -> None:
                self.inverter_count = inverter_count
                self.module_field_count = module_field_count
                self.has_pv = self.has_pv or any(
                    (
                        inverter_count > 0,
                        module_field_count > 0,
                        cumulative_energy is not None,
                    )
                )

            def to_dict(self) -> dict:
                return {"has_pv": self.has_pv}

        class FakeDiscovery:
            def analyze(self, devices):
                return FakeDiscoveryResult()

        scheduled: list[tuple[object, timedelta]] = []
        recorder_metadata = [
            {
                "source": "smart1_ems",
                "statistic_id": (
                    "smart1_ems:heat_pump_consumption_feedface"
                ),
            },
            {
                "source": "smart1_ems",
                "statistic_id": (
                    "smart1_ems:heat_pump_consumption_feedface_0123456789"
                ),
            },
            {
                "source": "other_integration",
                "statistic_id": "smart1_ems:grid_import_cafebabe",
            },
        ]

        def clear_statistics(statistic_ids, *, on_done) -> None:
            recorder_metadata[:] = [
                item
                for item in recorder_metadata
                if item.get("statistic_id") not in statistic_ids
            ]
            on_done()

        recorder = types.SimpleNamespace(
            async_clear_statistics=Mock(side_effect=clear_statistics),
        )

        async def list_statistic_ids(_hass, _statistic_ids):
            return list(recorder_metadata)

        async_list_statistic_ids = AsyncMock(
            side_effect=list_statistic_ids,
        )

        def async_track_time_interval(hass, callback, interval):
            scheduled.append((callback, interval))
            return Mock(name=f"cancel_{len(scheduled)}")

        homeassistant = _module("homeassistant")
        homeassistant.__path__ = []
        helpers = _module("homeassistant.helpers")
        helpers.__path__ = []
        components = _module("homeassistant.components")
        components.__path__ = []
        custom_components = _module("custom_components")
        custom_components.__path__ = [str(ROOT / "custom_components")]
        smart1_package = _module("custom_components.smart1_ems")
        smart1_package.__path__ = [
            str(ROOT / "custom_components" / "smart1_ems")
        ]

        modules = {
            "aiohttp": _module("aiohttp", ClientError=ClientError),
            "homeassistant": homeassistant,
            "homeassistant.components": components,
            "homeassistant.components.recorder": _module(
                "homeassistant.components.recorder",
                get_instance=lambda hass: recorder,
            ),
            "homeassistant.components.recorder.statistics": _module(
                "homeassistant.components.recorder.statistics",
                async_list_statistic_ids=async_list_statistic_ids,
            ),
            "homeassistant.config_entries": _module(
                "homeassistant.config_entries",
                ConfigEntry=object,
            ),
            "homeassistant.core": _module(
                "homeassistant.core",
                HomeAssistant=object,
            ),
            "homeassistant.exceptions": _module(
                "homeassistant.exceptions",
                ConfigEntryAuthFailed=ConfigEntryAuthFailed,
                ConfigEntryNotReady=ConfigEntryNotReady,
            ),
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.aiohttp_client": _module(
                "homeassistant.helpers.aiohttp_client",
                async_get_clientsession=lambda hass: object(),
            ),
            "homeassistant.helpers.event": _module(
                "homeassistant.helpers.event",
                async_track_time_interval=async_track_time_interval,
            ),
            "custom_components": custom_components,
            "custom_components.smart1_ems": smart1_package,
            "custom_components.smart1_ems.api": _module(
                "custom_components.smart1_ems.api",
                Smart1Api=FakeApi,
                Smart1ApiError=Smart1ApiError,
                describe_api_error=describe_api_error,
                is_auth_error=is_auth_error,
                sanitize_api_error_code=lambda code: "unknown",
            ),
            "custom_components.smart1_ems.const": _module(
                "custom_components.smart1_ems.const",
                DOMAIN="smart1_ems",
            ),
            "custom_components.smart1_ems.coordinator": _module(
                "custom_components.smart1_ems.coordinator",
                Smart1Coordinator=FakeCoordinator,
            ),
            "custom_components.smart1_ems.derived_history": _module(
                "custom_components.smart1_ems.derived_history",
                Smart1DerivedEnergyImporter=FakeDerivedImporter,
            ),
            "custom_components.smart1_ems.discovery": _module(
                "custom_components.smart1_ems.discovery",
                Smart1Discovery=FakeDiscovery,
            ),
            "custom_components.smart1_ems.energy_roles": _module(
                "custom_components.smart1_ems.energy_roles",
                ENERGY_ROLES_BY_KEY={},
                energy_candidates=lambda devices, role: [],
                statistic_id_for_role=(
                    lambda role_key, point_id, statistics_namespace="": (
                        f"smart1_ems:{role_key}_{point_id}"
                    )
                ),
            ),
            "custom_components.smart1_ems.history": _module(
                "custom_components.smart1_ems.history",
                PV_STATISTIC_ID="smart1_ems:pv_production",
                Smart1PvHistoryImporter=FakeHistoryImporter,
            ),
        }

        entry = types.SimpleNamespace(
            entry_id="entry-1",
            data={"api_key": "redacted", "device_id": "plant-1"},
            options={},
            background_coroutines=[],
            unload_callbacks=[],
        )

        def async_create_background_task(hass, coroutine, name) -> None:
            entry.background_coroutines.append((coroutine, name))

        def async_on_unload(callback) -> None:
            entry.unload_callbacks.append(callback)

        entry.async_create_background_task = async_create_background_task
        entry.async_on_unload = async_on_unload

        configured_entries = [entry]

        def async_update_entry(target, *, data) -> None:
            target.data = dict(data)

        hass = types.SimpleNamespace(
            data={},
            config_entries=types.SimpleNamespace(
                async_forward_entry_setups=AsyncMock(),
                async_entries=lambda domain: list(configured_entries),
                async_update_entry=async_update_entry,
                async_reload=AsyncMock(),
            ),
            async_create_task=lambda coroutine, name: asyncio.create_task(
                coroutine,
                name=name,
            ),
        )

        module_name = "custom_components.smart1_ems.entrypoint_under_test"
        loader = SourceFileLoader(
            module_name,
            str(ROOT / "custom_components" / "smart1_ems" / "__init__.py"),
        )
        spec = spec_from_loader(module_name, loader, is_package=False)
        assert spec is not None
        integration = module_from_spec(spec)

        async def run_setup_and_callbacks() -> None:
            with patch.dict(sys.modules, modules):
                loader.exec_module(integration)
                self.assertTrue(
                    await integration.async_setup_entry(hass, entry)
                )
                self.assertIs(FakeCoordinator.instance.config_entry, entry)
                runtime_data = hass.data["smart1_ems"][entry.entry_id]
                self.assertTrue(
                    runtime_data["inverter_discovery_authoritative"]
                )
                self.assertTrue(
                    runtime_data["module_field_discovery_authoritative"]
                )
                self.assertTrue(
                    runtime_data["bus_discovery_authoritative"]
                )

                self.assertEqual(len(entry.background_coroutines), 1)
                background, task_name = entry.background_coroutines[0]
                self.assertEqual(task_name, "smart1 EMS history import")
                await background

                importer = FakeHistoryImporter.instance
                assert importer is not None
                importer.async_import.assert_awaited_once_with()
                importer.async_import.reset_mock()

                self.assertEqual(
                    [interval for _, interval in scheduled],
                    [timedelta(hours=6), timedelta(minutes=15)],
                )

                await scheduled[0][0]()
                await scheduled[1][0]()

                self.assertEqual(
                    importer.async_import.await_args_list,
                    [call(), call(1, repair=False)],
                )

                # Optional static topology must remain non-blocking, but a
                # transient first failure must heal without a manual reload.
                # Once any failed endpoint responds, reload exactly once so
                # the sensor platform can create the recovered entities.
                entry.background_coroutines.clear()
                scheduled_before_retry = len(scheduled)
                topology_error = ClientError(
                    "Request failed for "
                    "https://example.test/?apikey=private-topology-key"
                )
                FakeApi.inverter_error = topology_error
                FakeApi.module_field_error = topology_error
                FakeApi.bus_error = topology_error
                hass.config_entries.async_reload.reset_mock()
                try:
                    self.assertTrue(
                        await integration.async_setup_entry(hass, entry)
                    )
                finally:
                    FakeApi.inverter_error = None
                    FakeApi.module_field_error = None
                    FakeApi.bus_error = None

                retry_runtime = hass.data["smart1_ems"][entry.entry_id]
                self.assertFalse(
                    retry_runtime["inverter_discovery_authoritative"]
                )
                self.assertFalse(
                    retry_runtime["module_field_discovery_authoritative"]
                )
                self.assertFalse(
                    retry_runtime["bus_discovery_authoritative"]
                )
                retry_api = FakeApi.instance
                topology_callbacks = [
                    callback
                    for callback, _interval in scheduled[
                        scheduled_before_retry:
                    ]
                    if getattr(callback, "__name__", "")
                    == "_async_retry_topology"
                ]
                self.assertEqual(len(topology_callbacks), 1)
                with self.assertLogs(
                    integration._LOGGER,
                    level="DEBUG",
                ) as topology_logs:
                    await topology_callbacks[0]()
                self.assertNotIn(
                    "private-topology-key",
                    "\n".join(topology_logs.output),
                )
                hass.config_entries.async_reload.assert_not_awaited()

                # A successful but schema-less blank response is still
                # ambiguous. It must neither reload nor consume the retry.
                for topology_mock in (
                    retry_api.get_inverters_with_probe,
                    retry_api.get_module_fields_with_probe,
                    retry_api.get_buses_with_probe,
                ):
                    topology_mock.side_effect = None
                    topology_mock.return_value = (
                        [],
                        {
                            "endpoint_result": "empty_response",
                            "response_columns": [],
                        },
                    )
                await topology_callbacks[0]()
                await asyncio.sleep(0)
                hass.config_entries.async_reload.assert_not_awaited()
                self.assertTrue(
                    integration._topology_probe_needs_retry(
                        [],
                        {
                            "endpoint_result": "empty_response",
                            "response_columns": [],
                        },
                        integration._INVERTER_ID_COLUMNS,
                    )
                )

                recovered_inverter = object()
                recovered_module_field = object()
                recovered_bus = object()
                retry_api.get_inverters_with_probe.side_effect = None
                retry_api.get_inverters_with_probe.return_value = (
                    [recovered_inverter],
                    {
                        "endpoint_result": "data_returned",
                        "response_columns": ["Inverter Id"],
                    },
                )
                retry_api.get_module_fields_with_probe.side_effect = None
                retry_api.get_module_fields_with_probe.return_value = (
                    [recovered_module_field],
                    {
                        "endpoint_result": "data_returned",
                        "response_columns": ["ModulfieldId"],
                    },
                )
                retry_api.get_buses_with_probe.side_effect = None
                retry_api.get_buses_with_probe.return_value = (
                    [recovered_bus],
                    {
                        "endpoint_result": "data_returned",
                        "response_columns": ["BusId"],
                    },
                )

                await topology_callbacks[0]()
                await asyncio.sleep(0)

                hass.config_entries.async_reload.assert_awaited_once_with(
                    entry.entry_id
                )
                self.assertEqual(
                    retry_runtime["inverters"],
                    [recovered_inverter],
                )
                self.assertEqual(
                    retry_runtime["module_fields"],
                    [recovered_module_field],
                )
                self.assertEqual(retry_runtime["buses"], [recovered_bus])
                self.assertEqual(
                    FakeCoordinator.instance.inverters,
                    [recovered_inverter],
                )
                self.assertTrue(
                    retry_runtime["inverter_discovery_authoritative"]
                )
                self.assertTrue(
                    retry_runtime["module_field_discovery_authoritative"]
                )
                self.assertTrue(
                    retry_runtime["bus_discovery_authoritative"]
                )

                # Simulate the reload while every immediate portal request
                # fails again. The in-memory handoff must be consumed instead
                # of re-requesting metadata and scheduling another recovery
                # reload cycle.
                FakeApi.inverter_error = topology_error
                FakeApi.module_field_error = topology_error
                FakeApi.bus_error = topology_error
                try:
                    self.assertTrue(
                        await integration.async_setup_entry(hass, entry)
                    )
                finally:
                    FakeApi.inverter_error = None
                    FakeApi.module_field_error = None
                    FakeApi.bus_error = None
                reloaded_api = FakeApi.instance
                reloaded_api.get_inverters_with_probe.assert_not_awaited()
                reloaded_api.get_module_fields_with_probe.assert_not_awaited()
                reloaded_api.get_buses_with_probe.assert_not_awaited()
                reloaded_runtime = hass.data["smart1_ems"][entry.entry_id]
                self.assertEqual(
                    reloaded_runtime["inverters"],
                    [recovered_inverter],
                )
                self.assertNotIn(
                    integration.TOPOLOGY_RECOVERY_CACHE_KEY,
                    hass.data,
                )

                # The old interval callback can race with unload, but its
                # guard must prevent both another request and reload.
                await topology_callbacks[0]()
                await asyncio.sleep(0)
                hass.config_entries.async_reload.assert_awaited_once()
                self.assertEqual(
                    retry_api.get_inverters_with_probe.await_count,
                    4,
                )
                self.assertEqual(
                    retry_api.get_module_fields_with_probe.await_count,
                    4,
                )
                self.assertEqual(
                    retry_api.get_buses_with_probe.await_count,
                    4,
                )
                for retry_background, _name in entry.background_coroutines:
                    await retry_background
                entry.background_coroutines.clear()
                hass.config_entries.async_reload.reset_mock()

                api_key = "fake-api-key-must-not-leak"
                FakeApi.linear_error = ClientError(
                    "Request failed for "
                    f"https://example.test/?apikey={api_key}"
                )
                try:
                    with self.assertRaises(ConfigEntryNotReady) as raised:
                        await integration.async_setup_entry(hass, entry)
                finally:
                    FakeApi.linear_error = None

                self.assertEqual(str(raised.exception), "ClientError")
                self.assertNotIn(api_key, str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(raised.exception.__suppress_context__)

                for error_code in ("500", "unknown"):
                    with self.subTest(error_code=error_code):
                        api_key = "fake-api-key-must-not-leak"
                        FakeApi.linear_error = Smart1ApiError(
                            f"{error_code} response for apikey={api_key}"
                        )
                        try:
                            with self.assertRaises(
                                ConfigEntryNotReady
                            ) as raised:
                                await integration.async_setup_entry(hass, entry)
                        finally:
                            FakeApi.linear_error = None

                        self.assertNotIn(api_key, str(raised.exception))
                        self.assertIsNone(raised.exception.__cause__)
                        self.assertTrue(
                            raised.exception.__suppress_context__
                        )

                for error_code in ("401", "403"):
                    with self.subTest(error_code=error_code):
                        api_key = "fake-api-key-must-not-leak"
                        FakeApi.linear_error = Smart1ApiError(
                            f"{error_code} response for apikey={api_key}"
                        )
                        try:
                            with self.assertRaises(
                                ConfigEntryAuthFailed
                            ) as raised:
                                await integration.async_setup_entry(hass, entry)
                        finally:
                            FakeApi.linear_error = None

                        self.assertEqual(
                            str(raised.exception),
                            f"smart1 API error {error_code}",
                        )
                        self.assertNotIn(api_key, str(raised.exception))
                        self.assertIsNone(raised.exception.__cause__)
                        self.assertTrue(
                            raised.exception.__suppress_context__
                        )

                # A successful cumulative request is sufficient proof of PV
                # support even if no linear point can be classified. It must
                # create the daily-only importer and protect its legacy
                # statistic from orphan cleanup.
                original_recorder_metadata = list(recorder_metadata)
                entry.background_coroutines.clear()
                entry.unload_callbacks.clear()
                scheduled.clear()
                FakeDiscoveryResult.has_pv = False
                FakeCoordinator.pv_energy_today = 0.0
                FakeApi.linear_devices = []
                integration.ENERGY_ROLES_BY_KEY[
                    "heat_pump_consumption"
                ] = object()
                entry.data = {
                    "api_key": "redacted",
                    "device_id": "plant-1",
                    "statistics_namespace": "",
                    "legacy_history_rebuild": True,
                }
                entry.options = {}
                new_scoped_sibling = types.SimpleNamespace(
                    entry_id="entry-new",
                    data={
                        "device_id": "plant-new",
                        "statistics_namespace": "new-scope",
                        "pv_capability_confirmed": True,
                    },
                    options={},
                )
                configured_entries[:] = [entry, new_scoped_sibling]

                self.assertTrue(
                    await integration.async_setup_entry(hass, entry)
                )
                runtime_data = hass.data["smart1_ems"][entry.entry_id]
                self.assertTrue(runtime_data["discovery"].has_pv)
                self.assertEqual(len(runtime_data["history_importers"]), 1)
                cumulative_evidence_task, _task_name = (
                    entry.background_coroutines[0]
                )
                await cumulative_evidence_task
                self.assertEqual(
                    recorder.async_clear_statistics.call_args.args[0],
                    ["smart1_ems:heat_pump_consumption_feedface"],
                )
                self.assertIsNone(
                    FakeHistoryImporter.instance.pv_power_point
                )

                recorder_metadata[:] = original_recorder_metadata
                recorder.async_clear_statistics.reset_mock()
                async_list_statistic_ids.reset_mock()
                entry.background_coroutines.clear()
                FakeCoordinator.pv_energy_today = None
                FakeApi.linear_devices = [point]

                # Existing PV history is protected when the optional
                # cumulative request is temporarily inconclusive at startup.
                # A later successful poll requests one reload so the static
                # sensor platform and history importer can be promoted.
                cumulative_unload_callbacks = list(entry.unload_callbacks)
                entry.background_coroutines.clear()
                entry.unload_callbacks.clear()
                scheduled.clear()
                recorder.async_clear_statistics.reset_mock()
                hass.config_entries.async_reload.reset_mock()
                FakeDiscoveryResult.has_pv = False
                FakeCoordinator.pv_energy_today = None
                FakeCoordinator.pv_cumulative_authoritative = True
                FakeApi.linear_devices = []
                recorder_metadata[:] = [
                    *original_recorder_metadata,
                    {
                        "source": "smart1_ems",
                        "statistic_id": "smart1_ems:pv_production",
                    },
                ]
                entry.data = {
                    "api_key": "redacted",
                    "device_id": "plant-1",
                    "statistics_namespace": "",
                    "legacy_history_rebuild": True,
                }
                entry.options = {}

                self.assertTrue(
                    await integration.async_setup_entry(hass, entry)
                )
                late_pv_runtime = hass.data["smart1_ems"][entry.entry_id]
                self.assertFalse(late_pv_runtime["discovery"].has_pv)
                self.assertNotIn("history_importers", late_pv_runtime)
                late_pv_cleanup, _task_name = entry.background_coroutines[0]
                await late_pv_cleanup
                cleared_ids = recorder.async_clear_statistics.call_args.args[0]
                self.assertNotIn("smart1_ems:pv_production", cleared_ids)
                self.assertEqual(len(FakeCoordinator.instance.listeners), 1)

                FakeCoordinator.instance.data["pv_energy_today"] = 0.0
                FakeCoordinator.instance.listeners[0]()
                await asyncio.sleep(0)
                hass.config_entries.async_reload.assert_awaited_once_with(
                    entry.entry_id
                )
                self.assertIs(
                    entry.data["pv_capability_confirmed"],
                    True,
                )

                # Capability proof must survive the reload itself. The next
                # setup can see another transient cumulative failure and must
                # still create the static PV sensor/history path immediately.
                entry.background_coroutines.clear()
                FakeCoordinator.pv_energy_today = None
                FakeCoordinator.pv_cumulative_authoritative = False
                self.assertTrue(
                    await integration.async_setup_entry(hass, entry)
                )
                persisted_pv_runtime = hass.data["smart1_ems"][entry.entry_id]
                self.assertTrue(persisted_pv_runtime["discovery"].has_pv)
                self.assertEqual(
                    len(persisted_pv_runtime["history_importers"]),
                    1,
                )
                persisted_pv_task, _task_name = (
                    entry.background_coroutines[0]
                )
                await persisted_pv_task

                recorder_metadata[:] = original_recorder_metadata
                recorder.async_clear_statistics.reset_mock()
                async_list_statistic_ids.reset_mock()
                entry.background_coroutines.clear()
                entry.unload_callbacks[:] = cumulative_unload_callbacks
                FakeCoordinator.pv_cumulative_authoritative = False
                FakeApi.linear_devices = [point]

                # A multi-installation legacy owner may have neither PV nor
                # selected derived roles. It must still remove shared history
                # written by a scoped sibling and by a previously deselected
                # role, while leaving scoped state untouched.
                initial_unload_callbacks = list(entry.unload_callbacks)
                entry.background_coroutines.clear()
                entry.unload_callbacks.clear()
                scheduled.clear()
                FakeDiscoveryResult.has_pv = False
                FakeCoordinator.pv_energy_today = None
                FakeCoordinator.pv_cumulative_authoritative = True
                FakeApi.linear_devices = []
                integration.ENERGY_ROLES_BY_KEY["grid_import"] = object()
                integration.ENERGY_ROLES_BY_KEY[
                    "heat_pump_consumption"
                ] = object()
                sibling = types.SimpleNamespace(
                    entry_id="entry-2",
                    data={
                        "device_id": "plant-2",
                        "statistics_namespace": "plant-2-scope",
                        # This old unscoped completion marker proves that the
                        # now-scoped sibling managed shared PV history before
                        # migration. It must work even when the legacy owner
                        # sets up before the sibling can persist the newer
                        # capability marker.
                        "history_schema_versions": {
                            "smart1_ems:pv_production": 5,
                        },
                    },
                    options={
                        "energy_roles": {
                            "grid_import": "sibling-power",
                        }
                    },
                )
                configured_entries[:] = [entry, sibling]
                legacy_role_id = "smart1_ems:grid_import_deadbeef"
                scoped_role_id = f"{legacy_role_id}_0123456789"
                entry.data = {
                    "api_key": "redacted",
                    "device_id": "plant-1",
                    "statistics_namespace": "",
                    "legacy_history_rebuild": True,
                    "history_schema_versions": {
                        legacy_role_id: 1,
                        scoped_role_id: 1,
                    },
                    "history_data_presence": {
                        legacy_role_id: {"1": True},
                        scoped_role_id: {"1": True},
                    },
                }
                entry.options = {}

                self.assertTrue(
                    await integration.async_setup_entry(hass, entry)
                )
                self.assertNotIn(
                    "history_importers",
                    hass.data["smart1_ems"][entry.entry_id],
                )
                self.assertEqual(len(entry.background_coroutines), 1)
                cleanup_task, task_name = entry.background_coroutines[0]
                self.assertEqual(task_name, "smart1 EMS history import")
                await cleanup_task

                recorder.async_clear_statistics.assert_called_once_with(
                    [
                        "smart1_ems:grid_import_deadbeef",
                        "smart1_ems:grid_import_sibling-power",
                        "smart1_ems:heat_pump_consumption_feedface",
                        "smart1_ems:pv_production",
                    ],
                    on_done=ANY,
                )
                self.assertEqual(scheduled, [])
                self.assertEqual(len(entry.unload_callbacks), 1)
                self.assertEqual(
                    entry.data["history_schema_versions"],
                    {scoped_role_id: 1},
                )
                self.assertEqual(
                    entry.data["history_data_presence"],
                    {scoped_role_id: {"1": True}},
                )
                self.assertEqual(
                    entry.data["legacy_orphan_cleanup_ids"],
                    [
                        "smart1_ems:grid_import_deadbeef",
                        "smart1_ems:grid_import_sibling-power",
                        "smart1_ems:heat_pump_consumption_feedface",
                        "smart1_ems:pv_production",
                    ],
                )
                async_list_statistic_ids.assert_awaited_once_with(hass, None)

                # The Recorder scan is cheap and intentionally repeated on
                # setup so a cancelled rebuild cannot hide an ID that was
                # managed during the first scan and deselected afterwards.
                entry.background_coroutines.clear()
                self.assertTrue(
                    await integration.async_setup_entry(hass, entry)
                )
                self.assertEqual(len(entry.background_coroutines), 1)
                repeated_scan, _task_name = entry.background_coroutines[0]
                await repeated_scan
                recorder.async_clear_statistics.assert_called_once()
                self.assertEqual(async_list_statistic_ids.await_count, 2)

                # A rebuild may enqueue rows and then be cancelled before its
                # schema marker is persisted. Recorder metadata must rearm
                # cleanup even though this ID was cleared previously.
                reappeared_id = (
                    "smart1_ems:heat_pump_consumption_feedface"
                )
                recorder_metadata.append(
                    {
                        "source": "smart1_ems",
                        "statistic_id": reappeared_id,
                    }
                )
                entry.background_coroutines.clear()
                self.assertTrue(
                    await integration.async_setup_entry(hass, entry)
                )
                reappeared_cleanup, _task_name = (
                    entry.background_coroutines[0]
                )
                await reappeared_cleanup
                self.assertEqual(
                    recorder.async_clear_statistics.call_args_list[-1].args[0],
                    [reappeared_id],
                )

                # If a previously cleaned role is later rebuilt, its schema
                # marker rearms cleanup when the role is deselected again.
                rebuilt_id = "smart1_ems:grid_import_sibling-power"
                entry.data["history_schema_versions"] = {rebuilt_id: 1}
                entry.data["history_data_presence"] = {
                    rebuilt_id: {"1": True}
                }
                entry.background_coroutines.clear()
                self.assertTrue(
                    await integration.async_setup_entry(hass, entry)
                )
                self.assertEqual(len(entry.background_coroutines), 1)
                cleanup_task, _task_name = entry.background_coroutines[0]
                await cleanup_task
                self.assertEqual(
                    recorder.async_clear_statistics.call_args_list[-1].args[0],
                    [rebuilt_id],
                )
                self.assertNotIn(
                    rebuilt_id,
                    entry.data["history_schema_versions"],
                )

                # Recorder maintenance must remain best effort: a transient
                # metadata-query failure cannot block live entities, escape
                # the background task, or suppress a later setup retry.
                entry.background_coroutines.clear()
                async_list_statistic_ids.side_effect = RuntimeError(
                    "database unavailable"
                )
                self.assertTrue(
                    await integration.async_setup_entry(hass, entry)
                )
                failed_scan, _task_name = entry.background_coroutines[0]
                await failed_scan
                self.assertEqual(
                    recorder.async_clear_statistics.call_count,
                    3,
                )
                async_list_statistic_ids.side_effect = list_statistic_ids

                # A queued orphan clear cannot be cancelled after the helper
                # times out. Invalidate its completion marker while queueing
                # so an options reload rearms a full backfill. The delayed
                # callback may record cleanup, but must not erase a marker
                # written by that replacement generation.
                delayed_callbacks = []

                def delay_clear(_statistic_ids, *, on_done) -> None:
                    delayed_callbacks.append(on_done)

                recorder.async_clear_statistics.reset_mock()
                recorder.async_clear_statistics.side_effect = delay_clear
                delayed_id = "smart1_ems:grid_import_deadbeef"
                entry.data = {
                    "api_key": "redacted",
                    "device_id": "plant-1",
                    "statistics_namespace": "",
                    "legacy_history_rebuild": True,
                    "history_schema_versions": {delayed_id: 1},
                    "history_data_presence": {
                        delayed_id: {"1": True}
                    },
                }
                queued_state = integration.Smart1HistoryState(hass, entry)
                recorder_helpers = sys.modules[
                    "custom_components.smart1_ems.recorder_helpers"
                ]

                with (
                    patch.object(
                        recorder_helpers,
                        "RECORDER_OPERATION_TIMEOUT",
                        0.001,
                    ),
                    self.assertLogs(integration._LOGGER, level="WARNING"),
                ):
                    await integration._async_clear_orphaned_legacy_statistics(
                        hass,
                        entry,
                        queued_state,
                        {delayed_id},
                    )

                self.assertNotIn(
                    delayed_id,
                    entry.data["history_schema_versions"],
                )
                self.assertNotIn(
                    "legacy_orphan_cleanup_ids",
                    entry.data,
                )

                queued_state.deactivate()
                reloaded_state = integration.Smart1HistoryState(hass, entry)
                self.assertFalse(reloaded_state.is_complete(delayed_id, 1))
                reloaded_state.mark_complete(
                    delayed_id,
                    1,
                    has_data=True,
                )

                delayed_callbacks[0]()
                await asyncio.sleep(0)

                self.assertTrue(reloaded_state.is_complete(delayed_id, 1))
                self.assertEqual(
                    entry.data["legacy_orphan_cleanup_ids"],
                    [delayed_id],
                )

                FakeDiscoveryResult.has_pv = True
                entry.unload_callbacks[:] = initial_unload_callbacks

        asyncio.run(run_setup_and_callbacks())

        self.assertIn("entry-1", hass.data["smart1_ems"])
        self.assertEqual(len(entry.unload_callbacks), 2)
        self.assertEqual(
            hass.config_entries.async_forward_entry_setups.await_args_list,
            [
                call(entry, ["sensor"]),
                call(entry, ["sensor"]),
                call(entry, ["sensor"]),
                call(entry, ["sensor"]),
                call(entry, ["sensor"]),
                call(entry, ["sensor"]),
                call(entry, ["sensor"]),
                call(entry, ["sensor"]),
                call(entry, ["sensor"]),
                call(entry, ["sensor"]),
                call(entry, ["sensor"]),
            ],
        )


if __name__ == "__main__":
    unittest.main()
