from __future__ import annotations

import importlib
from datetime import datetime, timezone
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
        self.assertEqual(len(result.hourly_energy_kwh), 1)
        self.assertEqual(
            result.hourly_energy_kwh[0][0],
            datetime(2026, 8, 2, 22, 0, tzinfo=timezone.utc),
        )
        self.assertAlmostEqual(result.hourly_energy_kwh[0][1], 0.075)

    def test_splits_a_trapezoid_at_the_utc_hour_boundary(self) -> None:
        result = power_integration.integrate_power_rows(
            [
                row("2026-08-03T00:55:00+00:00", "0"),
                row("2026-08-03T01:05:00+00:00", "1200"),
            ],
            "pv",
            ZoneInfo("Europe/Berlin"),
        )

        self.assertAlmostEqual(result.energy_kwh, 0.1)
        self.assertEqual(
            [item[0] for item in result.hourly_energy_kwh],
            [
                datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 3, 1, 0, tzinfo=timezone.utc),
            ],
        )
        self.assertAlmostEqual(result.hourly_energy_kwh[0][1], 0.025)
        self.assertAlmostEqual(result.hourly_energy_kwh[1][1], 0.075)

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
        self.assertEqual(result.hourly_energy_kwh, ())

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
        self.assertEqual(
            result.hourly_energy_kwh[0][0],
            datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
