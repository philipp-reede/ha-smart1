from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).parents[1]

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

bus_module = importlib.import_module("custom_components.smart1_ems.bus")


class BusParserTest(unittest.TestCase):
    def test_response_diagnostics_classify_rows_without_values(self) -> None:
        diagnostics = bus_module.bus_response_diagnostics(
            [
                {
                    "BusId": "Bus1",
                    "BusConfigured": "No value",
                    "BusManufactors": "No value",
                    "BusManufactor1": "No value",
                },
                {
                    "BusId": "Bus2",
                    "BusConfigured": "ok",
                    "BusManufactors": "1",
                    "BusManufactor1": "private-manufacturer",
                },
                {"BusId": "3", "BusConfigured": "No value"},
                {"BusId": "private-other-id"},
                {"BusConfigured": "No value"},
            ]
        )

        self.assertEqual(
            diagnostics,
            {
                "rows_with_documented_bus_id": 2,
                "rows_with_numeric_bus_id": 1,
                "rows_with_other_bus_id": 1,
                "rows_without_bus_id": 1,
                "rows_with_configuration_status": 1,
                "rows_with_manufacturer_count": 1,
                "rows_with_manufacturer_protocols": 1,
            },
        )
        self.assertNotIn("private-manufacturer", str(diagnostics))
        self.assertNotIn("private-other-id", str(diagnostics))

    def test_documented_response_omits_empty_bus_slots(self) -> None:
        buses = bus_module.parse_bus_systems(
            [
                {
                    "BusId": "Bus1",
                    "BusConfigured": "No value",
                    "BusManufactors": "No value",
                    "BusManufactor1": "No value",
                },
                {
                    "BusId": "Bus2",
                    "BusConfigured": "ok",
                    "BusManufactors": "1",
                    "BusManufactor1": "sma",
                    "BusManufactor2": "No value",
                },
            ]
        )

        self.assertEqual(len(buses), 1)
        self.assertEqual(buses[0].id, "Bus2")
        self.assertEqual(buses[0].number, 2)
        self.assertEqual(buses[0].configured, "ok")
        self.assertEqual(buses[0].documented_manufacturer_count, 1)
        self.assertEqual(buses[0].manufacturers, ("sma",))

    def test_accepts_corrected_headers_and_deduplicates_protocols(self) -> None:
        buses = bus_module.parse_bus_systems(
            [
                {
                    "Bus Id": "Bus-3",
                    "BusConfigured": "configured",
                    "BusManufacturers": "2.0",
                    "BusManufacturer2": "M-TEC",
                    "BusManufacturer1": "M-TEC",
                }
            ]
        )

        self.assertEqual(buses[0].number, 3)
        self.assertEqual(buses[0].documented_manufacturer_count, 2)
        self.assertEqual(buses[0].manufacturers, ("M-TEC",))

    def test_keeps_configured_bus_without_manufacturer_details(self) -> None:
        buses = bus_module.parse_bus_systems(
            [
                {
                    "BusId": "Bus4",
                    "BusConfigured": "ok",
                    "BusManufactors": "0",
                },
                {
                    "BusId": "not-a-bus",
                    "BusConfigured": "ok",
                },
            ]
        )

        self.assertEqual([bus.number for bus in buses], [4])
        self.assertEqual(buses[0].manufacturers, ())


if __name__ == "__main__":
    unittest.main()
