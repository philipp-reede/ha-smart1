from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).parents[1]

homeassistant = sys.modules.setdefault(
    "homeassistant",
    types.ModuleType("homeassistant"),
)
homeassistant.__path__ = []
config_entries = types.ModuleType("homeassistant.config_entries")
config_entries.ConfigEntry = object
sys.modules["homeassistant.config_entries"] = config_entries
core = types.ModuleType("homeassistant.core")
core.HomeAssistant = object
sys.modules["homeassistant.core"] = core

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

diagnostics = importlib.import_module("custom_components.smart1_ems.diagnostics")
discovery_module = importlib.import_module("custom_components.smart1_ems.discovery")
interface_module = importlib.import_module("custom_components.smart1_ems.interface")
point_module = importlib.import_module("custom_components.smart1_ems.point")


class Entry:
    entry_id = "entry-1"
    data = {"api_key": "secret", "device_id": "private-device"}


class Api:
    async def get_linear_cumulative_rows(self, *args, **kwargs):
        return [
            {
                "LinearId": "private-point-id",
                "Timestamp": "private-timestamp",
                "Value1": "private-energy-value",
            }
        ]

    async def get_linear_detailed_rows(self, *args, **kwargs):
        return [
            {
                "LinearId": "private-point-id",
                "Timestamp": "2026-08-03 00:00:00",
                "Value1": "1000",
            },
            {
                "LinearId": "private-point-id",
                "Timestamp": "2026-08-03 00:05:00",
                "Value1": "1000",
            },
            {
                "LinearId": "private-point-id",
                "Timestamp": "2026-08-03 00:10:00",
                "Value1": "1000",
            },
        ]

    async def get_pv_cumulative_energy(self, *args, **kwargs):
        return 1 / 6


class Hass:
    config = types.SimpleNamespace(time_zone="Europe/Berlin")
    data = {
        "smart1_ems": {
            "entry-1": {
                "api": Api(),
                "devices": [
                    point_module.Smart1Point(
                        id="private-point-id",
                        name="PV Erzeugung",
                        type="Energy",
                        source="counter",
                        hardware="pv_global",
                        interface="pv",
                        parsed_interface=interface_module.parse_interface(
                            "pv"
                        ),
                    )
                ],
                "discovery": discovery_module.Smart1DiscoveryResult(
                    has_pv=True
                ),
            }
        }
    }


class DiagnosticsTest(unittest.TestCase):
    def test_returns_classification_metadata_without_credentials_or_values(self) -> None:
        result = asyncio.run(
            diagnostics.async_get_config_entry_diagnostics(Hass(), Entry())
        )
        serialized = json.dumps(result)

        self.assertEqual(result["points"][0]["current_category"], "pv")
        self.assertEqual(
            result["linear_cumulative_probe"],
            {
                "period": "previous_complete_day",
                "requested_points": 1,
                "result": "data_returned",
                "response_rows": 1,
                "response_columns": ["LinearId", "Timestamp", "Value1"],
                "point_numbers_with_rows": [1],
                "unmatched_response_rows": 0,
            },
        )
        self.assertEqual(
            result["pv_power_integration_probe"],
            {
                "period": "previous_complete_day",
                "point_number": 1,
                "method": "trapezoidal_max_15_minute_gap",
                "sample_count": 3,
                "integrated_intervals": 2,
                "skipped_gaps": 0,
                "coverage_minutes": 10,
                "result": "calibrated",
                "relative_difference_percent": 0.0,
                "assessment": "good",
            },
        )
        self.assertNotIn("secret", serialized)
        self.assertNotIn("private-device", serialized)
        self.assertNotIn("private-point-id", serialized)
        self.assertNotIn("private-timestamp", serialized)
        self.assertNotIn("private-energy-value", serialized)
        self.assertNotIn("2026-08-03", serialized)
        self.assertNotIn('"1000"', serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("live", serialized)


if __name__ == "__main__":
    unittest.main()
