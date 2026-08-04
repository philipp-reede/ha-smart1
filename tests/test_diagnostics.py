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
smart1_csv = sys.modules.setdefault(
    "custom_components.smart1_csv",
    types.ModuleType("custom_components.smart1_csv"),
)
smart1_csv.__path__ = [str(ROOT / "custom_components" / "smart1_csv")]

diagnostics = importlib.import_module("custom_components.smart1_csv.diagnostics")
discovery_module = importlib.import_module("custom_components.smart1_csv.discovery")
interface_module = importlib.import_module("custom_components.smart1_csv.interface")
point_module = importlib.import_module("custom_components.smart1_csv.point")


class Entry:
    entry_id = "entry-1"
    data = {"api_key": "secret", "device_id": "private-device"}


class Hass:
    data = {
        "smart1_csv": {
            "entry-1": {
                "devices": [
                    point_module.Smart1Point(
                        id="private-point-id",
                        name="WP Leistung",
                        type="power",
                        source="sensor",
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
        self.assertNotIn("secret", serialized)
        self.assertNotIn("private-device", serialized)
        self.assertNotIn("private-point-id", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("live", serialized)


if __name__ == "__main__":
    unittest.main()
