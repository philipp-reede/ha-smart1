from __future__ import annotations

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
        self.assertEqual(
            entry.data[HISTORY_SCHEMA_VERSIONS_KEY][retained_id],
            4,
        )
        self.assertEqual(len(manager.calls), 2)


if __name__ == "__main__":
    unittest.main()
