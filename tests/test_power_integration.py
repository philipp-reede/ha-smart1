from __future__ import annotations

import importlib
from datetime import datetime, timezone
from itertools import permutations
from pathlib import Path
from random import Random
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

    def test_keeps_both_naive_fall_dst_hours_in_chronological_rows(
        self,
    ) -> None:
        rows = [
            row(f"2026-10-25 02:{minute:02d}:00", "1000")
            for _fold in range(2)
            for minute in range(0, 60, 5)
        ]
        rows.append(row("2026-10-25 03:00:00", "1000"))

        result = power_integration.integrate_power_rows(
            rows,
            "pv",
            ZoneInfo("Europe/Berlin"),
        )

        self.assertEqual(result.sample_count, 25)
        self.assertEqual(result.integrated_intervals, 24)
        self.assertEqual(result.skipped_gaps, 0)
        self.assertEqual(result.covered_seconds, 2 * 60 * 60)
        self.assertAlmostEqual(result.energy_kwh, 2.0)
        self.assertEqual(
            [item[0] for item in result.hourly_energy_kwh],
            [
                datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc),
            ],
        )

    def test_keeps_both_naive_fall_dst_hours_in_reverse_rows(self) -> None:
        rows = [
            row(f"2026-10-25 02:{minute:02d}:00", "1000")
            for _fold in range(2)
            for minute in range(0, 60, 5)
        ]
        rows.append(row("2026-10-25 03:00:00", "1000"))

        result = power_integration.integrate_power_rows(
            list(reversed(rows)),
            "pv",
            ZoneInfo("Europe/Berlin"),
        )

        self.assertEqual(result.sample_count, 25)
        self.assertEqual(result.integrated_intervals, 24)
        self.assertEqual(result.skipped_gaps, 0)
        self.assertEqual(result.covered_seconds, 2 * 60 * 60)
        self.assertAlmostEqual(result.energy_kwh, 2.0)
        self.assertEqual(
            [item[0] for item in result.hourly_energy_kwh],
            [
                datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc),
            ],
        )

    def test_keeps_interleaved_naive_fall_dst_rows_in_either_direction(
        self,
    ) -> None:
        rows = [
            row(f"2026-10-25 02:{minute:02d}:00", "1000")
            for minute in range(0, 60, 5)
            for _fold in range(2)
        ]
        rows.append(row("2026-10-25 03:00:00", "1000"))
        expected_hour_starts = [
            datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc),
        ]

        for descending, ordered_rows in (
            (False, rows),
            (True, list(reversed(rows))),
        ):
            with self.subTest(descending=descending):
                result = power_integration.integrate_power_rows(
                    ordered_rows,
                    "pv",
                    ZoneInfo("Europe/Berlin"),
                )

                self.assertEqual(result.sample_count, 25)
                self.assertEqual(result.integrated_intervals, 24)
                self.assertEqual(result.skipped_gaps, 0)
                self.assertEqual(result.covered_seconds, 2 * 60 * 60)
                self.assertAlmostEqual(result.energy_kwh, 2.0)
                self.assertEqual(
                    [item[0] for item in result.hourly_energy_kwh],
                    expected_hour_starts,
                )

    def test_exact_duplicates_do_not_invent_a_second_fold_hour(self) -> None:
        rows = [
            row("2026-10-25 02:00:00", "1000"),
            row("2026-10-25 02:05:00", "1000"),
        ]
        duplicated_rows = [
            duplicate
            for sample in rows
            for duplicate in (sample, dict(sample))
        ]

        expected = power_integration.integrate_power_rows(
            rows,
            "pv",
            ZoneInfo("Europe/Berlin"),
        )
        actual = power_integration.integrate_power_rows(
            duplicated_rows,
            "pv",
            ZoneInfo("Europe/Berlin"),
        )

        self.assertEqual(actual, expected)
        self.assertEqual(actual.sample_count, 2)
        self.assertEqual(actual.covered_seconds, 5 * 60)
        self.assertAlmostEqual(actual.energy_kwh, 1 / 12)

    def test_partial_fold_is_independent_of_row_order(self) -> None:
        rows = [
            row("2026-10-25 02:00:00", "100"),
            row("2026-10-25 02:05:00", "200"),
            row("2026-10-25 02:10:00", "300"),
            row("2026-10-25 02:10:00", "1600"),
        ]
        results = [
            power_integration.integrate_power_rows(
                list(ordered_rows),
                "pv",
                ZoneInfo("Europe/Berlin"),
            )
            for ordered_rows in permutations(rows)
        ]

        self.assertTrue(all(result == results[0] for result in results[1:]))
        self.assertEqual(results[0].sample_count, 4)
        self.assertEqual(results[0].integrated_intervals, 2)
        self.assertEqual(results[0].skipped_gaps, 1)
        self.assertEqual(results[0].covered_seconds, 10 * 60)

    def test_ambiguous_fold_profiles_are_order_and_duplicate_independent(
        self,
    ) -> None:
        first_fold = [
            row(
                f"2026-10-25 02:{minute:02d}:00",
                str(600 + minute * 10),
            )
            for minute in range(0, 60, 5)
        ]
        second_fold = [
            row(
                f"2026-10-25 02:{minute:02d}:00",
                str(1600 - minute * 5),
            )
            for minute in range(0, 60, 5)
        ]
        after_fold = row("2026-10-25 03:00:00", "1000")
        interleaved = [
            sample
            for pair in zip(first_fold, second_fold, strict=True)
            for sample in pair
        ]
        shuffled = list(interleaved)
        Random(42).shuffle(shuffled)
        duplicated = [
            duplicate
            for sample in interleaved
            for duplicate in (sample, dict(sample))
        ]
        unevenly_duplicated = [*interleaved]
        unevenly_duplicated.insert(13, dict(first_fold[6]))
        row_orders = (
            [*first_fold, *second_fold, after_fold],
            [*interleaved, after_fold],
            [*shuffled, after_fold],
            [*duplicated, after_fold],
            [*unevenly_duplicated, after_fold],
        )

        results = [
            power_integration.integrate_power_rows(
                ordered_rows,
                "pv",
                ZoneInfo("Europe/Berlin"),
            )
            for ordered_rows in row_orders
        ]
        expected = results[0]

        for index, result in enumerate(results):
            with self.subTest(order=index):
                self.assertEqual(result.sample_count, 25)
                self.assertEqual(result.integrated_intervals, 24)
                self.assertEqual(result.skipped_gaps, 0)
                self.assertEqual(result.covered_seconds, 2 * 60 * 60)
                self.assertAlmostEqual(result.energy_kwh, expected.energy_kwh)
                self.assertEqual(
                    [start for start, _energy in result.hourly_energy_kwh],
                    [
                        start
                        for start, _energy in expected.hourly_energy_kwh
                    ],
                )
                for (
                    _expected_start,
                    expected_energy,
                ), (_start, energy) in zip(
                    expected.hourly_energy_kwh,
                    result.hourly_energy_kwh,
                    strict=True,
                ):
                    self.assertAlmostEqual(energy, expected_energy)


if __name__ == "__main__":
    unittest.main()
