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
update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")


class DataUpdateCoordinator:
    def __init__(self, *args, **kwargs) -> None:
        pass


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


class LiveOnlyApi:
    def __init__(self) -> None:
        self.target_dates = []

    async def get_latest_linear_values(self, linear_ids, *, target_date=None):
        self.target_dates.append(target_date)
        return {linear_id: 123.0 for linear_id in linear_ids}

    async def get_pv_cumulative_energy(self, *, target_date=None):
        self.target_dates.append(target_date)
        raise ClientError("No PV data")


class Smart1CoordinatorTest(unittest.TestCase):
    def test_optional_pv_failure_keeps_live_values(self) -> None:
        api = LiveOnlyApi()
        coordinator = Smart1Coordinator(None, api, [], ["point-1"])

        data = asyncio.run(coordinator._async_update_data())

        self.assertEqual(data["live"], {"point-1": 123.0})
        self.assertIsNone(data["pv_energy_today"])
        self.assertEqual(api.target_dates, [date(2026, 8, 4)] * 2)


if __name__ == "__main__":
    unittest.main()
