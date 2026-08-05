from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "smart1_ems"
    / "inverter.py"
)
SPEC = importlib.util.spec_from_file_location("smart1_inverter", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
INVERTER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = INVERTER
SPEC.loader.exec_module(INVERTER)


class ParseInvertersTest(unittest.TestCase):
    def test_parses_documented_inverter_metadata(self) -> None:
        rows = [
            {
                "Inverter Id": "Inverter_B2_A1",
                "Name": "Energy Butler",
                "Manufactor": "M-TEC",
                "Type": "E-SMART",
                "Serial No": "serial-1",
                "Capacity": "11000",
                "Inverter Power": "10000",
                "Modulfield": "1",
                "Strings": "2",
                "String 1 Capacity": "5000",
                "String 1 Modulfield": "1",
                "String 2 Capacity": "6000",
                "String 2 Modulfield": "2",
                "Monitoring": "on",
                "Configured": "ok",
            }
        ]

        inverter = INVERTER.parse_inverters(rows)[0]

        self.assertEqual(inverter.id, "Inverter_B2_A1")
        self.assertEqual(inverter.key, (2, 1))
        self.assertEqual(inverter.string_ids, (1, 2))
        self.assertEqual(inverter.string_capacities_w, (5000.0, 6000.0))
        self.assertEqual(inverter.string_module_fields, ("1", "2"))
        self.assertEqual(inverter.serial_number, "serial-1")

    def test_infers_strings_from_documented_capacity_slots(self) -> None:
        rows = [
            {
                "InverterId": "Inverter_B1_A4",
                "Strings": "No value",
                "String 1 Capacity": "4500",
                "String 2 Capacity": "0",
                "String 3 Capacity": "4.500,0",
                "String 3 Modulfield": "3",
            }
        ]

        inverter = INVERTER.parse_inverters(rows)[0]

        self.assertEqual(inverter.string_count, 3)
        self.assertEqual(inverter.string_ids, (1, 2, 3))
        self.assertIsNone(inverter.string_capacities_w[2])

    def test_ignores_rows_without_documented_inverter_id(self) -> None:
        self.assertEqual(
            INVERTER.parse_inverters([{"Inverter Id": "unexpected"}]),
            [],
        )


class ParsePvStringSamplesTest(unittest.TestCase):
    def test_keeps_latest_row_for_each_string(self) -> None:
        rows = [
            {
                "Bus": "2",
                "Address": "1",
                "StringId": "1",
                "Timestamp": "2026-08-05 12:05:00",
                "Value 1": "1200,5",
                "Value 2": "1300",
                "Value 3": "501,2",
                "Value 4": "42,5",
            },
            {
                "Bus": "2",
                "Address": "1",
                "StringId": "1",
                "Timestamp": "2026-08-05 12:00:00",
                "Value 1": "900",
                "Value 2": "1000",
                "Value 3": "490",
                "Value 4": "40",
            },
            {
                "Bus": "2",
                "Address": "1",
                "StringId": "2",
                "Timestamp": "2026-08-05 12:05:00",
                "Value1": "No value",
                "Value2": "800",
                "Value3": "450",
                "Value4": "invalid",
            },
        ]

        samples = INVERTER.parse_latest_pv_string_samples(rows)

        self.assertEqual(set(samples), {(2, 1, 1), (2, 1, 2)})
        self.assertEqual(samples[(2, 1, 1)].ac_power_w, 1200.5)
        self.assertEqual(samples[(2, 1, 1)].dc_voltage_v, 501.2)
        self.assertIsNone(samples[(2, 1, 2)].ac_power_w)
        self.assertIsNone(samples[(2, 1, 2)].inverter_temperature_c)

    def test_ignores_incomplete_address_rows(self) -> None:
        rows = [
            {
                "Bus": "2",
                "StringId": "1",
                "Timestamp": "2026-08-05 12:05:00",
                "Value1": "1200",
            }
        ]

        self.assertEqual(INVERTER.parse_latest_pv_string_samples(rows), {})


if __name__ == "__main__":
    unittest.main()
