from __future__ import annotations

import asyncio
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

custom_components = sys.modules.setdefault(
    "custom_components",
    types.ModuleType("custom_components"),
)
custom_components.__path__ = [str(ROOT / "custom_components")]
smart1_csv = sys.modules.setdefault(
    "custom_components.smart1_csv",
    types.ModuleType("custom_components.smart1_csv"),
)
smart1_csv.__path__ = [str(ROOT / "custom_components" / "smart1_csv")]

coordinator_module = importlib.import_module(
    "custom_components.smart1_csv.coordinator"
)
Smart1Coordinator = coordinator_module.Smart1Coordinator


class LiveOnlyApi:
    async def get_latest_linear_values(self, linear_ids):
        return {linear_id: 123.0 for linear_id in linear_ids}

    async def get_pv_cumulative_energy(self):
        raise ClientError("No PV data")


class Smart1CoordinatorTest(unittest.TestCase):
    def test_optional_pv_failure_keeps_live_values(self) -> None:
        coordinator = Smart1Coordinator(None, LiveOnlyApi(), [], ["point-1"])

        data = asyncio.run(coordinator._async_update_data())

        self.assertEqual(data["live"], {"point-1": 123.0})
        self.assertIsNone(data["pv_energy_today"])


if __name__ == "__main__":
    unittest.main()
