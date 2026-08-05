from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import importlib
import json
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).parents[1]

aiohttp = sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
if not hasattr(aiohttp, "ClientError"):
    aiohttp.ClientError = type("ClientError", (Exception,), {})

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

diagnostics = importlib.import_module("custom_components.smart1_ems.diagnostics")
bus_module = importlib.import_module("custom_components.smart1_ems.bus")
discovery_module = importlib.import_module("custom_components.smart1_ems.discovery")
interface_module = importlib.import_module("custom_components.smart1_ems.interface")
inverter_module = importlib.import_module("custom_components.smart1_ems.inverter")
module_field_module = importlib.import_module(
    "custom_components.smart1_ems.module_field"
)
point_module = importlib.import_module("custom_components.smart1_ems.point")


class Entry:
    entry_id = "entry-1"
    data = {"api_key": "secret", "device_id": "private-device"}
    options = {
        "energy_roles": {"grid_import": "private-point-id"},
    }


class Api:
    async def get_linear_cumulative_rows(self, *args, **kwargs):
        self.target_date = kwargs["target_date"]
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
                "coordinator": types.SimpleNamespace(
                    data={
                        "pv_strings": {
                            (2, 1, 1): inverter_module.Smart1PvStringSample(
                                bus=2,
                                address=1,
                                string_id=1,
                                timestamp="private-inverter-timestamp",
                                ac_power_w=100.0,
                                dc_power_w=110.0,
                                dc_voltage_v=500.0,
                                inverter_temperature_c=42.0,
                            )
                        }
                    }
                ),
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
                    has_pv=True,
                    inverter_count=1,
                ),
                "inverters": [
                    inverter_module.Smart1Inverter(
                        id="private-inverter-id",
                        bus=2,
                        address=1,
                        name="Energy Butler",
                        manufacturer="M-TEC",
                        model="E-SMART",
                        serial_number="private-serial-number",
                        string_count=1,
                        string_capacities_w=(5000.0,),
                        string_module_fields=("1",),
                        monitoring="on",
                        configured="ok",
                    )
                ],
                "module_fields": [
                    module_field_module.Smart1ModuleField(
                        id="private-module-field-id",
                        reference="1",
                        name="private-module-field-name",
                        tilt_degrees=23.0,
                        azimuth_degrees=65.0,
                        shadow_from="11:00:00",
                        shadow_until="13:00:00",
                    )
                ],
                "buses": [
                    bus_module.Smart1BusSystem(
                        id="Bus2",
                        number=2,
                        configured="ok",
                        documented_manufacturer_count=1,
                        manufacturers=("private-bus-manufacturer",),
                    )
                ],
                "bus_probe": {
                    "endpoint_result": "data_returned",
                    "response_status": 200,
                    "response_rows": 1,
                    "response_columns": [
                        "BusManufactor1",
                        "BusId",
                    ],
                    "rows_with_documented_bus_id": 1,
                    "rows_with_numeric_bus_id": 0,
                    "rows_with_other_bus_id": 0,
                    "rows_without_bus_id": 0,
                    "rows_with_configuration_status": 1,
                    "rows_with_manufacturer_count": 1,
                    "rows_with_manufacturer_protocols": 1,
                    "private_value": "private-bus-value",
                },
                "history_importers": [
                    types.SimpleNamespace(
                        diagnostic_status={
                            "type": "derived_energy_history",
                            "last_result": "completed",
                        }
                    )
                ],
            }
        }
    }


class DiagnosticsTest(unittest.TestCase):
    def test_returns_classification_metadata_without_credentials_or_values(
        self,
    ) -> None:
        result = asyncio.run(
            diagnostics.async_get_config_entry_diagnostics(Hass(), Entry())
        )
        serialized = json.dumps(result)

        self.assertEqual(result["points"][0]["current_category"], "pv")
        self.assertEqual(
            result["history_imports"],
            [
                {
                    "type": "derived_energy_history",
                    "last_result": "completed",
                }
            ],
        )
        self.assertEqual(
            result["configured_energy_roles"],
            {"grid_import": 1},
        )
        self.assertEqual(
            result["inverter_diagnostics"],
            {
                "inverter_count": 1,
                "strings_with_data": 1,
                "inverters": [
                    {
                        "inverter_number": 1,
                        "name": "Energy Butler",
                        "manufacturer": "M-TEC",
                        "model": "E-SMART",
                        "configured_strings": 1,
                        "strings_with_data": 1,
                        "available_metrics": [
                            "ac_power_w",
                            "dc_power_w",
                            "dc_voltage_v",
                            "inverter_temperature_c",
                        ],
                        "monitoring": "on",
                        "configured": "ok",
                        "serial_number_present": True,
                    }
                ],
            },
        )
        self.assertEqual(
            result["module_field_diagnostics"],
            {
                "module_field_count": 1,
                "with_installed_capacity": 1,
                "with_azimuth": 1,
                "with_tilt": 1,
                "with_shadow_interval": 1,
            },
        )
        self.assertEqual(
            result["bus_diagnostics"],
            {
                "configured_bus_count": 1,
                "with_manufacturer_information": 1,
                "manufacturer_protocol_counts": [1],
                "endpoint_result": "data_returned",
                "response_status": 200,
                "response_rows": 1,
                "response_columns": ["BusId", "BusManufactor1"],
                "rows_with_documented_bus_id": 1,
                "rows_with_numeric_bus_id": 0,
                "rows_with_other_bus_id": 0,
                "rows_without_bus_id": 0,
                "rows_with_configuration_status": 1,
                "rows_with_manufacturer_count": 1,
                "rows_with_manufacturer_protocols": 1,
            },
        )
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
        self.assertNotIn("private-inverter-id", serialized)
        self.assertNotIn("private-serial-number", serialized)
        self.assertNotIn("private-inverter-timestamp", serialized)
        self.assertNotIn("private-module-field-id", serialized)
        self.assertNotIn("private-module-field-name", serialized)
        self.assertNotIn("private-bus-manufacturer", serialized)
        self.assertNotIn("private-bus-value", serialized)
        self.assertNotIn("2026-08-03", serialized)
        self.assertNotIn('"1000"', serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("live", serialized)
        self.assertEqual(
            Hass.data["smart1_ems"]["entry-1"]["api"].target_date,
            date(2026, 8, 3),
        )


if __name__ == "__main__":
    unittest.main()
