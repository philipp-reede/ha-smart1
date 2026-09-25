from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import importlib
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).parents[1]

aiohttp = sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))


class ClientError(Exception):
    pass


aiohttp.ClientError = ClientError

homeassistant = sys.modules.setdefault(
    "homeassistant",
    types.ModuleType("homeassistant"),
)
homeassistant.__path__ = []
helpers = sys.modules.setdefault(
    "homeassistant.helpers",
    types.ModuleType("homeassistant.helpers"),
)
helpers.__path__ = []
exceptions = types.ModuleType("homeassistant.exceptions")


class ConfigEntryAuthFailed(Exception):
    pass


exceptions.ConfigEntryAuthFailed = ConfigEntryAuthFailed
sys.modules["homeassistant.exceptions"] = exceptions
update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")


class DataUpdateCoordinator:
    def __init__(self, *args, **kwargs) -> None:
        self.config_entry = kwargs.get("config_entry")


class UpdateFailed(Exception):
    pass


update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
update_coordinator.UpdateFailed = UpdateFailed
sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator

util = sys.modules.setdefault(
    "homeassistant.util",
    types.ModuleType("homeassistant.util"),
)
util.__path__ = []
dt_util = types.ModuleType("homeassistant.util.dt")
dt_util.now = lambda: datetime(2026, 8, 4, tzinfo=timezone.utc)
sys.modules["homeassistant.util.dt"] = dt_util

custom_components = sys.modules.setdefault(
    "custom_components",
    types.ModuleType("custom_components"),
)
custom_components.__path__ = [str(ROOT / "custom_components")]
smart1_ems = sys.modules.setdefault(
    "custom_components.smart1_ems",
    types.ModuleType("custom_components.smart1_ems"),
)
smart1_ems.__path__ = [str(ROOT / "custom_components" / "smart1_ems")]

coordinator_module = importlib.import_module(
    "custom_components.smart1_ems.coordinator"
)
Smart1Coordinator = coordinator_module.Smart1Coordinator
Smart1ApiError = coordinator_module.Smart1ApiError


class LiveOnlyApi:
    def __init__(self) -> None:
        self.target_dates = []

    async def get_latest_linear_values(self, linear_ids, *, target_date=None):
        self.target_dates.append(target_date)
        return {linear_id: 123.0 for linear_id in linear_ids}

    async def get_pv_cumulative_energy(self, *, target_date=None):
        self.target_dates.append(target_date)
        raise ClientError("No PV data")


class InverterApi(LiveOnlyApi):
    async def get_latest_pv_string_samples(
        self,
        *,
        target_date=None,
        missing_ok=False,
    ):
        self.target_dates.append(target_date)
        self.missing_ok = missing_ok
        return {(2, 1, 1): "latest-sample"}


class FailingInverterApi(LiveOnlyApi):
    async def get_latest_pv_string_samples(self, **kwargs):
        raise ClientError("No inverter data")


class SecretOptionalErrorApi(LiveOnlyApi):
    def __init__(self, api_key: str) -> None:
        super().__init__()
        self.api_key = api_key

    async def get_pv_cumulative_energy(self, *, target_date=None):
        raise ClientError(
            f"Request failed for https://example.test/?apikey={self.api_key}"
        )

    async def get_latest_pv_string_samples(self, **kwargs):
        raise ClientError(
            f"Request failed for https://example.test/?apikey={self.api_key}"
        )


class FailingLiveApi:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def get_latest_linear_values(self, *args, **kwargs):
        raise ClientError(
            f"Request failed for https://example.test/?apikey={self.api_key}"
        )


class FailingApiErrorApi:
    def __init__(self, error_code: str, api_key: str) -> None:
        self.error_code = error_code
        self.api_key = api_key

    async def get_latest_linear_values(self, *args, **kwargs):
        raise Smart1ApiError(
            f"{self.error_code} response for apikey={self.api_key}"
        )


class EmptyLinearIdsApi(LiveOnlyApi):
    def __init__(
        self,
        error_code: str | None = None,
        *,
        device_id: str = "plant-1",
        plants: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self.error_code = error_code
        self.device_id = device_id
        self.plants = (
            [{"DeviceId": " plant-1 "}] if plants is None else plants
        )
        self.auth_probe_calls = 0
        self.live_calls = 0

    async def get_plants(self):
        self.auth_probe_calls += 1
        if self.error_code is not None:
            raise Smart1ApiError(self.error_code)
        return self.plants

    async def get_latest_linear_values(self, *args, **kwargs):
        self.live_calls += 1
        raise AssertionError("Empty point lists must use the auth probe")


class Smart1CoordinatorTest(unittest.TestCase):
    def test_coordinator_is_linked_to_config_entry(self) -> None:
        entry = object()

        coordinator = Smart1Coordinator(
            None,
            LiveOnlyApi(),
            [],
            ["point-1"],
            config_entry=entry,
        )

        self.assertIs(coordinator.config_entry, entry)

    def test_auth_failure_requests_reauthentication_without_secret(self) -> None:
        api_key = "fake-api-key-must-not-leak"

        for error_code in ("401", "403"):
            with self.subTest(error_code=error_code):
                coordinator = Smart1Coordinator(
                    None,
                    FailingApiErrorApi(error_code, api_key),
                    [],
                    ["point-1"],
                )

                with self.assertRaises(ConfigEntryAuthFailed) as raised:
                    asyncio.run(coordinator._async_update_data())

                self.assertEqual(
                    str(raised.exception),
                    f"smart1 API error {error_code}",
                )
                self.assertNotIn(api_key, str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(raised.exception.__suppress_context__)

    def test_empty_linear_ids_use_required_auth_probe(self) -> None:
        api = EmptyLinearIdsApi()
        coordinator = Smart1Coordinator(None, api, [], [])

        data = asyncio.run(coordinator._async_update_data())

        self.assertEqual(data["live"], {})
        self.assertEqual(api.auth_probe_calls, 1)
        self.assertEqual(api.live_calls, 0)

    def test_empty_linear_ids_auth_failure_requests_reauthentication(
        self,
    ) -> None:
        for error_code in ("401", "403"):
            with self.subTest(error_code=error_code):
                api = EmptyLinearIdsApi(error_code)
                coordinator = Smart1Coordinator(None, api, [], [])

                with self.assertRaises(ConfigEntryAuthFailed) as raised:
                    asyncio.run(coordinator._async_update_data())

                self.assertEqual(
                    str(raised.exception),
                    f"smart1 API error {error_code}",
                )
                self.assertEqual(api.auth_probe_calls, 1)
                self.assertEqual(api.live_calls, 0)

    def test_empty_linear_ids_missing_plant_requests_reauthentication(
        self,
    ) -> None:
        for plants in (
            [],
            [{"DeviceId": "other-plant"}],
            [{"DeviceId": "other-plant"}, {"DeviceName": "No ID"}],
        ):
            with self.subTest(plants=plants):
                api = EmptyLinearIdsApi(plants=plants)
                coordinator = Smart1Coordinator(None, api, [], [])

                with self.assertRaises(ConfigEntryAuthFailed) as raised:
                    asyncio.run(coordinator._async_update_data())

                self.assertEqual(
                    str(raised.exception),
                    "smart1 installation is no longer accessible",
                )
                self.assertNotIn(api.device_id, str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(raised.exception.__suppress_context__)
                self.assertEqual(api.auth_probe_calls, 1)
                self.assertEqual(api.live_calls, 0)

    def test_empty_linear_ids_probe_failure_remains_update_failed(self) -> None:
        api = EmptyLinearIdsApi("500")
        coordinator = Smart1Coordinator(None, api, [], [])

        with self.assertRaises(UpdateFailed) as raised:
            asyncio.run(coordinator._async_update_data())

        self.assertEqual(str(raised.exception), "smart1 API error 500")
        self.assertEqual(api.auth_probe_calls, 1)
        self.assertEqual(api.live_calls, 0)

    def test_non_auth_api_failures_remain_update_failed(self) -> None:
        api_key = "fake-api-key-must-not-leak"

        for error_code in ("500", "unknown"):
            with self.subTest(error_code=error_code):
                coordinator = Smart1Coordinator(
                    None,
                    FailingApiErrorApi(error_code, api_key),
                    [],
                    ["point-1"],
                )

                with self.assertRaises(UpdateFailed) as raised:
                    asyncio.run(coordinator._async_update_data())

                self.assertEqual(
                    str(raised.exception),
                    f"smart1 API error {error_code}",
                )
                self.assertNotIn(api_key, str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(raised.exception.__suppress_context__)

    def test_required_update_failure_does_not_expose_api_key(self) -> None:
        api_key = "fake-api-key-must-not-leak"
        coordinator = Smart1Coordinator(
            None,
            FailingLiveApi(api_key),
            [],
            ["point-1"],
        )

        with self.assertRaises(UpdateFailed) as raised:
            asyncio.run(coordinator._async_update_data())

        self.assertEqual(str(raised.exception), "ClientError")
        self.assertNotIn(api_key, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_optional_failure_logs_do_not_expose_api_key(self) -> None:
        api_key = "fake-api-key-must-not-leak"
        coordinator = Smart1Coordinator(
            None,
            SecretOptionalErrorApi(api_key),
            [],
            ["point-1"],
            [object()],
        )

        with self.assertLogs(
            coordinator_module._LOGGER,
            level="DEBUG",
        ) as captured:
            data = asyncio.run(coordinator._async_update_data())

        logs = "\n".join(captured.output)
        self.assertNotIn(api_key, logs)
        self.assertEqual(logs.count("ClientError"), 2)
        self.assertIsNone(data["pv_energy_today"])
        self.assertEqual(data["pv_strings"], {})

    def test_optional_pv_failure_keeps_live_values(self) -> None:
        api = LiveOnlyApi()
        coordinator = Smart1Coordinator(None, api, [], ["point-1"])

        data = asyncio.run(coordinator._async_update_data())

        self.assertEqual(data["live"], {"point-1": 123.0})
        self.assertIsNone(data["pv_energy_today"])
        self.assertEqual(data["pv_strings"], {})
        self.assertEqual(api.target_dates, [date(2026, 8, 4)] * 2)

    def test_optional_inverter_values_are_returned(self) -> None:
        api = InverterApi()
        coordinator = Smart1Coordinator(
            None,
            api,
            [],
            ["point-1"],
            [object()],
        )

        data = asyncio.run(coordinator._async_update_data())

        self.assertEqual(data["pv_strings"], {(2, 1, 1): "latest-sample"})
        self.assertTrue(api.missing_ok)
        self.assertEqual(api.target_dates, [date(2026, 8, 4)] * 3)

    def test_optional_inverter_failure_keeps_previous_values(self) -> None:
        coordinator = Smart1Coordinator(
            None,
            FailingInverterApi(),
            [],
            ["point-1"],
            [object()],
        )
        coordinator.data = {"pv_strings": {(2, 1, 1): "previous-sample"}}

        data = asyncio.run(coordinator._async_update_data())

        self.assertEqual(
            data["pv_strings"],
            {(2, 1, 1): "previous-sample"},
        )


if __name__ == "__main__":
    unittest.main()
