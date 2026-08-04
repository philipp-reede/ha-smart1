from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "smart1_csv"
    / "pv.py"
)
SPEC = importlib.util.spec_from_file_location("smart1_pv", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
PV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PV)


class ParsePvCumulativeEnergyTest(unittest.TestCase):
    def test_documented_first_string_response(self) -> None:
        rows = [
            {
                "Bus": "2",
                "Address": "1",
                "StringId": "1",
                "Value1": "19265",
            },
            {
                "Bus": "2",
                "Address": "1",
                "StringId": "2",
                "Value1": "0",
            },
        ]

        self.assertEqual(PV.parse_pv_cumulative_energy(rows), 19.265)

    def test_sums_inverters_without_counting_strings_twice(self) -> None:
        rows = [
            {
                "Bus": "1",
                "Address": "1",
                "StringId": "1",
                "Value 1": "12500",
            },
            {
                "Bus": "1",
                "Address": "1",
                "StringId": "2",
                "Value 1": "12500",
            },
            {
                "Bus": "2",
                "Address": "4",
                "StringId": "1",
                "Value 1": "7,5",
            },
        ]

        self.assertEqual(PV.parse_pv_cumulative_energy(rows), 12.5075)

    def test_uses_largest_value_for_repeated_inverter_rows(self) -> None:
        rows = [
            {"Bus": "2", "Address": "1", "Value1": "1000"},
            {"Bus": "2", "Address": "1", "Value1": "1500"},
        ]

        self.assertEqual(PV.parse_pv_cumulative_energy(rows), 1.5)

    def test_returns_none_without_valid_values(self) -> None:
        rows = [
            {"Bus": "2", "Address": "1", "Value1": "No value"},
            {"Bus": "2", "Address": "2", "Value1": "invalid"},
            {"Bus": "2", "Address": "3", "Value1": "-1"},
        ]

        self.assertIsNone(PV.parse_pv_cumulative_energy(rows))


if __name__ == "__main__":
    unittest.main()
