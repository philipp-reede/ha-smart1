from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).parents[1]

aiohttp = sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
aiohttp.ClientError = type("ClientError", (Exception,), {})

homeassistant = sys.modules.setdefault(
    "homeassistant",
    types.ModuleType("homeassistant"),
)
homeassistant.__path__ = []

components = sys.modules.setdefault(
    "homeassistant.components",
    types.ModuleType("homeassistant.components"),
)
components.__path__ = []

recorder = types.ModuleType("homeassistant.components.recorder")
recorder.get_instance = lambda hass: None
recorder.__path__ = []
sys.modules["homeassistant.components.recorder"] = recorder


class StatisticData(dict):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)


class StatisticMetaData(dict):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)


models = types.ModuleType("homeassistant.components.recorder.models")
models.StatisticData = StatisticData
models.StatisticMetaData = StatisticMetaData
models.StatisticMeanType = types.SimpleNamespace(NONE="none")
sys.modules["homeassistant.components.recorder.models"] = models

statistics = types.ModuleType("homeassistant.components.recorder.statistics")
statistics.async_add_external_statistics = lambda *args, **kwargs: None
statistics.get_last_statistics = lambda *args, **kwargs: {}
sys.modules["homeassistant.components.recorder.statistics"] = statistics

const = types.ModuleType("homeassistant.const")
const.UnitOfEnergy = types.SimpleNamespace(KILO_WATT_HOUR="kWh")
sys.modules["homeassistant.const"] = const

core = sys.modules.setdefault(
    "homeassistant.core",
    types.ModuleType("homeassistant.core"),
)
core.HomeAssistant = object

util = sys.modules.setdefault(
    "homeassistant.util",
    types.ModuleType("homeassistant.util"),
)
util.__path__ = []
unit_conversion = types.ModuleType("homeassistant.util.unit_conversion")
unit_conversion.EnergyConverter = types.SimpleNamespace(UNIT_CLASS="energy")
sys.modules["homeassistant.util.unit_conversion"] = unit_conversion

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

history = importlib.import_module("custom_components.smart1_ems.history")
derived_history = importlib.import_module(
    "custom_components.smart1_ems.derived_history"
)


class Smart1HistoryTest(unittest.TestCase):
    def test_initial_window_contains_365_days(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")

        start, baseline = history.determine_import_window(
            [],
            date(2026, 8, 4),
            local_tz,
        )

        self.assertEqual(start, date(2025, 8, 5))
        self.assertEqual(baseline, 0.0)

    def test_refresh_uses_sum_before_recent_window(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        records = [
            {
                "start": datetime(
                    2026,
                    8,
                    1,
                    tzinfo=local_tz,
                ).timestamp(),
                "sum": 42.5,
            },
            {
                "start": datetime(
                    2026,
                    8,
                    2,
                    tzinfo=local_tz,
                ).timestamp(),
                "sum": 47.0,
            },
        ]

        start, baseline = history.determine_import_window(
            records,
            date(2026, 8, 4),
            local_tz,
        )

        self.assertEqual(start, date(2026, 8, 2))
        self.assertEqual(baseline, 42.5)

    def test_current_day_pv_refresh_uses_previous_day_baseline(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        records = [
            {
                "start": datetime(2026, 8, 3, tzinfo=local_tz).timestamp(),
                "sum": 42.5,
            },
            {
                "start": datetime(2026, 8, 4, tzinfo=local_tz).timestamp(),
                "sum": 47.0,
            },
        ]

        start, baseline = history.determine_import_window(
            records,
            date(2026, 8, 4),
            local_tz,
            1,
        )

        self.assertEqual(start, date(2026, 8, 4))
        self.assertEqual(baseline, 42.5)

    def test_statistics_are_cumulative_and_hour_aligned(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")

        result = history.build_daily_energy_statistics(
            [
                (date(2026, 3, 29), 4.25),
                (date(2026, 3, 30), 5.75),
            ],
            10.0,
            local_tz,
        )

        self.assertEqual([record["state"] for record in result], [4.25, 5.75])
        self.assertEqual([record["sum"] for record in result], [14.25, 20.0])
        self.assertEqual(result[0]["start"].tzinfo, local_tz)
        self.assertEqual(result[0]["start"].minute, 0)
        self.assertEqual(
            result[0]["start"].astimezone(timezone.utc).minute,
            0,
        )
        self.assertEqual(
            (
                result[1]["start"].astimezone(timezone.utc)
                - result[0]["start"].astimezone(timezone.utc)
            ).total_seconds(),
            23 * 60 * 60,
        )

    def test_refresh_preserves_existing_value_when_portal_has_no_data(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        records = [
            {
                "start": datetime(2026, 8, 2, tzinfo=local_tz).timestamp(),
                "state": 3.0,
                "sum": 20.0,
            },
            {
                "start": datetime(2026, 8, 3, tzinfo=local_tz).timestamp(),
                "state": 4.0,
                "sum": 24.0,
            },
        ]

        result = history.merge_daily_energy(
            records,
            [(date(2026, 8, 3), 4.5), (date(2026, 8, 4), 2.0)],
            date(2026, 8, 2),
            date(2026, 8, 4),
            local_tz,
        )

        self.assertEqual(
            result,
            [
                (date(2026, 8, 2), 3.0),
                (date(2026, 8, 3), 4.5),
                (date(2026, 8, 4), 2.0),
            ],
        )

    def test_derived_import_fetches_all_selected_points_together(self) -> None:
        class FakeApi:
            def __init__(self) -> None:
                self.calls = []

            async def get_linear_detailed_rows(
                self,
                linear_ids,
                *,
                target_date,
                missing_ok,
            ):
                self.calls.append((linear_ids, target_date, missing_ok))
                return [
                    {
                        "LinearId": linear_id,
                        "Timestamp": timestamp,
                        "Value1": "1000",
                    }
                    for linear_id in linear_ids
                    for timestamp in ("2026-08-03 00:00", "2026-08-03 00:05")
                ]

        api = FakeApi()
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(),
            api,
            {
                "grid_import": types.SimpleNamespace(id="grid"),
                "wallbox_consumption": types.SimpleNamespace(id="wallbox"),
            },
        )

        result, completed = asyncio.run(
            importer._fetch_hourly_energy(
                date(2026, 8, 3),
                date(2026, 8, 3),
                ZoneInfo("Europe/Berlin"),
            )
        )

        self.assertEqual(
            api.calls,
            [(["grid", "wallbox"], date(2026, 8, 3), True)],
        )
        self.assertTrue(completed)
        self.assertEqual(
            result["grid_import"][0][0],
            datetime(2026, 8, 2, 22, 0, tzinfo=timezone.utc),
        )
        self.assertAlmostEqual(result["grid_import"][0][1], 1 / 12)
        self.assertAlmostEqual(
            result["wallbox_consumption"][0][1],
            1 / 12,
        )

    def test_derived_import_retries_a_transient_daily_failure(self) -> None:
        class RetryApi:
            def __init__(self) -> None:
                self.calls = 0

            async def get_linear_detailed_rows(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise aiohttp.ClientError("temporary")
                return [
                    {
                        "LinearId": "grid",
                        "Timestamp": "2026-08-03 00:00",
                        "Value1": "1000",
                    },
                    {
                        "LinearId": "grid",
                        "Timestamp": "2026-08-03 00:05",
                        "Value1": "1000",
                    },
                ]

        api = RetryApi()
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(),
            api,
            {"grid_import": types.SimpleNamespace(id="grid")},
        )

        with patch.object(
            derived_history.asyncio,
            "sleep",
            new=AsyncMock(),
        ) as sleep:
            result, completed = asyncio.run(
                importer._fetch_hourly_energy(
                    date(2026, 8, 3),
                    date(2026, 8, 3),
                    ZoneInfo("Europe/Berlin"),
                )
            )

        self.assertTrue(completed)
        self.assertEqual(api.calls, 2)
        sleep.assert_awaited_once_with(1)
        self.assertTrue(result["grid_import"])

    def test_daily_derived_history_triggers_full_hourly_migration(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        records = [
            {
                "start": datetime(2026, 8, day, tzinfo=local_tz).timestamp(),
                "state": float(day),
                "sum": float(day * 10),
            }
            for day in (1, 2, 3)
        ]

        start, baseline = derived_history.determine_hourly_import_window(
            records,
            date(2026, 8, 4),
            local_tz,
        )

        self.assertEqual(start, date(2025, 8, 5))
        self.assertEqual(baseline, 0.0)
        self.assertTrue(
            derived_history.needs_hourly_rebuild(records, local_tz)
        )

    def test_hourly_derived_history_refreshes_three_days(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        records = [
            {
                "start": datetime(
                    2026,
                    8,
                    1,
                    hour,
                    tzinfo=local_tz,
                ).timestamp(),
                "state": 1.0,
                "sum": 40.0 + hour,
            }
            for hour in (22, 23)
        ]

        start, baseline = derived_history.determine_hourly_import_window(
            records,
            date(2026, 8, 4),
            local_tz,
        )

        self.assertEqual(start, date(2026, 8, 2))
        self.assertEqual(baseline, 63.0)
        self.assertFalse(
            derived_history.needs_hourly_rebuild(records, local_tz)
        )

    def test_hourly_current_day_refresh_uses_previous_sum(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        records = [
            {
                "start": datetime(
                    2026,
                    8,
                    3,
                    23,
                    tzinfo=local_tz,
                ).timestamp(),
                "state": 1.0,
                "sum": 63.0,
            }
        ]

        start, baseline = derived_history.determine_hourly_import_window(
            records,
            date(2026, 8, 4),
            local_tz,
            1,
        )

        self.assertEqual(start, date(2026, 8, 4))
        self.assertEqual(baseline, 63.0)

    def test_decreasing_hourly_sum_triggers_full_rebuild(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        records = [
            {
                "start": datetime(2026, 8, 3, hour, tzinfo=local_tz).timestamp(),
                "state": 1.0,
                "sum": total,
            }
            for hour, total in ((0, 100.0), (1, 2.0), (2, 3.0))
        ]

        self.assertTrue(
            derived_history.needs_hourly_rebuild(records, local_tz)
        )
        self.assertEqual(
            derived_history.determine_hourly_import_window(
                records,
                date(2026, 8, 4),
                local_tz,
            ),
            (date(2025, 8, 5), 0.0),
        )

    def test_rebuild_clears_only_selected_derived_statistics(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        daily_record = {
            "start": datetime(2026, 8, 3, tzinfo=local_tz).timestamp(),
            "state": 5.0,
            "sum": 20.0,
        }
        hour_start = datetime(2026, 8, 3, tzinfo=local_tz).astimezone(
            timezone.utc
        )
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/Berlin")
        )
        importer = derived_history.Smart1DerivedEnergyImporter(
            hass,
            types.SimpleNamespace(),
            {
                "grid_import": types.SimpleNamespace(
                    id="grid",
                    name="Bezug",
                ),
                "wallbox_consumption": types.SimpleNamespace(
                    id="wallbox",
                    name="ECar Laden",
                ),
            },
        )
        importer._existing_statistics = AsyncMock(
            return_value=[daily_record]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=(
                {
                    "grid_import": [(hour_start, 0.25)],
                    "wallbox_consumption": [(hour_start, 0.75)],
                },
                True,
            )
        )
        fake_recorder = types.SimpleNamespace(
            async_clear_statistics=Mock(),
            async_block_till_done=AsyncMock(),
        )

        with (
            patch.object(
                derived_history,
                "get_instance",
                return_value=fake_recorder,
            ),
            patch.object(
                derived_history,
                "async_add_external_statistics",
            ) as add_statistics,
        ):
            asyncio.run(importer.async_import())

        expected_ids = sorted(
            [
                derived_history.statistic_id_for_role("grid_import", "grid"),
                derived_history.statistic_id_for_role(
                    "wallbox_consumption",
                    "wallbox",
                ),
            ]
        )
        fake_recorder.async_clear_statistics.assert_called_once_with(
            expected_ids
        )
        self.assertEqual(fake_recorder.async_block_till_done.await_count, 2)
        self.assertEqual(add_statistics.call_count, 2)
        self.assertNotIn("smart1_ems:pv_production", expected_ids)
        for call in add_statistics.call_args_list:
            self.assertEqual(len(call.kwargs["statistics"]), 1)
            self.assertNotEqual(
                call.kwargs["statistics"][0]["state"],
                daily_record["state"],
            )

    def test_incomplete_fetch_does_not_clear_existing_statistics(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        daily_record = {
            "start": datetime(2026, 8, 3, tzinfo=local_tz).timestamp(),
            "state": 5.0,
            "sum": 20.0,
        }
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/Berlin")
        )
        importer = derived_history.Smart1DerivedEnergyImporter(
            hass,
            types.SimpleNamespace(),
            {
                "grid_import": types.SimpleNamespace(
                    id="grid",
                    name="Bezug",
                )
            },
        )
        importer._existing_statistics = AsyncMock(
            return_value=[daily_record]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=({"grid_import": []}, False)
        )
        fake_recorder = types.SimpleNamespace(
            async_clear_statistics=Mock(),
            async_block_till_done=AsyncMock(),
        )

        with (
            patch.object(
                derived_history,
                "get_instance",
                return_value=fake_recorder,
            ),
            patch.object(
                derived_history,
                "async_add_external_statistics",
            ) as add_statistics,
        ):
            asyncio.run(importer.async_import())

        fake_recorder.async_clear_statistics.assert_not_called()
        fake_recorder.async_block_till_done.assert_not_awaited()
        add_statistics.assert_not_called()

    def test_missing_role_does_not_block_other_statistic_repair(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        daily_record = {
            "start": datetime(2026, 8, 3, tzinfo=local_tz).timestamp(),
            "state": 5.0,
            "sum": 20.0,
        }
        hour_start = datetime(2026, 8, 3, tzinfo=local_tz).astimezone(
            timezone.utc
        )
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/Berlin")
        )
        importer = derived_history.Smart1DerivedEnergyImporter(
            hass,
            types.SimpleNamespace(),
            {
                "grid_import": types.SimpleNamespace(
                    id="grid",
                    name="Bezug",
                ),
                "wallbox_consumption": types.SimpleNamespace(
                    id="wallbox",
                    name="ECar Laden",
                ),
            },
        )
        importer._existing_statistics = AsyncMock(
            return_value=[daily_record]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=(
                {
                    "grid_import": [(hour_start, 0.25)],
                    "wallbox_consumption": [],
                },
                True,
            )
        )
        fake_recorder = types.SimpleNamespace(
            async_clear_statistics=Mock(),
            async_block_till_done=AsyncMock(),
        )

        with (
            patch.object(
                derived_history,
                "get_instance",
                return_value=fake_recorder,
            ),
            patch.object(
                derived_history,
                "async_add_external_statistics",
            ) as add_statistics,
        ):
            with self.assertLogs(derived_history._LOGGER, level="WARNING"):
                asyncio.run(importer.async_import())

        fake_recorder.async_clear_statistics.assert_called_once_with(
            [derived_history.statistic_id_for_role("grid_import", "grid")]
        )
        self.assertEqual(add_statistics.call_count, 1)
        self.assertEqual(
            importer.diagnostic_status["roles_without_replacement"],
            ["wallbox_consumption"],
        )
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "completed_with_preserved_roles",
        )

    def test_current_day_refresh_waits_for_pending_repair(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        daily_record = {
            "start": datetime(2026, 8, 3, tzinfo=local_tz).timestamp(),
            "state": 5.0,
            "sum": 20.0,
        }
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/Berlin")
        )
        importer = derived_history.Smart1DerivedEnergyImporter(
            hass,
            types.SimpleNamespace(),
            {
                "grid_import": types.SimpleNamespace(
                    id="grid",
                    name="Bezug",
                )
            },
        )
        importer._existing_statistics = AsyncMock(
            return_value=[daily_record]
        )
        importer._fetch_hourly_energy = AsyncMock()

        asyncio.run(importer.async_import(1, repair=False))

        importer._fetch_hourly_energy.assert_not_awaited()
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "repair_pending",
        )

    def test_hourly_merge_replaces_old_daily_midnight_record(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        midnight = datetime(2026, 8, 3, tzinfo=local_tz).astimezone(
            timezone.utc
        )
        one_am = datetime(2026, 8, 3, 1, tzinfo=local_tz).astimezone(
            timezone.utc
        )

        result = derived_history.merge_hourly_energy(
            [{"start": midnight.timestamp(), "state": 5.0, "sum": 20.0}],
            [(midnight, 0.25), (one_am, 0.75)],
            date(2026, 8, 3),
            date(2026, 8, 3),
            local_tz,
        )

        self.assertEqual(result, [(midnight, 0.25), (one_am, 0.75)])

    def test_hourly_statistics_keep_cumulative_sum(self) -> None:
        starts = [
            datetime(2026, 8, 3, hour, tzinfo=timezone.utc)
            for hour in (0, 1)
        ]

        result = derived_history.build_hourly_energy_statistics(
            [(starts[0], 0.25), (starts[1], 0.75)],
            10.0,
        )

        self.assertEqual([record["start"] for record in result], starts)
        self.assertEqual([record["state"] for record in result], [0.25, 0.75])
        self.assertEqual([record["sum"] for record in result], [10.25, 11.0])


if __name__ == "__main__":
    unittest.main()
