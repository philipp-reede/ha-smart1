from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
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
HISTORY_SOURCE_TIME_ZONES_KEY = history_state.HISTORY_SOURCE_TIME_ZONES_KEY
HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY = (
    history_state.HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY
)
Smart1HistoryState = history_state.Smart1HistoryState
recorded_source_time_zone = history_state.recorded_source_time_zone
scoped_statistic_id = history_state.scoped_statistic_id
source_time_zone_requires_audit = (
    history_state.source_time_zone_requires_audit
)
source_time_zone_requires_rebuild = (
    history_state.source_time_zone_requires_rebuild
)
statistics_namespace_for_device = history_state.statistics_namespace_for_device


class _ConfigEntries:
    def __init__(self) -> None:
        self.calls = []

    def async_update_entry(self, entry, **changes) -> None:
        self.calls.append((entry, changes))
        entry.data = dict(changes["data"])


class _DurableStore:
    def __init__(self, data=None) -> None:
        self.data = deepcopy(data)
        self.saves = []
        self.fail_saves = False
        self.ignore_saves = False

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data) -> None:
        if self.fail_saves:
            raise OSError("store unavailable")
        if self.ignore_saves:
            return
        self.data = deepcopy(data)
        self.saves.append(deepcopy(data))


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
                HISTORY_SOURCE_TIME_ZONES_KEY: {
                    removed_id: "Asia/Kathmandu",
                    retained_id: "Europe/Berlin",
                },
                HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY: {
                    removed_id: {
                        "source_time_zone": "Asia/Kathmandu",
                        "target_time_zone": "Europe/Berlin",
                        "generation": "pending-remove",
                    },
                    retained_id: {
                        "source_time_zone": "Europe/Berlin",
                        "target_time_zone": "UTC",
                        "generation": "pending-retain",
                    },
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
        self.assertNotIn(
            removed_id,
            entry.data[HISTORY_SOURCE_TIME_ZONES_KEY],
        )
        self.assertEqual(
            entry.data[HISTORY_SOURCE_TIME_ZONES_KEY][retained_id],
            "Europe/Berlin",
        )
        self.assertNotIn(
            removed_id,
            entry.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY],
        )
        self.assertEqual(
            entry.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY][retained_id][
                "generation"
            ],
            "pending-retain",
        )
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

    def test_source_timezone_is_committed_atomically_with_scan_state(
        self,
    ) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(data={})
        state = Smart1HistoryState(hass, entry)

        self.assertIsNone(state.source_time_zone(statistic_id))
        self.assertTrue(
            state.commit_scan_if_unchanged(
                statistic_id,
                5,
                has_data=True,
                expected_version=0,
                checked_through=date(2026, 9, 25),
                empty_days=set(),
                nonempty_days={date(2026, 9, 25)},
                oldest_supported=date(2025, 9, 26),
                source_time_zone="Asia/Kathmandu",
            )
        )
        self.assertEqual(
            state.source_time_zone(statistic_id),
            "Asia/Kathmandu",
        )
        self.assertEqual(
            entry.data[HISTORY_SOURCE_TIME_ZONES_KEY],
            {statistic_id: "Asia/Kathmandu"},
        )

        reloaded = Smart1HistoryState(hass, entry)
        self.assertEqual(
            reloaded.source_time_zone(statistic_id),
            "Asia/Kathmandu",
        )
        self.assertFalse(
            reloaded.commit_scan_if_unchanged(
                statistic_id,
                5,
                has_data=True,
                expected_version=0,
                checked_through=date(2026, 9, 25),
                empty_days=set(),
                nonempty_days=set(),
                oldest_supported=date(2025, 9, 26),
                source_time_zone="Europe/Berlin",
            )
        )
        self.assertEqual(
            reloaded.source_time_zone(statistic_id),
            "Asia/Kathmandu",
        )

    def test_pending_timezone_migration_is_idempotent_and_survives_reload(
        self,
    ) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(
            data={HISTORY_SOURCE_TIME_ZONES_KEY: {statistic_id: "UTC"}}
        )
        state = Smart1HistoryState(hass, entry)

        generation = state.begin_time_zone_migration(
            statistic_id,
            "UTC",
            "Europe/Berlin",
            fallback_daily_energy={},
            clear_rebuild_baseline_sum=0.0,
        )
        same_generation = state.begin_time_zone_migration(
            statistic_id,
            "UTC",
            "Europe/Berlin",
            fallback_daily_energy={},
            clear_rebuild_baseline_sum=0.0,
        )

        self.assertEqual(same_generation, generation)
        self.assertEqual(len(manager.calls), 1)
        self.assertEqual(state.source_time_zone(statistic_id), "UTC")
        self.assertEqual(
            state.effective_source_time_zone(statistic_id, "Etc/Unknown"),
            "Europe/Berlin",
        )
        self.assertEqual(
            entry.data[HISTORY_SOURCE_TIME_ZONES_KEY][statistic_id],
            "UTC",
        )

        reloaded = Smart1HistoryState(hass, entry)
        pending = reloaded.pending_time_zone_migration(statistic_id)
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.source_time_zone, "UTC")
        self.assertEqual(pending.target_time_zone, "Europe/Berlin")
        self.assertEqual(pending.generation, generation)
        self.assertEqual(
            recorded_source_time_zone(
                reloaded,
                statistic_id,
                "Etc/Unknown",
            ),
            "Europe/Berlin",
        )
        self.assertFalse(
            source_time_zone_requires_rebuild(
                reloaded,
                statistic_id,
                "Europe/Berlin",
            )
        )
        self.assertTrue(
            source_time_zone_requires_audit(
                reloaded,
                statistic_id,
                "Europe/Berlin",
            )
        )

    def test_identity_migration_requires_recoverable_baseline_anchor(
        self,
    ) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        state = Smart1HistoryState(
            types.SimpleNamespace(config_entries=manager),
            types.SimpleNamespace(data={}),
        )

        generation = state.begin_time_zone_migration(
            statistic_id,
            "Europe/Berlin",
            "Europe/Berlin",
            fallback_daily_energy={date(2026, 9, 27): 0.0},
            clear_rebuild_baseline_sum=100.0,
        )

        pending = state.pending_time_zone_migration(statistic_id)
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.generation, generation)
        self.assertEqual(pending.source_time_zone, "Europe/Berlin")
        self.assertEqual(pending.target_time_zone, "Europe/Berlin")
        self.assertEqual(
            pending.fallback_energy_by_date(),
            {date(2026, 9, 27): 0.0},
        )

        with self.assertRaisesRegex(ValueError, "requires an anchor row"):
            Smart1HistoryState(
                types.SimpleNamespace(config_entries=_ConfigEntries()),
                types.SimpleNamespace(data={}),
            ).begin_time_zone_migration(
                "smart1_ems:grid_import",
                "Europe/Berlin",
                "Europe/Berlin",
                fallback_daily_energy={},
                clear_rebuild_baseline_sum=100.0,
            )

    def test_exact_replacement_journal_round_trips_all_or_nothing(
        self,
    ) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        entry = types.SimpleNamespace(data={})
        first_start = datetime(2026, 9, 27, 10, tzinfo=timezone.utc)
        second_start = datetime(2026, 9, 27, 11, tzinfo=timezone.utc)
        state = Smart1HistoryState(
            types.SimpleNamespace(config_entries=manager),
            entry,
        )

        state.begin_time_zone_migration(
            statistic_id,
            "Europe/Berlin",
            "Europe/Berlin",
            fallback_daily_energy={date(2026, 9, 27): 3.0},
            clear_rebuild_baseline_sum=100.0,
            replacement_hourly_energy=[
                {"start": first_start, "state": 1.0},
                {"start": second_start, "state": 2.0},
            ],
        )

        stored = entry.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY][
            statistic_id
        ]["replacement_hourly_energy"]
        self.assertEqual(
            stored,
            [
                {"start": first_start.isoformat(), "state": 1.0},
                {"start": second_start.isoformat(), "state": 2.0},
            ],
        )
        reloaded = Smart1HistoryState(
            types.SimpleNamespace(config_entries=_ConfigEntries()),
            types.SimpleNamespace(data=entry.data),
        )
        pending = reloaded.pending_time_zone_migration(statistic_id)
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertTrue(pending.has_complete_replacement)
        self.assertEqual(
            pending.replacement_energy_by_start(),
            {first_start: 1.0, second_start: 2.0},
        )

        malformed_data = deepcopy(entry.data)
        malformed_data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY][
            statistic_id
        ]["replacement_hourly_energy"].append(
            {"start": "not-a-date", "state": 4.0}
        )
        malformed_entry = types.SimpleNamespace(data=malformed_data)
        malformed_state = Smart1HistoryState(
            types.SimpleNamespace(config_entries=_ConfigEntries()),
            malformed_entry,
        )
        malformed = malformed_state.pending_time_zone_migration(statistic_id)
        self.assertIsNotNone(malformed)
        assert malformed is not None
        self.assertFalse(malformed.has_complete_replacement)
        self.assertTrue(malformed.has_invalid_replacement)
        self.assertEqual(malformed.replacement_energy_by_start(), {})
        malformed_state.mark_complete(
            "smart1_ems:unrelated",
            1,
            has_data=False,
        )
        self.assertIsNone(
            malformed_entry.data[
                HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY
            ][statistic_id]["replacement_hourly_energy"]
        )

        absent_data = deepcopy(entry.data)
        del absent_data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY][
            statistic_id
        ]["replacement_hourly_energy"]
        absent = Smart1HistoryState(
            types.SimpleNamespace(config_entries=_ConfigEntries()),
            types.SimpleNamespace(data=absent_data),
        ).pending_time_zone_migration(statistic_id)
        self.assertIsNotNone(absent)
        assert absent is not None
        self.assertFalse(absent.has_complete_replacement)
        self.assertFalse(absent.has_invalid_replacement)

        malformed_fallback_data = deepcopy(absent_data)
        malformed_fallback_data[
            HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY
        ][statistic_id]["fallback_daily_energy"] = {
            "2026-09-27": 3.0,
            "2026-09-28": "bad",
        }
        malformed_fallback = Smart1HistoryState(
            types.SimpleNamespace(config_entries=_ConfigEntries()),
            types.SimpleNamespace(data=malformed_fallback_data),
        ).pending_time_zone_migration(statistic_id)
        self.assertIsNotNone(malformed_fallback)
        assert malformed_fallback is not None
        self.assertFalse(malformed_fallback.has_complete_fallback)
        self.assertEqual(malformed_fallback.fallback_energy_by_date(), {})

        empty_fallback_data = deepcopy(absent_data)
        empty_fallback_data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY][
            statistic_id
        ]["fallback_daily_energy"] = {}
        empty_fallback = Smart1HistoryState(
            types.SimpleNamespace(config_entries=_ConfigEntries()),
            types.SimpleNamespace(data=empty_fallback_data),
        ).pending_time_zone_migration(statistic_id)
        self.assertIsNotNone(empty_fallback)
        assert empty_fallback is not None
        self.assertTrue(empty_fallback.has_complete_fallback)
        self.assertEqual(empty_fallback.fallback_energy_by_date(), {})

        empty_data = deepcopy(entry.data)
        empty_data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY][statistic_id][
            "replacement_hourly_energy"
        ] = []
        empty = Smart1HistoryState(
            types.SimpleNamespace(config_entries=_ConfigEntries()),
            types.SimpleNamespace(data=empty_data),
        ).pending_time_zone_migration(statistic_id)
        self.assertIsNotNone(empty)
        assert empty is not None
        self.assertTrue(empty.has_complete_replacement)
        self.assertEqual(empty.replacement_energy_by_start(), {})

        exact_empty_state = Smart1HistoryState(
            types.SimpleNamespace(config_entries=_ConfigEntries()),
            types.SimpleNamespace(data={}),
        )
        exact_empty_state.begin_time_zone_migration(
            "smart1_ems:empty_replacement",
            "Europe/Berlin",
            "Europe/Berlin",
            fallback_daily_energy={},
            clear_rebuild_baseline_sum=100.0,
            replacement_hourly_energy=[],
        )
        exact_empty = exact_empty_state.pending_time_zone_migration(
            "smart1_ems:empty_replacement"
        )
        self.assertIsNotNone(exact_empty)
        assert exact_empty is not None
        self.assertTrue(exact_empty.has_complete_replacement)
        self.assertFalse(exact_empty.has_invalid_replacement)

        with self.assertRaisesRegex(
            ValueError,
            "Fallback daily energy must be complete",
        ):
            exact_empty_state.begin_time_zone_migration(
                "smart1_ems:invalid_fallback",
                "UTC",
                "Europe/Berlin",
                fallback_daily_energy={date(2026, 9, 27): "bad"},
                clear_rebuild_baseline_sum=0.0,
                replacement_hourly_energy=[],
            )

    def test_timezone_migration_commit_is_atomic_and_generation_guarded(
        self,
    ) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(
            data={HISTORY_SOURCE_TIME_ZONES_KEY: {statistic_id: "UTC"}}
        )
        state = Smart1HistoryState(hass, entry)
        generation = state.begin_time_zone_migration(
            statistic_id,
            "UTC",
            "Asia/Kathmandu",
            fallback_daily_energy={},
            clear_rebuild_baseline_sum=0.0,
        )
        stale_generation = "stale-generation"
        state = Smart1HistoryState(hass, entry)
        calls_before_commit = len(manager.calls)

        commit_kwargs = {
            "has_data": True,
            "expected_version": 0,
            "checked_through": date(2026, 9, 25),
            "empty_days": set(),
            "nonempty_days": {date(2026, 9, 25)},
            "oldest_supported": date(2025, 9, 26),
            "source_time_zone": "Asia/Kathmandu",
        }
        self.assertFalse(
            state.commit_scan_if_unchanged(
                statistic_id,
                5,
                **commit_kwargs,
                time_zone_migration_generation=stale_generation,
            )
        )
        self.assertEqual(len(manager.calls), calls_before_commit)
        self.assertEqual(state.current_schema_version(statistic_id), 0)
        self.assertEqual(state.source_time_zone(statistic_id), "UTC")
        self.assertFalse(
            state.commit_scan_if_unchanged(
                statistic_id,
                5,
                **{**commit_kwargs, "source_time_zone": "Europe/Berlin"},
                time_zone_migration_generation=generation,
            )
        )
        self.assertEqual(len(manager.calls), calls_before_commit)

        self.assertTrue(
            state.commit_scan_if_unchanged(
                statistic_id,
                5,
                **commit_kwargs,
                time_zone_migration_generation=generation,
            )
        )
        self.assertEqual(len(manager.calls), calls_before_commit + 1)
        self.assertEqual(
            state.source_time_zone(statistic_id),
            "Asia/Kathmandu",
        )
        self.assertIsNone(state.pending_time_zone_migration(statistic_id))
        self.assertNotIn(
            statistic_id,
            entry.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY],
        )

        reloaded = Smart1HistoryState(hass, entry)
        self.assertEqual(
            reloaded.source_time_zone(statistic_id),
            "Asia/Kathmandu",
        )
        self.assertIsNone(reloaded.pending_time_zone_migration(statistic_id))
        self.assertFalse(
            source_time_zone_requires_audit(
                reloaded,
                statistic_id,
                "Asia/Kathmandu",
            )
        )

    def test_exact_replay_commit_resets_existing_schema_progress(
        self,
    ) -> None:
        statistic_id = "smart1_ems:pv_production"
        schema_version = 8
        today = date(2026, 9, 28)
        old_empty_day = today - timedelta(days=30)
        manager = _ConfigEntries()
        entry = types.SimpleNamespace(
            data={
                HISTORY_SOURCE_TIME_ZONES_KEY: {
                    statistic_id: "Europe/Berlin"
                }
            }
        )
        state = Smart1HistoryState(
            types.SimpleNamespace(config_entries=manager),
            entry,
        )
        state.mark_complete(
            statistic_id,
            schema_version,
            has_data=True,
            checked_through=today,
        )
        state.record_empty_day_results(
            statistic_id,
            schema_version,
            empty_days={old_empty_day},
            nonempty_days=set(),
            oldest_supported=today - timedelta(days=364),
            retried_day=old_empty_day,
            checked_on=today,
        )
        generation = state.begin_time_zone_migration(
            statistic_id,
            "Europe/Berlin",
            "Europe/Berlin",
            fallback_daily_energy={today: 1.0},
            clear_rebuild_baseline_sum=100.0,
            replacement_hourly_energy=[
                {
                    "start": datetime(
                        2026,
                        9,
                        28,
                        10,
                        tzinfo=timezone.utc,
                    ),
                    "state": 1.0,
                }
            ],
        )

        self.assertTrue(
            state.commit_scan_if_unchanged(
                statistic_id,
                schema_version,
                has_data=True,
                expected_version=schema_version,
                checked_through=today,
                empty_days={today - timedelta(days=20)},
                nonempty_days={today - timedelta(days=10)},
                oldest_supported=today - timedelta(days=364),
                retried_day=today - timedelta(days=20),
                checked_on=today,
                source_time_zone="Europe/Berlin",
                time_zone_migration_generation=generation,
                reset_history_progress=True,
            )
        )

        self.assertIsNone(
            state.checked_through(statistic_id, schema_version)
        )
        self.assertNotIn(
            statistic_id,
            entry.data[HISTORY_EMPTY_DAYS_KEY],
        )
        self.assertIsNone(
            state.next_empty_retry_date(
                statistic_id,
                schema_version,
                oldest_supported=today - timedelta(days=364),
                before=today,
                today=today,
            )
        )
        self.assertIsNone(state.pending_time_zone_migration(statistic_id))

    def test_incomplete_pending_timezone_migration_is_retained_fail_safe(
        self,
    ) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        generation = "legacy-generation"
        entry = types.SimpleNamespace(
            data={
                HISTORY_SOURCE_TIME_ZONES_KEY: {statistic_id: "UTC"},
                HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY: {
                    statistic_id: {
                        "source_time_zone": "UTC",
                        "target_time_zone": "Europe/Berlin",
                        "generation": generation,
                    }
                },
            }
        )
        state = Smart1HistoryState(
            types.SimpleNamespace(config_entries=manager),
            entry,
        )
        pending = state.pending_time_zone_migration(statistic_id)
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertFalse(pending.has_complete_fallback)
        self.assertIsNone(pending.clear_rebuild_baseline_sum)

        new_generation = state.begin_time_zone_migration(
            statistic_id,
            "UTC",
            "Europe/Berlin",
            fallback_daily_energy={date(2026, 9, 24): 7.5},
            clear_rebuild_baseline_sum=12.25,
        )

        self.assertEqual(new_generation, generation)
        pending = state.pending_time_zone_migration(statistic_id)
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertTrue(pending.has_complete_fallback)
        self.assertEqual(
            pending.fallback_energy_by_date(),
            {date(2026, 9, 24): 7.5},
        )
        self.assertEqual(pending.clear_rebuild_baseline_sum, 12.25)
        self.assertEqual(
            entry.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY][
                statistic_id
            ]["fallback_daily_energy"],
            {"2026-09-24": 7.5},
        )

    def test_pending_timezone_migration_cannot_be_replaced(self) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(
            data={HISTORY_SOURCE_TIME_ZONES_KEY: {statistic_id: "UTC"}}
        )
        state = Smart1HistoryState(hass, entry)
        generation = state.begin_time_zone_migration(
            statistic_id,
            "UTC",
            "Pacific/Kiritimati",
            fallback_daily_energy={},
            clear_rebuild_baseline_sum=0.0,
        )
        calls_before_replacement = len(manager.calls)

        with self.assertRaisesRegex(
            RuntimeError,
            "Cannot replace an unfinished time-zone migration",
        ):
            state.begin_time_zone_migration(
                statistic_id,
                "Pacific/Kiritimati",
                "Etc/GMT+12",
                fallback_daily_energy={},
                clear_rebuild_baseline_sum=0.0,
            )

        self.assertEqual(len(manager.calls), calls_before_replacement)
        pending = state.pending_time_zone_migration(statistic_id)
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.generation, generation)
        self.assertEqual(pending.source_time_zone, "UTC")
        self.assertEqual(
            pending.target_time_zone,
            "Pacific/Kiritimati",
        )

    def test_timezone_migration_is_saved_to_durable_store(self) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(
            data={HISTORY_SOURCE_TIME_ZONES_KEY: {statistic_id: "UTC"}}
        )
        store = _DurableStore()
        state = Smart1HistoryState(hass, entry, durable_store=store)

        async def run() -> str:
            await state.async_initialize()
            return await state.async_begin_time_zone_migration(
                statistic_id,
                "UTC",
                "Europe/Berlin",
                fallback_daily_energy={date(2026, 9, 24): 4.25},
                clear_rebuild_baseline_sum=11.5,
            )

        generation = asyncio.run(run())

        self.assertGreaterEqual(len(store.saves), 2)
        stored_pending = store.data[
            HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY
        ][statistic_id]
        self.assertEqual(stored_pending["generation"], generation)
        self.assertEqual(
            stored_pending["fallback_daily_energy"],
            {"2026-09-24": 4.25},
        )
        self.assertEqual(stored_pending["clear_rebuild_baseline_sum"], 11.5)
        self.assertEqual(stored_pending["source_time_zone"], "UTC")
        self.assertEqual(
            stored_pending["target_time_zone"],
            "Europe/Berlin",
        )

    def test_initial_timezone_fingerprint_is_saved_to_durable_store(
        self,
    ) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(data={})
        store = _DurableStore()
        state = Smart1HistoryState(hass, entry, durable_store=store)

        async def run() -> bool:
            await state.async_initialize()
            return await state.async_commit_scan_if_unchanged(
                statistic_id,
                5,
                has_data=True,
                expected_version=0,
                checked_through=date(2026, 9, 25),
                empty_days=set(),
                nonempty_days={date(2026, 9, 25)},
                oldest_supported=date(2025, 9, 26),
                source_time_zone="Europe/Berlin",
            )

        self.assertTrue(asyncio.run(run()))
        self.assertEqual(
            store.data[HISTORY_SOURCE_TIME_ZONES_KEY][statistic_id],
            "Europe/Berlin",
        )
        self.assertEqual(
            store.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY],
            {},
        )

    def test_pending_migration_refreshes_durable_fallback_in_place(
        self,
    ) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        entry = types.SimpleNamespace(
            data={HISTORY_SOURCE_TIME_ZONES_KEY: {statistic_id: "UTC"}}
        )
        store = _DurableStore()
        state = Smart1HistoryState(
            types.SimpleNamespace(config_entries=manager),
            entry,
            durable_store=store,
        )

        async def run() -> tuple[str, str]:
            await state.async_initialize()
            first = await state.async_begin_time_zone_migration(
                statistic_id,
                "UTC",
                "Europe/Berlin",
                fallback_daily_energy={date(2026, 9, 24): 5.0},
                clear_rebuild_baseline_sum=10.0,
            )
            second = await state.async_begin_time_zone_migration(
                statistic_id,
                "UTC",
                "Europe/Berlin",
                fallback_daily_energy={date(2026, 9, 24): 7.0},
                clear_rebuild_baseline_sum=12.0,
            )
            return first, second

        first, second = asyncio.run(run())

        self.assertEqual(second, first)
        self.assertEqual(
            store.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY][
                statistic_id
            ]["fallback_daily_energy"],
            {"2026-09-24": 7.0},
        )
        self.assertEqual(
            store.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY][
                statistic_id
            ]["clear_rebuild_baseline_sum"],
            12.0,
        )

    def test_failed_pending_fallback_refresh_restores_previous_batch(
        self,
    ) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        entry = types.SimpleNamespace(
            data={HISTORY_SOURCE_TIME_ZONES_KEY: {statistic_id: "UTC"}}
        )
        store = _DurableStore()
        state = Smart1HistoryState(
            types.SimpleNamespace(config_entries=manager),
            entry,
            durable_store=store,
        )

        async def run() -> None:
            await state.async_initialize()
            await state.async_begin_time_zone_migration(
                statistic_id,
                "UTC",
                "Europe/Berlin",
                fallback_daily_energy={date(2026, 9, 24): 5.0},
                clear_rebuild_baseline_sum=10.0,
            )
            store.fail_saves = True
            with self.assertRaises(OSError):
                await state.async_begin_time_zone_migration(
                    statistic_id,
                    "UTC",
                    "Europe/Berlin",
                    fallback_daily_energy={date(2026, 9, 24): 7.0},
                    clear_rebuild_baseline_sum=12.0,
                )

        asyncio.run(run())

        pending = state.pending_time_zone_migration(statistic_id)
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(
            pending.fallback_energy_by_date(),
            {date(2026, 9, 24): 5.0},
        )
        self.assertEqual(pending.clear_rebuild_baseline_sum, 10.0)
        self.assertEqual(
            store.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY][
                statistic_id
            ]["fallback_daily_energy"],
            {"2026-09-24": 5.0},
        )

    def test_failed_durable_migration_commit_keeps_journal_pending(
        self,
    ) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(
            data={HISTORY_SOURCE_TIME_ZONES_KEY: {statistic_id: "UTC"}}
        )
        store = _DurableStore()
        state = Smart1HistoryState(hass, entry, durable_store=store)

        async def run() -> tuple[str, bool]:
            await state.async_initialize()
            generation = await state.async_begin_time_zone_migration(
                statistic_id,
                "UTC",
                "Europe/Berlin",
                fallback_daily_energy={},
                clear_rebuild_baseline_sum=0.0,
            )
            store.fail_saves = True
            committed = await state.async_commit_scan_if_unchanged(
                statistic_id,
                5,
                has_data=True,
                expected_version=0,
                checked_through=date(2026, 9, 25),
                empty_days=set(),
                nonempty_days={date(2026, 9, 25)},
                oldest_supported=date(2025, 9, 26),
                source_time_zone="Europe/Berlin",
                time_zone_migration_generation=generation,
            )
            return generation, committed

        generation, committed = asyncio.run(run())

        self.assertFalse(committed)
        self.assertEqual(state.source_time_zone(statistic_id), "UTC")
        self.assertEqual(state.current_schema_version(statistic_id), 0)
        self.assertIsNone(state.data_presence(statistic_id, 5))
        self.assertIsNone(state.checked_through(statistic_id, 5))
        pending = state.pending_time_zone_migration(statistic_id)
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.generation, generation)
        self.assertEqual(
            store.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY][
                statistic_id
            ]["generation"],
            generation,
        )

        store.fail_saves = False
        self.assertTrue(
            asyncio.run(
                state.async_commit_scan_if_unchanged(
                    statistic_id,
                    5,
                    has_data=True,
                    expected_version=0,
                    checked_through=date(2026, 9, 25),
                    empty_days=set(),
                    nonempty_days={date(2026, 9, 25)},
                    oldest_supported=date(2025, 9, 26),
                    source_time_zone="Europe/Berlin",
                    time_zone_migration_generation=generation,
                )
            )
        )
        self.assertEqual(state.current_schema_version(statistic_id), 5)
        self.assertEqual(state.source_time_zone(statistic_id), "Europe/Berlin")
        self.assertIsNone(state.pending_time_zone_migration(statistic_id))

    def test_silently_failed_journal_write_is_detected_before_import(
        self,
    ) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(
            data={HISTORY_SOURCE_TIME_ZONES_KEY: {statistic_id: "UTC"}}
        )
        store = _DurableStore()
        state = Smart1HistoryState(hass, entry, durable_store=store)

        async def run() -> None:
            await state.async_initialize()
            store.ignore_saves = True
            with self.assertRaisesRegex(
                OSError,
                "journal was not persisted",
            ):
                await state.async_begin_time_zone_migration(
                    statistic_id,
                    "UTC",
                    "Europe/Berlin",
                    fallback_daily_energy={},
                    clear_rebuild_baseline_sum=0.0,
                )

        asyncio.run(run())

        self.assertEqual(
            store.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY],
            {},
        )
        self.assertIsNone(state.pending_time_zone_migration(statistic_id))
        self.assertEqual(
            entry.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY],
            {},
        )

        async def retry() -> str:
            store.ignore_saves = False
            return await state.async_begin_time_zone_migration(
                statistic_id,
                "UTC",
                "Europe/Berlin",
                fallback_daily_energy={},
                clear_rebuild_baseline_sum=0.0,
            )

        generation = asyncio.run(retry())
        self.assertEqual(
            store.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY][
                statistic_id
            ]["generation"],
            generation,
        )

    def test_stopping_home_assistant_cannot_begin_durable_migration(
        self,
    ) -> None:
        statistic_id = "smart1_ems:pv_production"
        for core_state in ("STOPPING", "FINAL_WRITE", "STOPPED"):
            with self.subTest(core_state=core_state):
                manager = _ConfigEntries()
                hass = types.SimpleNamespace(
                    config_entries=manager,
                    state=types.SimpleNamespace(value=core_state),
                )
                entry = types.SimpleNamespace(
                    data={
                        HISTORY_SOURCE_TIME_ZONES_KEY: {
                            statistic_id: "UTC"
                        }
                    }
                )
                store = _DurableStore(
                    {
                        HISTORY_SOURCE_TIME_ZONES_KEY: {
                            statistic_id: "UTC"
                        },
                        HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY: {},
                    }
                )
                state = Smart1HistoryState(
                    hass,
                    entry,
                    durable_store=store,
                )

                async def run() -> None:
                    await state.async_initialize()
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "while stopping",
                    ):
                        await state.async_begin_time_zone_migration(
                            statistic_id,
                            "UTC",
                            "Europe/Berlin",
                            fallback_daily_energy={},
                            clear_rebuild_baseline_sum=0.0,
                        )

                asyncio.run(run())

                self.assertIsNone(
                    state.pending_time_zone_migration(statistic_id)
                )
                self.assertEqual(
                    store.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY],
                    {},
                )

    def test_durable_completed_timezone_wins_over_stale_config_entry(
        self,
    ) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        stale_pending = {
            statistic_id: {
                "source_time_zone": "UTC",
                "target_time_zone": "Europe/Berlin",
                "generation": "old-generation",
            }
        }
        entry = types.SimpleNamespace(
            data={
                HISTORY_SOURCE_TIME_ZONES_KEY: {statistic_id: "UTC"},
                HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY: stale_pending,
            }
        )
        store = _DurableStore(
            {
                HISTORY_SOURCE_TIME_ZONES_KEY: {
                    statistic_id: "Europe/Berlin"
                },
                HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY: {},
            }
        )
        state = Smart1HistoryState(hass, entry, durable_store=store)

        asyncio.run(state.async_initialize())

        self.assertEqual(
            state.source_time_zone(statistic_id),
            "Europe/Berlin",
        )
        self.assertIsNone(state.pending_time_zone_migration(statistic_id))
        self.assertEqual(
            entry.data[HISTORY_SOURCE_TIME_ZONES_KEY][statistic_id],
            "Europe/Berlin",
        )
        self.assertEqual(
            entry.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY],
            {},
        )

    def test_durable_store_removal_wins_over_stale_config_fingerprint(
        self,
    ) -> None:
        removed_id = "smart1_ems:pv_production"
        retained_id = "smart1_ems:grid_import"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(
            data={
                HISTORY_SOURCE_TIME_ZONES_KEY: {
                    removed_id: "UTC",
                    retained_id: "Europe/Berlin",
                }
            }
        )
        store = _DurableStore(
            {
                HISTORY_SOURCE_TIME_ZONES_KEY: {
                    retained_id: "Europe/Berlin"
                },
                HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY: {},
            }
        )
        state = Smart1HistoryState(hass, entry, durable_store=store)

        asyncio.run(state.async_initialize())

        self.assertIsNone(state.source_time_zone(removed_id))
        self.assertEqual(
            state.source_time_zone(retained_id),
            "Europe/Berlin",
        )
        self.assertNotIn(
            removed_id,
            entry.data[HISTORY_SOURCE_TIME_ZONES_KEY],
        )

    def test_forget_statistics_updates_durable_timezone_store(self) -> None:
        removed_id = "smart1_ems:pv_production"
        retained_id = "smart1_ems:grid_import"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(
            data={
                HISTORY_SOURCE_TIME_ZONES_KEY: {
                    removed_id: "UTC",
                    retained_id: "Europe/Berlin",
                },
                HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY: {
                    removed_id: {
                        "source_time_zone": "UTC",
                        "target_time_zone": "Europe/Berlin",
                        "generation": "remove-me",
                    }
                },
            }
        )
        store = _DurableStore()
        state = Smart1HistoryState(hass, entry, durable_store=store)

        async def run() -> None:
            await state.async_initialize()
            await state.async_forget_statistics({removed_id})

        asyncio.run(run())

        self.assertNotIn(
            removed_id,
            store.data[HISTORY_SOURCE_TIME_ZONES_KEY],
        )
        self.assertNotIn(
            removed_id,
            store.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY],
        )
        self.assertEqual(
            store.data[HISTORY_SOURCE_TIME_ZONES_KEY][retained_id],
            "Europe/Berlin",
        )

    def test_pending_timezone_is_not_committed_without_generation(self) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(
            data={HISTORY_SOURCE_TIME_ZONES_KEY: {statistic_id: "UTC"}}
        )
        state = Smart1HistoryState(hass, entry)
        state.begin_time_zone_migration(
            statistic_id,
            "UTC",
            "Europe/Berlin",
            fallback_daily_energy={},
            clear_rebuild_baseline_sum=0.0,
        )

        self.assertTrue(
            state.commit_scan_if_unchanged(
                statistic_id,
                5,
                has_data=True,
                expected_version=0,
                checked_through=date(2026, 9, 25),
                empty_days=set(),
                nonempty_days=set(),
                oldest_supported=date(2025, 9, 26),
                source_time_zone="Europe/Berlin",
            )
        )
        self.assertEqual(state.source_time_zone(statistic_id), "UTC")
        self.assertIsNotNone(state.pending_time_zone_migration(statistic_id))

    def test_timezone_migration_clear_rejects_stale_generation(self) -> None:
        statistic_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(data={})
        state = Smart1HistoryState(hass, entry)
        generation = state.begin_time_zone_migration(
            statistic_id,
            "UTC",
            "Europe/Berlin",
            fallback_daily_energy={},
            clear_rebuild_baseline_sum=0.0,
        )
        calls_before_clear = len(manager.calls)

        self.assertFalse(
            state.clear_time_zone_migration(statistic_id, "stale")
        )
        self.assertEqual(len(manager.calls), calls_before_clear)
        self.assertTrue(
            state.clear_time_zone_migration(statistic_id, generation)
        )
        self.assertEqual(len(manager.calls), calls_before_clear + 1)
        self.assertIsNone(state.pending_time_zone_migration(statistic_id))

    def test_malformed_pending_migrations_ignore_only_invalid_entries(
        self,
    ) -> None:
        valid_id = "smart1_ems:pv_production"
        manager = _ConfigEntries()
        hass = types.SimpleNamespace(config_entries=manager)
        entry = types.SimpleNamespace(
            data={
                HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY: {
                    valid_id: {
                        "source_time_zone": "UTC",
                        "target_time_zone": "Europe/Berlin",
                        "generation": "valid-generation",
                    },
                    "missing_generation": {
                        "source_time_zone": "UTC",
                        "target_time_zone": "Europe/Berlin",
                    },
                    "identity_layout": {
                        "source_time_zone": "UTC",
                        "target_time_zone": "UTC",
                        "generation": "identity-generation",
                    },
                    "not_a_mapping": "invalid",
                    42: {
                        "source_time_zone": "UTC",
                        "target_time_zone": "Europe/Berlin",
                        "generation": "invalid",
                    },
                }
            }
        )

        state = Smart1HistoryState(hass, entry)

        self.assertEqual(
            state.pending_time_zone_migration(valid_id).generation,
            "valid-generation",
        )
        self.assertIsNone(
            state.pending_time_zone_migration("missing_generation")
        )
        self.assertEqual(
            state.pending_time_zone_migration(
                "identity_layout"
            ).generation,
            "identity-generation",
        )
        state.mark_complete(valid_id, 1, has_data=False)
        self.assertEqual(
            set(entry.data[HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY]),
            {valid_id, "identity_layout"},
        )

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
