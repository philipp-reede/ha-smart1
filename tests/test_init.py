from __future__ import annotations

import asyncio
from datetime import timedelta
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, call, patch


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
            pass

        class ConfigEntryNotReady(Exception):
            pass

        point = types.SimpleNamespace(
            id="pv-power",
            source="counter",
            type="Energy",
            hardware="pv_global",
        )

        class FakeApi:
            instance = None
            linear_error = None

            def __init__(self, session, api_key, device_id) -> None:
                type(self).instance = self
                self.get_linear_devices = AsyncMock(
                    return_value=[point],
                    side_effect=type(self).linear_error,
                )
                self.get_inverters = AsyncMock(return_value=[])
                self.get_module_fields = AsyncMock(return_value=[])
                self.get_buses_with_probe = AsyncMock(
                    return_value=([], {"endpoint_result": "empty_response"})
                )

        class FakeCoordinator:
            instance = None

            def __init__(self, *args, **kwargs) -> None:
                type(self).instance = self
                self.async_config_entry_first_refresh = AsyncMock()

        class FakeHistoryImporter:
            instance = None

            def __init__(self, *args, **kwargs) -> None:
                type(self).instance = self
                self.async_import = AsyncMock()

        class FakeDerivedImporter:
            def __init__(self, *args, **kwargs) -> None:
                self.async_import = AsyncMock()

        class FakeDiscoveryResult:
            has_pv = True
            inverter_count = 0
            module_field_count = 0
            bus_count = 0

            def to_dict(self) -> dict:
                return {"has_pv": self.has_pv}

        class FakeDiscovery:
            def analyze(self, devices):
                return FakeDiscoveryResult()

        scheduled: list[tuple[object, timedelta]] = []

        def async_track_time_interval(hass, callback, interval):
            scheduled.append((callback, interval))
            return Mock(name=f"cancel_{len(scheduled)}")

        homeassistant = _module("homeassistant")
        homeassistant.__path__ = []
        helpers = _module("homeassistant.helpers")
        helpers.__path__ = []
        custom_components = _module("custom_components")
        custom_components.__path__ = [str(ROOT / "custom_components")]
        smart1_package = _module("custom_components.smart1_ems")
        smart1_package.__path__ = [
            str(ROOT / "custom_components" / "smart1_ems")
        ]

        modules = {
            "aiohttp": _module("aiohttp", ClientError=ClientError),
            "homeassistant": homeassistant,
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
                describe_api_error=lambda error: type(error).__name__,
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
            ),
            "custom_components.smart1_ems.history": _module(
                "custom_components.smart1_ems.history",
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

        hass = types.SimpleNamespace(
            data={},
            config_entries=types.SimpleNamespace(
                async_forward_entry_setups=AsyncMock(),
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

        asyncio.run(run_setup_and_callbacks())

        self.assertIn("entry-1", hass.data["smart1_ems"])
        self.assertEqual(len(entry.unload_callbacks), 2)
        hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
            entry,
            ["sensor"],
        )


if __name__ == "__main__":
    unittest.main()
