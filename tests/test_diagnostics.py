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


class Hass:
    data = {
        "smart1_ems": {
            "entry-1": {
                "api": Api(),
                "devices": [
                    point_module.Smart1Point(
                        id="private-point-id",
                        name="WP Leistung",
                        type="Energy",
                        source="counter",
                        hardware="meter",
                        interface="modbus:1_2_heatpump_3:power",
                        parsed_interface=interface_module.parse_interface(
                            "modbus:1_2_heatpump_3:power"
                        ),
                    )
                ],
                "discovery": discovery_module.Smart1DiscoveryResult(
                    has_heat_pump=True
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

        self.assertEqual(result["points"][0]["current_category"], "heat_pump")
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
        self.assertNotIn("secret", serialized)
        self.assertNotIn("private-device", serialized)
        self.assertNotIn("private-point-id", serialized)
        self.assertNotIn("private-timestamp", serialized)
        self.assertNotIn("private-energy-value", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("live", serialized)


if __name__ == "__main__":
    unittest.main()
