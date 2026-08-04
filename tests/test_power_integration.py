from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest
from zoneinfo import ZoneInfo


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

power_integration = importlib.import_module(
    "custom_components.smart1_ems.power_integration"
)


def row(timestamp: str, value: str, linear_id: str = "pv") -> dict[str, str]:
    return {
        "LinearId": linear_id,
        "Timestamp": timestamp,
        "Value1": value,
    }


class PowerIntegrationTest(unittest.TestCase):
    def test_integrates_trapezoids_in_kwh(self) -> None:
        result = power_integration.integrate_power_rows(
            [
                row("2026-08-03 00:00:00", "0"),
                row("2026-08-03 00:05:00", "600"),
                row("2026-08-03 00:10:00", "600"),
            ],
            "pv",
            ZoneInfo("Europe/Berlin"),
        )

        self.assertAlmostEqual(result.energy_kwh, 0.075)
        self.assertEqual(result.sample_count, 3)
        self.assertEqual(result.integrated_intervals, 2)
        self.assertEqual(result.covered_seconds, 600)

    def test_does_not_bridge_large_gaps(self) -> None:
        result = power_integration.integrate_power_rows(
            [
                row("2026-08-03 00:00:00", "1000"),
                row("2026-08-03 01:00:00", "1000"),
                row("invalid", "private"),
            ],
            "pv",
            ZoneInfo("Europe/Berlin"),
        )

        self.assertEqual(result.energy_kwh, 0)
        self.assertEqual(result.sample_count, 2)
        self.assertEqual(result.integrated_intervals, 0)
        self.assertEqual(result.skipped_gaps, 1)

    def test_normalizes_spring_dst_timestamps_before_integration(self) -> None:
        result = power_integration.integrate_power_rows(
            [
                row("2026-03-29 01:55:00", "1000"),
                row("2026-03-29 03:00:00", "1000"),
            ],
            "pv",
            ZoneInfo("Europe/Berlin"),
        )

        self.assertAlmostEqual(result.energy_kwh, 1 / 12)
        self.assertEqual(result.integrated_intervals, 1)
        self.assertEqual(result.skipped_gaps, 0)


if __name__ == "__main__":
    unittest.main()
