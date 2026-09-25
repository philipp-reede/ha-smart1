from __future__ import annotations

from datetime import date, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import types
import unittest


ROOT = Path(__file__).parents[1]
SPEC = spec_from_file_location(
    "smart1_history_state_test_module",
    ROOT / "custom_components" / "smart1_ems" / "history_state.py",
)
assert SPEC is not None and SPEC.loader is not None
history_state = module_from_spec(SPEC)
SPEC.loader.exec_module(history_state)

HISTORY_SCHEMA_VERSIONS_KEY = history_state.HISTORY_SCHEMA_VERSIONS_KEY
HISTORY_DATA_PRESENCE_KEY = history_state.HISTORY_DATA_PRESENCE_KEY
HISTORY_COVERAGE_KEY = history_state.HISTORY_COVERAGE_KEY
HISTORY_EMPTY_DAYS_KEY = history_state.HISTORY_EMPTY_DAYS_KEY
HISTORY_EMPTY_RETRY_CURSORS_KEY = (
    history_state.HISTORY_EMPTY_RETRY_CURSORS_KEY
)
HISTORY_EMPTY_RETRY_RUNS_KEY = history_state.HISTORY_EMPTY_RETRY_RUNS_KEY
Smart1HistoryState = history_state.Smart1HistoryState
scoped_statistic_id = history_state.scoped_statistic_id
statistics_namespace_for_device = history_state.statistics_namespace_for_device


class _ConfigEntries:
    def __init__(self) -> None:
        self.calls = []

    def async_update_entry(self, entry, **changes) -> None:
        self.calls.append((entry, changes))
        entry.data = dict(changes["data"])


class Smart1HistoryStateTest(unittest.TestCase):
    def test_installation_namespace_is_stable_and_non_identifying(self) -> None:
        first = statistics_namespace_for_device(" plant-1 ")

        self.assertEqual(first, "8aac131a20")
        self.assertEqual(first, statistics_namespace_for_device("plant-1"))
        self.assertNotEqual(first, statistics_namespace_for_device("plant-2"))
        self.assertNotIn("plant", first)

    def test_scoped_id_preserves_empty_legacy_namespace(self) -> None:
        statistic_id = "smart1_ems:pv_production"

        self.assertEqual(scoped_statistic_id(statistic_id, ""), statistic_id)
        self.assertEqual(
            scoped_statistic_id(statistic_id, "abc123"),
            "smart1_ems:pv_production_abc123",
        )

    def test_completion_is_persisted_once_per_schema(self) -> None:
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(
            data={"api_key": "secret", "device_id": "plant-1"}
        )
        state = Smart1HistoryState(hass, entry)

        self.assertFalse(state.is_complete("smart1_ems:pv", 2))
        state.mark_complete("smart1_ems:pv", 2, has_data=False)
        state.mark_complete("smart1_ems:pv", 2, has_data=False)

        self.assertTrue(state.is_complete("smart1_ems:pv", 2))
        self.assertTrue(state.is_current_schema("smart1_ems:pv", 2))
        self.assertEqual(len(manager.calls), 1)
        self.assertEqual(
            entry.data[HISTORY_SCHEMA_VERSIONS_KEY],
            {"smart1_ems:pv": 2},
        )
        self.assertEqual(
            entry.data[HISTORY_DATA_PRESENCE_KEY],
            {"smart1_ems:pv": {"2": False}},
        )
        self.assertIs(
            state.data_presence("smart1_ems:pv", 2),
            False,
        )
        self.assertEqual(entry.data["api_key"], "secret")

    def test_schema_switch_preserves_presence_per_schema_version(self) -> None:
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(data={})
        state = Smart1HistoryState(hass, entry)

        state.mark_complete("smart1_ems:pv", 3, has_data=True)
        state.mark_complete("smart1_ems:pv", 2, has_data=False)

        self.assertFalse(state.is_complete("smart1_ems:pv", 3))
        self.assertTrue(state.is_current_schema("smart1_ems:pv", 2))
        self.assertIs(state.data_presence("smart1_ems:pv", 3), None)
        self.assertIs(state.data_presence("smart1_ems:pv", 2), False)
        self.assertEqual(
            entry.data[HISTORY_SCHEMA_VERSIONS_KEY],
            {"smart1_ems:pv": 2},
        )
        self.assertEqual(
            entry.data[HISTORY_DATA_PRESENCE_KEY],
            {"smart1_ems:pv": {"2": False, "3": True}},
        )
        self.assertEqual(len(manager.calls), 2)

        state.mark_complete("smart1_ems:pv", 3, has_data=True)
        self.assertTrue(state.is_current_schema("smart1_ems:pv", 3))
        self.assertIs(state.data_presence("smart1_ems:pv", 3), True)
        self.assertIs(state.data_presence("smart1_ems:pv", 2), False)
        self.assertEqual(len(manager.calls), 3)

        reloaded_state = Smart1HistoryState(hass, entry)
        self.assertTrue(
            reloaded_state.is_current_schema("smart1_ems:pv", 3)
        )
        self.assertIs(
            reloaded_state.data_presence("smart1_ems:pv", 3),
            True,
        )
        self.assertIs(
            reloaded_state.data_presence("smart1_ems:pv", 2),
            False,
        )

        state.mark_complete("smart1_ems:pv", 2, has_data=False)
        self.assertTrue(state.is_current_schema("smart1_ems:pv", 2))
        self.assertEqual(len(manager.calls), 4)

    def test_checked_through_is_persisted_per_active_schema(self) -> None:
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(data={})
        state = Smart1HistoryState(hass, entry)
        statistic_id = "smart1_ems:pv_production"

        state.mark_complete(
            statistic_id,
            5,
            has_data=True,
            checked_through=date(2026, 8, 25),
        )
        self.assertEqual(
            state.checked_through(statistic_id, 5),
            date(2026, 8, 25),
        )
        self.assertEqual(
            entry.data[HISTORY_COVERAGE_KEY],
            {statistic_id: {"5": "2026-08-25"}},
        )

        self.assertTrue(
            state.mark_checked_through(
                statistic_id,
                5,
                date(2026, 9, 25),
            )
        )
        self.assertEqual(
            state.checked_through(statistic_id, 5),
            date(2026, 9, 25),
        )
        self.assertTrue(
            state.mark_checked_through(
                statistic_id,
                5,
                date(2026, 9, 1),
            )
        )
        state.mark_complete(
            statistic_id,
            5,
            has_data=True,
            checked_through=date(2026, 9, 2),
        )
        self.assertEqual(
            state.checked_through(statistic_id, 5),
            date(2026, 9, 25),
        )
        self.assertEqual(len(manager.calls), 2)

        state.mark_complete(statistic_id, 6, has_data=False)
        self.assertIsNone(state.checked_through(statistic_id, 6))
        self.assertIsNone(state.checked_through(statistic_id, 5))

        reloaded = Smart1HistoryState(hass, entry)
        self.assertIsNone(reloaded.checked_through(statistic_id, 6))

    def test_malformed_checked_through_dates_are_ignored(self) -> None:
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        statistic_id = "smart1_ems:pv_production"
        entry = types.SimpleNamespace(
            data={
                HISTORY_SCHEMA_VERSIONS_KEY: {statistic_id: 5},
                HISTORY_COVERAGE_KEY: {
                    statistic_id: {
                        "5": "not-a-date",
                        "4": "2026-08-25",
                    }
                },
            }
        )

        state = Smart1HistoryState(hass, entry)

        self.assertIsNone(state.checked_through(statistic_id, 5))

    def test_empty_retry_queue_is_bounded_rotating_and_once_daily(self) -> None:
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(data={})
        state = Smart1HistoryState(hass, entry)
        statistic_id = "smart1_ems:pv_production"
        schema_version = 5
        today = date(2026, 9, 25)
        oldest_supported = date(2025, 9, 26)
        empty_days = {
            oldest_supported + timedelta(days=offset)
            for offset in range(365)
        }
        expired = oldest_supported - timedelta(days=1)

        state.mark_complete(
            statistic_id,
            schema_version,
            has_data=False,
            checked_through=today,
        )
        self.assertTrue(
            state.record_empty_day_results(
                statistic_id,
                schema_version,
                empty_days=empty_days | {expired},
                nonempty_days=set(),
                oldest_supported=oldest_supported,
            )
        )
        first = state.next_empty_retry_date(
            statistic_id,
            schema_version,
            oldest_supported=oldest_supported,
            before=today - timedelta(days=2),
            today=today,
        )
        self.assertEqual(first, oldest_supported)
        self.assertEqual(
            len(entry.data[HISTORY_EMPTY_DAYS_KEY][statistic_id]["5"]),
            365,
        )

        self.assertTrue(
            state.record_empty_day_results(
                statistic_id,
                schema_version,
                empty_days={first},
                nonempty_days=set(),
                oldest_supported=oldest_supported,
                retried_day=first,
                checked_on=today,
            )
        )
        self.assertIsNone(
            state.next_empty_retry_date(
                statistic_id,
                schema_version,
                oldest_supported=oldest_supported,
                before=today - timedelta(days=2),
                today=today,
            )
        )
        self.assertEqual(
            entry.data[HISTORY_EMPTY_RETRY_CURSORS_KEY][statistic_id]["5"],
            first.isoformat(),
        )
        self.assertEqual(
            entry.data[HISTORY_EMPTY_RETRY_RUNS_KEY][statistic_id]["5"],
            today.isoformat(),
        )

        reloaded = Smart1HistoryState(hass, entry)
        second = reloaded.next_empty_retry_date(
            statistic_id,
            schema_version,
            oldest_supported=oldest_supported,
            before=today - timedelta(days=2),
            today=today + timedelta(days=1),
        )
        self.assertEqual(second, oldest_supported + timedelta(days=1))

        # A non-empty result is removed only when the successful result is
        # explicitly committed (Recorder callers do that after readback).
        self.assertIn(
            second.isoformat(),
            entry.data[HISTORY_EMPTY_DAYS_KEY][statistic_id]["5"],
        )
        self.assertTrue(
            reloaded.record_empty_day_results(
                statistic_id,
                schema_version,
                empty_days=set(),
                nonempty_days={second},
                oldest_supported=oldest_supported,
                retried_day=second,
                checked_on=today + timedelta(days=1),
            )
        )
        self.assertNotIn(
            second.isoformat(),
            entry.data[HISTORY_EMPTY_DAYS_KEY][statistic_id]["5"],
        )

    def test_forgetting_statistics_prevents_completion_state_resurrection(
        self,
    ) -> None:
        removed_id = "smart1_ems:grid_import_deadbeef"
        retained_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(
            data={
                HISTORY_SCHEMA_VERSIONS_KEY: {
                    removed_id: 1,
                    retained_id: 4,
                },
                HISTORY_DATA_PRESENCE_KEY: {
                    removed_id: {"1": False},
                    retained_id: {"4": True},
                },
                HISTORY_COVERAGE_KEY: {
                    removed_id: {"1": "2026-08-25"},
                    retained_id: {"4": "2026-09-25"},
                },
            }
        )
        state = Smart1HistoryState(hass, entry)

        state.forget_statistics({removed_id})
        state.mark_complete("smart1_ems:battery_charge_cafebabe", 1, has_data=True)

        self.assertNotIn(
            removed_id,
            entry.data[HISTORY_SCHEMA_VERSIONS_KEY],
        )
        self.assertNotIn(
            removed_id,
            entry.data[HISTORY_DATA_PRESENCE_KEY],
        )
        self.assertNotIn(removed_id, entry.data[HISTORY_COVERAGE_KEY])
        self.assertEqual(
            entry.data[HISTORY_SCHEMA_VERSIONS_KEY][retained_id],
            4,
        )
        self.assertEqual(len(manager.calls), 2)

    def test_late_completion_cannot_mutate_deactivated_state(self) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(data={})
        stale_state = Smart1HistoryState(hass, entry)
        expected_version = stale_state.current_schema_version(statistic_id)

        stale_state.deactivate()
        active_state = Smart1HistoryState(hass, entry)
        active_state.mark_complete(statistic_id, 2, has_data=False)

        self.assertFalse(
            stale_state.mark_complete_if_unchanged(
                statistic_id,
                4,
                has_data=True,
                expected_version=expected_version,
            )
        )
        stale_state.mark_complete(statistic_id, 4, has_data=True)
        stale_state.forget_statistics({statistic_id})
        self.assertEqual(
            entry.data[HISTORY_SCHEMA_VERSIONS_KEY][statistic_id],
            2,
        )
        self.assertEqual(len(manager.calls), 1)

    def test_late_completion_requires_unchanged_generation(self) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(data={})
        state = Smart1HistoryState(hass, entry)
        expected_version = state.current_schema_version(statistic_id)

        self.assertTrue(
            state.mark_complete_if_unchanged(
                statistic_id,
                4,
                has_data=True,
                expected_version=expected_version,
            )
        )
        self.assertFalse(
            state.mark_complete_if_unchanged(
                statistic_id,
                2,
                has_data=False,
                expected_version=expected_version,
            )
        )
        self.assertEqual(
            entry.data[HISTORY_SCHEMA_VERSIONS_KEY][statistic_id],
            4,
        )
        self.assertEqual(len(manager.calls), 1)


if __name__ == "__main__":
    unittest.main()
