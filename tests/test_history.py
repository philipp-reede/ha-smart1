from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import ANY, AsyncMock, Mock, call, patch
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


def _complete_statistics_clear(_statistic_ids, *, on_done) -> None:
    """Complete a mocked Recorder clear operation."""
    on_done()


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
recorder_helpers = importlib.import_module(
    "custom_components.smart1_ems.recorder_helpers"
)


class _HistoryState:
    def __init__(self) -> None:
        self.completed: dict[str, int] = {}
        self.has_data: dict[tuple[str, int], bool] = {}

    def is_complete(self, statistic_id: str, schema_version: int) -> bool:
        return self.completed.get(statistic_id, 0) >= schema_version

    def is_current_schema(
        self,
        statistic_id: str,
        schema_version: int,
    ) -> bool:
        return self.completed.get(statistic_id, 0) == schema_version

    def current_schema_version(self, statistic_id: str) -> int:
        return self.completed.get(statistic_id, 0)

    def data_presence(
        self,
        statistic_id: str,
        schema_version: int,
    ) -> bool | None:
        if not self.is_complete(statistic_id, schema_version):
            return None
        return self.has_data.get((statistic_id, schema_version))

    def mark_complete(
        self,
        statistic_id: str,
        schema_version: int,
        *,
        has_data: bool,
    ) -> None:
        self.completed[statistic_id] = schema_version
        self.has_data[(statistic_id, schema_version)] = has_data

    def mark_complete_if_unchanged(
        self,
        statistic_id: str,
        schema_version: int,
        *,
        has_data: bool,
        expected_version: int,
    ) -> bool:
        if self.completed.get(statistic_id, 0) != expected_version:
            return False
        self.mark_complete(
            statistic_id,
            schema_version,
            has_data=has_data,
        )
        return True


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

    def test_current_day_pv_refresh_waits_for_initial_backfill(self) -> None:
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/Berlin")
        )
        importer = history.Smart1PvHistoryImporter(
            hass,
            types.SimpleNamespace(),
        )
        importer._existing_statistics = AsyncMock(return_value=[])
        importer._fetch_daily_energy = AsyncMock()

        asyncio.run(importer.async_import(1, repair=False))

        importer._existing_statistics.assert_awaited_once_with(
            2 * history.MAX_HOURLY_RECORDS_PER_DAY
        )
        importer._fetch_daily_energy.assert_not_awaited()
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "repair_pending",
        )

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

    def test_daily_pv_total_requires_hourly_migration(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        records = [
            {
                "start": datetime(2026, 8, 3, tzinfo=local_tz).timestamp(),
                "state": 116.71,
                "sum": 500.0,
            }
        ]

        self.assertTrue(
            history.needs_hourly_pv_migration(records, local_tz)
        )
        self.assertEqual(
            history.determine_hourly_pv_import_window(
                records,
                date(2026, 8, 5),
                local_tz,
            ),
            (date(2025, 8, 6), 0.0),
        )

    def test_hourly_pv_migration_keeps_pre_window_baseline(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = date(2026, 8, 5)
        full_start = today - history.timedelta(
            days=history.HISTORY_DAYS - 1
        )
        predecessor = {
            "start": datetime.combine(
                full_start - history.timedelta(days=1),
                datetime.min.time().replace(hour=23),
                tzinfo=local_tz,
            ).timestamp(),
            "state": 5.0,
            "sum": 500.0,
        }

        for force_full_refresh, midnight_state in (
            (False, 10.0),
            (True, 0.0),
        ):
            with self.subTest(force_full_refresh=force_full_refresh):
                midnight = {
                    "start": datetime.combine(
                        today - history.timedelta(days=1),
                        datetime.min.time(),
                        tzinfo=local_tz,
                    ).timestamp(),
                    "state": midnight_state,
                    "sum": 500.0 + midnight_state,
                }
                self.assertEqual(
                    history.determine_hourly_pv_import_window(
                        [predecessor, midnight],
                        today,
                        local_tz,
                        force_full_refresh=force_full_refresh,
                    ),
                    (full_start, 500.0),
                )

    def test_exact_pv_total_is_scaled_over_measured_hours(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        midnight = datetime(2026, 8, 3, tzinfo=local_tz).astimezone(
            timezone.utc
        )
        noon = datetime(2026, 8, 3, 12, tzinfo=local_tz).astimezone(
            timezone.utc
        )
        one_pm = datetime(2026, 8, 3, 13, tzinfo=local_tz).astimezone(
            timezone.utc
        )
        integration = history.PowerIntegrationResult(
            energy_kwh=10.0,
            hourly_energy_kwh=((noon, 4.0), (one_pm, 6.0)),
            sample_count=3,
            integrated_intervals=2,
            skipped_gaps=0,
            covered_seconds=7200,
        )

        result, distributed = history.distribute_exact_pv_energy(
            date(2026, 8, 3),
            116.71,
            integration,
            local_tz,
        )

        self.assertTrue(distributed)
        self.assertEqual(result[0], (midnight, 0.0))
        self.assertAlmostEqual(sum(value for _start, value in result), 116.71)
        self.assertAlmostEqual(dict(result)[noon], 116.71 * 0.4)
        self.assertAlmostEqual(dict(result)[one_pm], 116.71 * 0.6)

    def test_pv_distribution_reuses_existing_exact_daily_total(self) -> None:
        class PvApi:
            def __init__(self) -> None:
                self.detailed_calls = 0

            async def get_pv_cumulative_energy(self, *args, **kwargs):
                raise AssertionError("existing exact total should be reused")

            async def get_linear_detailed_rows(self, *args, **kwargs):
                self.detailed_calls += 1
                return [
                    {
                        "LinearId": "pv",
                        "Timestamp": "2026-08-03 12:00",
                        "Value1": "1000",
                    },
                    {
                        "LinearId": "pv",
                        "Timestamp": "2026-08-03 12:05",
                        "Value1": "1000",
                    },
                ]

        api = PvApi()
        importer = history.Smart1PvHistoryImporter(
            types.SimpleNamespace(),
            api,
            types.SimpleNamespace(id="pv"),
        )

        result, completed, distributed_days, fallback_days = asyncio.run(
            importer._fetch_hourly_energy(
                date(2026, 8, 3),
                date(2026, 8, 3),
                ZoneInfo("Europe/Berlin"),
                {date(2026, 8, 3): 116.71},
            )
        )

        self.assertTrue(completed)
        self.assertEqual(api.detailed_calls, 1)
        self.assertEqual(distributed_days, 1)
        self.assertEqual(fallback_days, 0)
        self.assertAlmostEqual(sum(value for _start, value in result), 116.71)

    def test_daily_pv_fetch_reports_failure_after_retries(self) -> None:
        api_key = "fake-api-key-must-not-leak"

        class FailingApi:
            def __init__(self) -> None:
                self.calls = 0

            async def get_pv_cumulative_energy(self, *args, **kwargs):
                self.calls += 1
                raise aiohttp.ClientError(
                    "Request failed for "
                    f"https://example.test/?apikey={api_key}"
                )

        api = FailingApi()
        importer = history.Smart1PvHistoryImporter(
            types.SimpleNamespace(),
            api,
        )

        with (
            patch.object(
                history.asyncio,
                "sleep",
                new=AsyncMock(),
            ) as sleep,
            self.assertLogs(
                history._LOGGER,
                level="WARNING",
            ) as captured,
        ):
            result, completed = asyncio.run(
                importer._fetch_daily_energy(
                    date(2026, 8, 3),
                    date(2026, 8, 3),
                )
            )

        self.assertEqual(result, [])
        self.assertFalse(completed)
        self.assertEqual(api.calls, history.PV_FETCH_ATTEMPTS)
        self.assertEqual(
            [awaited.args[0] for awaited in sleep.await_args_list],
            [1, 2],
        )
        logs = "\n".join(captured.output)
        self.assertNotIn(api_key, logs)
        self.assertIn("ClientError", logs)

    def test_hourly_pv_replaces_daily_midnight_spike(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        midnight = datetime(2026, 8, 3, tzinfo=local_tz).astimezone(
            timezone.utc
        )
        noon = datetime(2026, 8, 3, 12, tzinfo=local_tz).astimezone(
            timezone.utc
        )
        records = [
            {
                "start": midnight.timestamp(),
                "state": 116.71,
                "sum": 500.0,
            }
        ]

        result = history.merge_hourly_pv_energy(
            records,
            [(midnight, 0.0), (noon, 116.71)],
            date(2026, 8, 3),
            date(2026, 8, 3),
            local_tz,
        )

        self.assertEqual(result, [(midnight, 0.0), (noon, 116.71)])

    def test_daily_fallback_zeroes_stale_hourly_distribution(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        midnight = datetime(2026, 8, 3, tzinfo=local_tz).astimezone(
            timezone.utc
        )
        noon = datetime(2026, 8, 3, 12, tzinfo=local_tz).astimezone(
            timezone.utc
        )
        one_pm = datetime(2026, 8, 3, 13, tzinfo=local_tz).astimezone(
            timezone.utc
        )
        records = [
            {"start": noon.timestamp(), "state": 4.0, "sum": 4.0},
            {"start": one_pm.timestamp(), "state": 6.0, "sum": 10.0},
        ]

        merged = history.merge_hourly_pv_energy(
            records,
            [(midnight, 10.0)],
            date(2026, 8, 3),
            date(2026, 8, 3),
            local_tz,
        )
        statistics_result = history.build_hourly_pv_statistics(merged, 0.0)

        self.assertEqual(
            merged,
            [(midnight, 10.0), (noon, 0.0), (one_pm, 0.0)],
        )
        self.assertEqual(statistics_result[-1]["sum"], 10.0)

    def test_pv_import_migrates_daily_total_under_same_statistic_id(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        midnight = datetime(2026, 8, 3, tzinfo=local_tz).astimezone(
            timezone.utc
        )
        noon = datetime(2026, 8, 3, 12, tzinfo=local_tz).astimezone(
            timezone.utc
        )
        daily_record = {
            "start": midnight.timestamp(),
            "state": 116.71,
            "sum": 500.0,
        }
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/Berlin")
        )
        importer = history.Smart1PvHistoryImporter(
            hass,
            types.SimpleNamespace(),
            types.SimpleNamespace(id="pv"),
        )
        importer._existing_statistics = AsyncMock(
            return_value=[daily_record]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=(
                [(midnight, 0.0), (noon, 116.71)],
                True,
                1,
                0,
            )
        )
        fake_recorder = types.SimpleNamespace(
            async_block_till_done=AsyncMock()
        )

        with (
            patch.object(history, "get_instance", return_value=fake_recorder),
            patch.object(
                history,
                "async_add_external_statistics",
            ) as add_statistics,
        ):
            asyncio.run(importer.async_import())

        call = add_statistics.call_args
        self.assertEqual(
            call.kwargs["metadata"]["statistic_id"],
            history.PV_STATISTIC_ID,
        )
        self.assertEqual(
            [record["state"] for record in call.kwargs["statistics"]],
            [0.0, 116.71],
        )
        self.assertAlmostEqual(
            call.kwargs["statistics"][-1]["sum"],
            116.71,
        )
        self.assertFalse(importer.diagnostic_status["migration_required"])

    def test_incomplete_initial_pv_hourly_fetch_is_not_imported(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        hour_start = datetime(2026, 8, 3, tzinfo=local_tz).astimezone(
            timezone.utc
        )
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/Berlin")
        )
        importer = history.Smart1PvHistoryImporter(
            hass,
            types.SimpleNamespace(),
            types.SimpleNamespace(id="pv"),
        )
        importer._existing_statistics = AsyncMock(return_value=[])
        importer._fetch_hourly_energy = AsyncMock(
            return_value=(
                [(hour_start, 0.25)],
                False,
                1,
                0,
            )
        )

        with patch.object(
            history,
            "async_add_external_statistics",
        ) as add_statistics:
            with self.assertLogs(history._LOGGER, level="WARNING"):
                asyncio.run(importer.async_import())

        add_statistics.assert_not_called()
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "incomplete_fetch",
        )

    def test_incomplete_initial_pv_daily_fetch_is_not_imported(self) -> None:
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/Berlin")
        )
        importer = history.Smart1PvHistoryImporter(
            hass,
            types.SimpleNamespace(),
        )
        importer._existing_statistics = AsyncMock(return_value=[])
        importer._fetch_daily_energy = AsyncMock(
            return_value=(
                [(date(2026, 8, 3), 2.5)],
                False,
            )
        )

        with patch.object(
            history,
            "async_add_external_statistics",
        ) as add_statistics:
            with self.assertLogs(history._LOGGER, level="WARNING"):
                asyncio.run(importer.async_import())

        add_statistics.assert_not_called()
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "incomplete_fetch",
        )

    def test_empty_pv_backfill_is_persisted_and_not_repeated(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        state = _HistoryState()
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/Berlin")
        )
        importer = history.Smart1PvHistoryImporter(
            hass,
            types.SimpleNamespace(),
            history_state=state,
        )
        importer._existing_statistics = AsyncMock(return_value=[])
        importer._fetch_daily_energy = AsyncMock(return_value=([], True))

        asyncio.run(importer.async_import())
        asyncio.run(importer.async_import())
        asyncio.run(importer.async_import(1, repair=False))

        self.assertTrue(
            state.is_complete(
                importer.statistic_id,
                importer.schema_version,
            )
        )
        self.assertIs(
            state.data_presence(
                importer.statistic_id,
                importer.schema_version,
            ),
            False,
        )
        self.assertEqual(
            importer._fetch_daily_energy.await_args_list,
            [
                call(
                    today - history.timedelta(days=history.HISTORY_DAYS - 1),
                    today,
                ),
                call(
                    today - history.timedelta(days=history.REFRESH_DAYS - 1),
                    today,
                ),
                call(today, today),
            ],
        )

    def test_missing_pv_statistics_trigger_one_full_rebuild(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        state = _HistoryState()
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/Berlin")
        )
        importer = history.Smart1PvHistoryImporter(
            hass,
            types.SimpleNamespace(),
            history_state=state,
        )
        state.mark_complete(
            importer.statistic_id,
            importer.schema_version,
            has_data=True,
        )
        importer._existing_statistics = AsyncMock(return_value=[])
        importer._fetch_daily_energy = AsyncMock(return_value=([], True))

        asyncio.run(importer.async_import())
        asyncio.run(importer.async_import())

        self.assertEqual(
            importer._fetch_daily_energy.await_args_list,
            [
                call(
                    today - history.timedelta(days=history.HISTORY_DAYS - 1),
                    today,
                ),
                call(
                    today - history.timedelta(days=history.REFRESH_DAYS - 1),
                    today,
                ),
            ],
        )
        self.assertIs(
            state.data_presence(
                importer.statistic_id,
                importer.schema_version,
            ),
            False,
        )

    def test_pv_empty_marker_changes_when_data_appears(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        state = _HistoryState()
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/Berlin")
        )
        importer = history.Smart1PvHistoryImporter(
            hass,
            types.SimpleNamespace(),
            history_state=state,
        )
        importer._existing_statistics = AsyncMock(return_value=[])
        importer._fetch_daily_energy = AsyncMock(
            side_effect=[([], True), ([(today, 2.5)], False)]
        )
        recorder_instance = types.SimpleNamespace(
            async_block_till_done=AsyncMock()
        )

        with (
            patch.object(
                history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(history, "async_add_external_statistics"),
        ):
            asyncio.run(importer.async_import())
            asyncio.run(importer.async_import())

        self.assertIs(
            state.data_presence(
                importer.statistic_id,
                importer.schema_version,
            ),
            True,
        )

    def test_daily_pv_fallback_completes_schema_migration_once(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        midnight = datetime.combine(
            today,
            datetime.min.time(),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        legacy_record = {
            "start": midnight.timestamp(),
            "state": 10.0,
            "sum": 10.0,
        }
        state = _HistoryState()
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/Berlin")
        )
        importer = history.Smart1PvHistoryImporter(
            hass,
            types.SimpleNamespace(),
            types.SimpleNamespace(id="pv"),
            history_state=state,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[legacy_record]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=([(midnight, 10.0)], True, 0, 1)
        )
        recorder_instance = types.SimpleNamespace(
            async_block_till_done=AsyncMock()
        )

        with (
            patch.object(
                history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(history, "async_add_external_statistics"),
        ):
            asyncio.run(importer.async_import())
            asyncio.run(importer.async_import())

        first_start = importer._fetch_hourly_energy.await_args_list[0].args[0]
        second_start = importer._fetch_hourly_energy.await_args_list[1].args[0]
        self.assertEqual(
            first_start,
            today - history.timedelta(days=history.HISTORY_DAYS - 1),
        )
        self.assertEqual(
            second_start,
            today - history.timedelta(days=history.REFRESH_DAYS - 1),
        )
        self.assertFalse(importer.diagnostic_status["migration_required"])

    def test_new_power_profile_upgrades_completed_daily_history(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        midnight = datetime.combine(
            today,
            datetime.min.time(),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        state = _HistoryState()
        statistic_id = history.pv_statistic_id()
        state.mark_complete(
            statistic_id,
            history.PV_DAILY_HISTORY_SCHEMA_VERSION,
            has_data=True,
        )
        importer = history.Smart1PvHistoryImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            types.SimpleNamespace(id="pv"),
            history_state=state,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[
                {
                    "start": midnight.timestamp(),
                    "state": 10.0,
                    "sum": 10.0,
                }
            ]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=([(midnight, 10.0)], True, 0, 1)
        )
        recorder_instance = types.SimpleNamespace(
            async_block_till_done=AsyncMock()
        )

        with (
            patch.object(
                history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(history, "async_add_external_statistics"),
        ):
            asyncio.run(importer.async_import())

        self.assertEqual(
            importer._fetch_hourly_energy.await_args.args[0],
            today - history.timedelta(days=history.HISTORY_DAYS - 1),
        )
        self.assertTrue(
            state.is_complete(
                statistic_id,
                history.PV_HISTORY_SCHEMA_VERSION,
            )
        )

    def test_daily_to_hourly_upgrade_preserves_cumulative_baseline(
        self,
    ) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        full_start = today - history.timedelta(
            days=history.HISTORY_DAYS - 1
        )
        predecessor_start = datetime.combine(
            full_start - history.timedelta(days=1),
            datetime.min.time().replace(hour=23),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        daily_start = datetime.combine(
            today - history.timedelta(days=10),
            datetime.min.time(),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        fetched_start = datetime.combine(
            full_start,
            datetime.min.time().replace(hour=12),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        state = _HistoryState()
        statistic_id = history.pv_statistic_id()
        state.mark_complete(
            statistic_id,
            history.PV_DAILY_HISTORY_SCHEMA_VERSION,
            has_data=True,
        )
        importer = history.Smart1PvHistoryImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            types.SimpleNamespace(id="pv"),
            history_state=state,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[
                {
                    "start": predecessor_start.timestamp(),
                    "state": 5.0,
                    "sum": 500.0,
                },
                {
                    "start": daily_start.timestamp(),
                    "state": 0.0,
                    "sum": 500.0,
                },
            ]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=([(fetched_start, 10.0)], True, 1, 0)
        )
        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(
                side_effect=_complete_statistics_clear
            ),
            async_block_till_done=AsyncMock(),
        )

        with (
            patch.object(
                history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(
                history,
                "async_add_external_statistics",
            ) as add_statistics,
        ):
            asyncio.run(importer.async_import())

        importer._fetch_hourly_energy.assert_awaited_once()
        self.assertEqual(
            importer._fetch_hourly_energy.await_args.args[0],
            full_start,
        )
        recorder_instance.async_clear_statistics.assert_not_called()
        imported = add_statistics.call_args.kwargs["statistics"]
        self.assertEqual(imported[0]["sum"], 510.0)
        self.assertTrue(all(item["sum"] >= 500.0 for item in imported))
        self.assertTrue(
            state.is_current_schema(
                statistic_id,
                history.PV_HISTORY_SCHEMA_VERSION,
            )
        )

    def test_empty_hourly_upgrade_retains_existing_presence(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        full_start = today - history.timedelta(
            days=history.HISTORY_DAYS - 1
        )
        predecessor_start = datetime.combine(
            full_start - history.timedelta(days=1),
            datetime.min.time(),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        state = _HistoryState()
        statistic_id = history.pv_statistic_id()
        state.mark_complete(
            statistic_id,
            history.PV_DAILY_HISTORY_SCHEMA_VERSION,
            has_data=True,
        )
        importer = history.Smart1PvHistoryImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            types.SimpleNamespace(id="pv"),
            history_state=state,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[
                {
                    "start": predecessor_start.timestamp(),
                    "state": 5.0,
                    "sum": 500.0,
                }
            ]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=([], True, 0, 0)
        )
        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(),
            async_block_till_done=AsyncMock(),
        )

        with (
            patch.object(
                history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(
                history,
                "async_add_external_statistics",
            ) as add_statistics,
        ):
            asyncio.run(importer.async_import())

        add_statistics.assert_not_called()
        recorder_instance.async_clear_statistics.assert_not_called()
        self.assertTrue(
            state.is_current_schema(
                statistic_id,
                history.PV_HISTORY_SCHEMA_VERSION,
            )
        )
        self.assertIs(
            state.data_presence(
                statistic_id,
                history.PV_HISTORY_SCHEMA_VERSION,
            ),
            True,
        )

    def test_hourly_schema_repairs_daily_or_contaminated_history_once(
        self,
    ) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        old_date = today - history.timedelta(days=10)
        old_midnight = datetime.combine(
            old_date,
            datetime.min.time(),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        old_noon = datetime.combine(
            old_date,
            datetime.min.time().replace(hour=12),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        fetched_noon = datetime.combine(
            today,
            datetime.min.time().replace(hour=12),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        full_start = today - history.timedelta(
            days=history.HISTORY_DAYS - 1
        )
        refresh_start = today - history.timedelta(
            days=history.REFRESH_DAYS - 1
        )

        cases = (
            (
                "current daily schema with zero midnight",
                history.PV_DAILY_HISTORY_SCHEMA_VERSION,
                old_midnight,
                0.0,
                full_start,
            ),
            (
                "pre-fix hourly schema with midnight spike",
                history.PV_HISTORY_SCHEMA_VERSION - 1,
                old_midnight,
                10.0,
                full_start,
            ),
            (
                "valid pre-fix hourly schema",
                history.PV_HISTORY_SCHEMA_VERSION - 1,
                old_noon,
                1.0,
                refresh_start,
            ),
        )

        for name, stored_schema, old_start, old_state, expected_start in cases:
            with self.subTest(name=name):
                state = _HistoryState()
                statistic_id = history.pv_statistic_id()
                state.mark_complete(
                    statistic_id,
                    stored_schema,
                    has_data=True,
                )
                importer = history.Smart1PvHistoryImporter(
                    types.SimpleNamespace(
                        config=types.SimpleNamespace(
                            time_zone="Europe/Berlin"
                        )
                    ),
                    types.SimpleNamespace(),
                    types.SimpleNamespace(id="pv"),
                    history_state=state,
                )
                importer._existing_statistics = AsyncMock(
                    return_value=[
                        {
                            "start": old_start.timestamp(),
                            "state": old_state,
                            "sum": old_state,
                        }
                    ]
                )
                importer._fetch_hourly_energy = AsyncMock(
                    return_value=([(fetched_noon, 1.0)], True, 1, 0)
                )
                recorder_instance = types.SimpleNamespace(
                    async_block_till_done=AsyncMock(),
                    async_clear_statistics=Mock(),
                )

                with (
                    patch.object(
                        history,
                        "get_instance",
                        return_value=recorder_instance,
                    ),
                    patch.object(
                        history,
                        "async_add_external_statistics",
                    ),
                ):
                    asyncio.run(importer.async_import())
                    asyncio.run(importer.async_import())

                self.assertEqual(
                    [
                        awaited.args[0]
                        for awaited in (
                            importer._fetch_hourly_energy.await_args_list
                        )
                    ],
                    [expected_start, refresh_start],
                )
                self.assertTrue(
                    state.is_current_schema(
                        statistic_id,
                        history.PV_HISTORY_SCHEMA_VERSION,
                    )
                )

    def test_daily_importer_reactivates_existing_daily_schema_marker(
        self,
    ) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        old_noon = datetime.combine(
            today - history.timedelta(days=4),
            datetime.min.time().replace(hour=12),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        state = _HistoryState()
        statistic_id = history.pv_statistic_id()
        state.mark_complete(
            statistic_id,
            history.PV_DAILY_HISTORY_SCHEMA_VERSION,
            has_data=True,
        )
        state.mark_complete(
            statistic_id,
            history.PV_HISTORY_SCHEMA_VERSION,
            has_data=True,
        )
        importer = history.Smart1PvHistoryImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            history_state=state,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[
                {"start": old_noon.timestamp(), "state": 1.0, "sum": 1.0}
            ]
        )
        importer._fetch_daily_energy = AsyncMock(
            return_value=([(today, 2.0)], True)
        )
        recorder_instance = types.SimpleNamespace(
            async_block_till_done=AsyncMock(),
            async_clear_statistics=Mock(),
        )

        with (
            patch.object(
                history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(history, "async_add_external_statistics"),
        ):
            asyncio.run(importer.async_import())

        importer._fetch_daily_energy.assert_awaited_once_with(
            today - history.timedelta(days=history.REFRESH_DAYS - 1),
            today,
        )
        self.assertTrue(
            state.is_current_schema(
                statistic_id,
                history.PV_DAILY_HISTORY_SCHEMA_VERSION,
            )
        )

    def test_pv_statistic_id_can_be_scoped_per_installation(self) -> None:
        first = history.Smart1PvHistoryImporter(
            types.SimpleNamespace(),
            types.SimpleNamespace(),
            statistics_namespace="installation-a",
        )
        second = history.Smart1PvHistoryImporter(
            types.SimpleNamespace(),
            types.SimpleNamespace(),
            statistics_namespace="installation-b",
        )

        self.assertEqual(
            first.statistic_id,
            f"{history.PV_STATISTIC_ID}_installation-a",
        )
        self.assertNotEqual(first.statistic_id, second.statistic_id)

    def test_forced_pv_rebuild_clears_legacy_statistic_after_full_fetch(
        self,
    ) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        noon = datetime.combine(
            today,
            datetime.min.time().replace(hour=12),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        state = _HistoryState()
        importer = history.Smart1PvHistoryImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            types.SimpleNamespace(id="pv"),
            history_state=state,
            force_initial_rebuild=True,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[
                {"start": noon.timestamp(), "state": 99.0, "sum": 99.0}
            ]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=([(noon, 1.0)], True, 1, 0)
        )
        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(
                side_effect=_complete_statistics_clear
            ),
            async_block_till_done=AsyncMock(),
        )

        with (
            patch.object(
                history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(history, "async_add_external_statistics"),
        ):
            asyncio.run(importer.async_import())

        self.assertEqual(
            importer._fetch_hourly_energy.await_args.args[0],
            today - history.timedelta(days=history.HISTORY_DAYS - 1),
        )
        self.assertEqual(importer._fetch_hourly_energy.await_args.args[3], {})
        recorder_instance.async_clear_statistics.assert_called_once_with(
            [importer.statistic_id],
            on_done=ANY,
        )
        self.assertTrue(
            state.is_complete(importer.statistic_id, importer.schema_version)
        )

    def test_forced_pv_rebuild_queues_replacement_before_timeout(
        self,
    ) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        noon = datetime.combine(
            today,
            datetime.min.time().replace(hour=12),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        state = _HistoryState()
        importer = history.Smart1PvHistoryImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            types.SimpleNamespace(id="pv"),
            history_state=state,
            force_initial_rebuild=True,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[
                {"start": noon.timestamp(), "state": 99.0, "sum": 99.0}
            ]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=([(noon, 1.0)], True, 1, 0)
        )
        operations: list[str] = []
        queued_jobs = []
        stored_sums = [99.0]

        def clear_statistics(_statistic_ids, *, on_done) -> None:
            operations.append("clear")

            def execute_clear() -> None:
                stored_sums.clear()
                on_done()

            queued_jobs.append(execute_clear)

        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(side_effect=clear_statistics),
        )

        def add_statistics(*, statistics, **_kwargs) -> None:
            operations.append("import")
            replacement_sums = [item["sum"] for item in statistics]
            queued_jobs.append(
                lambda: stored_sums.extend(replacement_sums)
            )

        async def run_scenario() -> None:
            with (
                patch.object(
                    history,
                    "get_instance",
                    return_value=recorder_instance,
                ),
                patch.object(
                    history,
                    "async_add_external_statistics",
                    side_effect=add_statistics,
                ),
                patch.object(
                    recorder_helpers,
                    "RECORDER_OPERATION_TIMEOUT",
                    0.001,
                ),
            ):
                await importer.async_import()
            self.assertEqual(operations, ["clear", "import"])
            self.assertEqual(
                importer.diagnostic_status["last_result"],
                "clear_timeout",
            )
            self.assertFalse(
                state.is_complete(
                    importer.statistic_id,
                    importer.schema_version,
                )
            )
            for job in queued_jobs:
                job()
            await asyncio.sleep(0)
            self.assertTrue(
                state.is_complete(
                    importer.statistic_id,
                    importer.schema_version,
                )
            )

            importer._existing_statistics.return_value = [
                {
                    "start": noon.timestamp(),
                    "state": 1.0,
                    "sum": 1.0,
                }
            ]
            await importer.async_import()

        asyncio.run(run_scenario())

        self.assertEqual(importer.diagnostic_status["last_result"], "completed")
        self.assertTrue(
            state.is_complete(importer.statistic_id, importer.schema_version)
        )
        self.assertEqual(
            importer._fetch_hourly_energy.await_args_list[-1].args[0],
            today - history.timedelta(days=history.REFRESH_DAYS - 1),
        )
        self.assertEqual(
            recorder_instance.async_clear_statistics.call_count,
            1,
        )
        self.assertEqual(stored_sums, [1.0])

    def test_single_entry_adopts_valid_hourly_pv_history(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        existing_start = datetime.combine(
            today - history.timedelta(days=4),
            datetime.min.time().replace(hour=12),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        state = _HistoryState()
        importer = history.Smart1PvHistoryImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            types.SimpleNamespace(id="pv"),
            history_state=state,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[
                {
                    "start": existing_start.timestamp(),
                    "state": 1.0,
                    "sum": 10.0,
                }
            ]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=([], True, 0, 0)
        )
        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(),
        )

        with patch.object(
            history,
            "get_instance",
            return_value=recorder_instance,
        ):
            asyncio.run(importer.async_import())

        self.assertEqual(
            importer._fetch_hourly_energy.await_args.args[0],
            today - history.timedelta(days=history.REFRESH_DAYS - 1),
        )
        recorder_instance.async_clear_statistics.assert_not_called()
        self.assertTrue(
            state.is_complete(importer.statistic_id, importer.schema_version)
        )

    def test_forced_empty_pv_rebuild_clears_and_marks_complete(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        midnight = datetime.combine(
            today,
            datetime.min.time(),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        state = _HistoryState()
        importer = history.Smart1PvHistoryImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            history_state=state,
            force_initial_rebuild=True,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[
                {
                    "start": midnight.timestamp(),
                    "state": 99.0,
                    "sum": 99.0,
                }
            ]
        )
        importer._fetch_daily_energy = AsyncMock(return_value=([], True))
        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(
                side_effect=_complete_statistics_clear
            ),
            async_block_till_done=AsyncMock(),
        )

        with (
            patch.object(
                history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(history, "async_add_external_statistics") as add_stats,
        ):
            asyncio.run(importer.async_import())

        importer._fetch_daily_energy.assert_awaited_once_with(
            today - history.timedelta(days=history.HISTORY_DAYS - 1),
            today,
        )
        recorder_instance.async_clear_statistics.assert_called_once_with(
            [importer.statistic_id],
            on_done=ANY,
        )
        add_stats.assert_not_called()
        self.assertTrue(
            state.is_complete(importer.statistic_id, importer.schema_version)
        )

    def test_forced_pv_rebuild_keeps_legacy_data_on_incomplete_fetch(
        self,
    ) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        state = _HistoryState()
        importer = history.Smart1PvHistoryImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            history_state=state,
            force_initial_rebuild=True,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[
                {
                    "start": datetime.now(timezone.utc).timestamp(),
                    "state": 99.0,
                    "sum": 99.0,
                }
            ]
        )
        importer._fetch_daily_energy = AsyncMock(return_value=([], False))
        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(),
            async_block_till_done=AsyncMock(),
        )

        with (
            patch.object(
                history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(history, "async_add_external_statistics") as add_stats,
        ):
            asyncio.run(importer.async_import())

        importer._fetch_daily_energy.assert_awaited_once_with(
            today - history.timedelta(days=history.HISTORY_DAYS - 1),
            today,
        )
        recorder_instance.async_clear_statistics.assert_not_called()
        add_stats.assert_not_called()
        self.assertFalse(
            state.is_complete(importer.statistic_id, importer.schema_version)
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

    def test_derived_failure_log_does_not_expose_api_key(self) -> None:
        api_key = "fake-api-key-must-not-leak"

        class FailingApi:
            def __init__(self) -> None:
                self.calls = 0

            async def get_linear_detailed_rows(self, *args, **kwargs):
                self.calls += 1
                raise aiohttp.ClientError(
                    "Request failed for "
                    f"https://example.test/?apikey={api_key}"
                )

        api = FailingApi()
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(),
            api,
            {"grid_import": types.SimpleNamespace(id="grid")},
        )

        with (
            patch.object(
                derived_history.asyncio,
                "sleep",
                new=AsyncMock(),
            ),
            self.assertLogs(
                derived_history._LOGGER,
                level="WARNING",
            ) as captured,
        ):
            result, completed = asyncio.run(
                importer._fetch_hourly_energy(
                    date(2026, 8, 3),
                    date(2026, 8, 3),
                    ZoneInfo("Europe/Berlin"),
                )
            )

        logs = "\n".join(captured.output)
        self.assertFalse(completed)
        self.assertEqual(result, {"grid_import": []})
        self.assertEqual(api.calls, derived_history.FETCH_ATTEMPTS)
        self.assertNotIn(api_key, logs)
        self.assertIn("ClientError", logs)

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
            derived_history.needs_hourly_rebuild(
                records,
                date(2026, 8, 4),
                local_tz,
            )
        )

    def test_completed_midnight_only_derived_history_uses_refresh_window(
        self,
    ) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        state = _HistoryState()
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            {
                "grid_import": types.SimpleNamespace(
                    id="grid",
                    name="Bezug",
                )
            },
            history_state=state,
        )
        statistic_id = derived_history.statistic_id_for_role(
            "grid_import",
            "grid",
        )
        state.mark_complete(
            statistic_id,
            derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
            has_data=True,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[
                {
                    "start": datetime.combine(
                        target_date,
                        datetime.min.time(),
                        tzinfo=local_tz,
                    ).timestamp(),
                    "state": energy,
                    "sum": total,
                }
                for target_date, energy, total in (
                    (today - history.timedelta(days=4), 1.0, 10.0),
                    (today - history.timedelta(days=1), 2.0, 12.0),
                )
            ]
        )
        fetched_start = datetime.combine(
            today,
            datetime.min.time().replace(hour=1),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        importer._fetch_hourly_energy = AsyncMock(
            return_value=({"grid_import": [(fetched_start, 0.25)]}, True)
        )
        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(),
            async_block_till_done=AsyncMock(),
        )

        with (
            patch.object(
                derived_history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(
                derived_history,
                "async_add_external_statistics",
            ),
        ):
            asyncio.run(importer.async_import())

        importer._fetch_hourly_energy.assert_awaited_once_with(
            today - history.timedelta(days=history.REFRESH_DAYS - 1),
            today,
            local_tz,
        )
        recorder_instance.async_clear_statistics.assert_not_called()
        self.assertEqual(
            importer.diagnostic_status["detected_rebuild_roles"],
            [],
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
            derived_history.needs_hourly_rebuild(
                records,
                date(2026, 8, 4),
                local_tz,
            )
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
            derived_history.needs_hourly_rebuild(
                records,
                date(2026, 8, 4),
                local_tz,
                initial_backfill_complete=True,
            )
        )
        self.assertEqual(
            derived_history.determine_hourly_import_window(
                records,
                date(2026, 8, 4),
                local_tz,
                initial_backfill_complete=True,
            ),
            (date(2025, 8, 5), 0.0),
        )

    def test_decrease_before_supported_window_is_ignored(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = date(2026, 8, 4)
        full_start = today - history.timedelta(
            days=history.HISTORY_DAYS - 1
        )

        def record(target: date, hour: int, total: float) -> dict:
            return {
                "start": datetime(
                    target.year,
                    target.month,
                    target.day,
                    hour,
                    tzinfo=local_tz,
                ).timestamp(),
                "state": 1.0,
                "sum": total,
            }

        records = [
            record(full_start - history.timedelta(days=2), 12, 200.0),
            record(full_start - history.timedelta(days=1), 12, 100.0),
            record(full_start, 1, 101.0),
            record(date(2026, 8, 1), 23, 150.0),
            record(today, 1, 151.0),
            record(today + history.timedelta(days=1), 1, 1.0),
        ]

        self.assertFalse(
            derived_history.needs_hourly_rebuild(
                records,
                today,
                local_tz,
                initial_backfill_complete=True,
            )
        )
        self.assertEqual(
            derived_history.determine_hourly_import_window(
                records,
                today,
                local_tz,
                initial_backfill_complete=True,
            ),
            (date(2026, 8, 2), 150.0),
        )

    def test_drop_at_supported_window_boundary_triggers_rebuild(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = date(2026, 8, 4)
        full_start = today - history.timedelta(
            days=history.HISTORY_DAYS - 1
        )
        records = [
            {
                "start": datetime.combine(
                    full_start - history.timedelta(days=1),
                    datetime.min.time().replace(hour=23),
                    tzinfo=local_tz,
                ).timestamp(),
                "state": 1.0,
                "sum": 100.0,
            },
            {
                "start": datetime.combine(
                    full_start,
                    datetime.min.time().replace(hour=1),
                    tzinfo=local_tz,
                ).timestamp(),
                "state": 1.0,
                "sum": 2.0,
            },
        ]

        self.assertTrue(
            derived_history.needs_hourly_rebuild(
                records,
                today,
                local_tz,
                initial_backfill_complete=True,
            )
        )
        self.assertEqual(
            derived_history.determine_hourly_import_window(
                records,
                today,
                local_tz,
                initial_backfill_complete=True,
            ),
            (full_start, 100.0),
        )

    def test_supported_window_boundaries_follow_local_dst_dates(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")

        autumn_today = date(2026, 10, 25)
        autumn_start = autumn_today - history.timedelta(
            days=history.HISTORY_DAYS - 1
        )
        predecessor = datetime.combine(
            autumn_start - history.timedelta(days=1),
            datetime.min.time().replace(hour=23),
            tzinfo=local_tz,
        )
        first_fold = datetime(
            autumn_start.year,
            autumn_start.month,
            autumn_start.day,
            2,
            tzinfo=local_tz,
            fold=0,
        )
        second_fold = first_fold.replace(fold=1)
        autumn_records = [
            {"start": predecessor.timestamp(), "state": 1.0, "sum": 100.0},
            {"start": second_fold.timestamp(), "state": 1.0, "sum": 102.0},
            {"start": first_fold.timestamp(), "state": 1.0, "sum": 101.0},
        ]
        self.assertFalse(
            derived_history.needs_hourly_rebuild(
                autumn_records,
                autumn_today,
                local_tz,
                initial_backfill_complete=True,
            )
        )
        decreasing_autumn_records = [dict(record) for record in autumn_records]
        decreasing_autumn_records[1]["sum"] = 99.0
        self.assertTrue(
            derived_history.needs_hourly_rebuild(
                decreasing_autumn_records,
                autumn_today,
                local_tz,
                initial_backfill_complete=True,
            )
        )

        spring_today = date(2026, 3, 29)
        spring_start = spring_today - history.timedelta(
            days=history.HISTORY_DAYS - 1
        )
        spring_records = [
            {
                "start": datetime.combine(
                    spring_start - history.timedelta(days=1),
                    datetime.min.time().replace(hour=23),
                    tzinfo=local_tz,
                ).timestamp(),
                "state": 1.0,
                "sum": 100.0,
            }
        ] + [
            {
                "start": datetime.combine(
                    spring_start,
                    datetime.min.time().replace(hour=hour),
                    tzinfo=local_tz,
                ).timestamp(),
                "state": 1.0,
                "sum": total,
            }
            for hour, total in ((1, 101.0), (3, 102.0))
        ]
        self.assertFalse(
            derived_history.needs_hourly_rebuild(
                spring_records,
                spring_today,
                local_tz,
                initial_backfill_complete=True,
            )
        )

    def test_predecessor_does_not_mask_daily_only_window(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = date(2026, 8, 4)
        full_start = today - history.timedelta(
            days=history.HISTORY_DAYS - 1
        )
        records = [
            {
                "start": datetime.combine(
                    full_start - history.timedelta(days=1),
                    datetime.min.time().replace(hour=23),
                    tzinfo=local_tz,
                ).timestamp(),
                "state": 1.0,
                "sum": 1.0,
            }
        ] + [
            {
                "start": datetime.combine(
                    target,
                    datetime.min.time(),
                    tzinfo=local_tz,
                ).timestamp(),
                "state": 1.0,
                "sum": total,
            }
            for target, total in ((full_start, 2.0), (today, 3.0))
        ]

        self.assertTrue(
            derived_history.needs_hourly_rebuild(
                records,
                today,
                local_tz,
            )
        )

    def test_multi_role_importer_ignores_unrepairable_decreases(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        full_start = today - history.timedelta(
            days=history.HISTORY_DAYS - 1
        )
        state = _HistoryState()
        points = {
            "grid_import": types.SimpleNamespace(id="grid", name="Bezug"),
            "wallbox_consumption": types.SimpleNamespace(
                id="wallbox",
                name="Wallbox",
            ),
        }
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            points,
            history_state=state,
        )
        statistic_ids = {
            role_key: derived_history.statistic_id_for_role(
                role_key,
                point.id,
            )
            for role_key, point in points.items()
        }
        for statistic_id in statistic_ids.values():
            state.mark_complete(
                statistic_id,
                derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
                has_data=True,
            )

        sparse_records = [
            {
                "start": datetime.combine(
                    target,
                    datetime.min.time().replace(hour=hour),
                    tzinfo=local_tz,
                ).timestamp(),
                "state": 1.0,
                "sum": total,
            }
            for target, hour, total in (
                (full_start - history.timedelta(days=2), 12, 200.0),
                (full_start - history.timedelta(days=1), 12, 100.0),
                (full_start, 1, 101.0),
                (today - history.timedelta(days=3), 23, 150.0),
                (today + history.timedelta(days=1), 1, 1.0),
            )
        ]
        normal_records = [
            {
                "start": datetime.combine(
                    target,
                    datetime.min.time().replace(hour=hour),
                    tzinfo=local_tz,
                ).timestamp(),
                "state": 1.0,
                "sum": total,
            }
            for target, hour, total in (
                (today - history.timedelta(days=4), 23, 20.0),
                (today - history.timedelta(days=1), 23, 21.0),
            )
        ]
        records_by_id = {
            statistic_ids["grid_import"]: sparse_records,
            statistic_ids["wallbox_consumption"]: normal_records,
        }

        async def existing_statistics(
            statistic_id: str,
            _record_count: int,
        ) -> list[dict]:
            return records_by_id[statistic_id]

        importer._existing_statistics = AsyncMock(
            side_effect=existing_statistics
        )
        fetched_start = datetime.combine(
            today,
            datetime.min.time().replace(hour=1),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        importer._fetch_hourly_energy = AsyncMock(
            return_value=(
                {
                    "grid_import": [(fetched_start, 0.25)],
                    "wallbox_consumption": [(fetched_start, 0.5)],
                },
                True,
            )
        )
        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(),
        )

        with (
            patch.object(
                derived_history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(
                derived_history,
                "async_add_external_statistics",
            ),
        ):
            asyncio.run(importer.async_import())

        importer._fetch_hourly_energy.assert_awaited_once_with(
            today - history.timedelta(days=history.REFRESH_DAYS - 1),
            today,
            local_tz,
        )
        recorder_instance.async_clear_statistics.assert_not_called()
        self.assertEqual(
            importer.diagnostic_status["detected_rebuild_roles"],
            [],
        )

    def test_detected_rebuild_upserts_only_selected_statistics(self) -> None:
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
        fake_recorder.async_clear_statistics.assert_not_called()
        fake_recorder.async_block_till_done.assert_not_awaited()
        self.assertEqual(add_statistics.call_count, 2)
        self.assertNotIn("smart1_ems:pv_production", expected_ids)
        for call in add_statistics.call_args_list:
            self.assertEqual(len(call.kwargs["statistics"]), 1)
            self.assertNotEqual(
                call.kwargs["statistics"][0]["state"],
                daily_record["state"],
            )

    def test_partial_derived_rebuild_preserves_unreplaced_days(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        preserved_start = datetime.combine(
            today - history.timedelta(days=400),
            datetime.min.time(),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        replaced_start = datetime.combine(
            today - history.timedelta(days=1),
            datetime.min.time(),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        replacement_hour = datetime.combine(
            today - history.timedelta(days=1),
            datetime.min.time().replace(hour=12),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        state = _HistoryState()
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            {
                "grid_import": types.SimpleNamespace(
                    id="grid",
                    name="Bezug",
                )
            },
            history_state=state,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[
                {
                    "start": preserved_start.timestamp(),
                    "state": 2.0,
                    "sum": 2.0,
                },
                {
                    "start": replaced_start.timestamp(),
                    "state": 4.0,
                    "sum": 6.0,
                },
            ]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=(
                {"grid_import": [(replacement_hour, 1.0)]},
                True,
            )
        )
        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(
                side_effect=_complete_statistics_clear
            ),
            async_block_till_done=AsyncMock(),
        )

        with (
            patch.object(
                derived_history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(
                derived_history,
                "async_add_external_statistics",
            ) as add_statistics,
        ):
            asyncio.run(importer.async_import())

        statistic_id = derived_history.statistic_id_for_role(
            "grid_import",
            "grid",
        )
        recorder_instance.async_clear_statistics.assert_not_called()
        imported = add_statistics.call_args.kwargs["statistics"]
        self.assertEqual(
            [(item["start"], item["state"], item["sum"]) for item in imported],
            [
                (replaced_start, 0.0, 2.0),
                (replacement_hour, 1.0, 3.0),
            ],
        )
        self.assertTrue(
            state.is_complete(
                statistic_id,
                derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
            )
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

    def test_incomplete_initial_derived_fetch_is_not_imported(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
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
                )
            },
        )
        importer._existing_statistics = AsyncMock(return_value=[])
        importer._fetch_hourly_energy = AsyncMock(
            return_value=(
                {"grid_import": [(hour_start, 0.25)]},
                False,
            )
        )

        with patch.object(
            derived_history,
            "async_add_external_statistics",
        ) as add_statistics:
            with self.assertLogs(derived_history._LOGGER, level="WARNING"):
                asyncio.run(importer.async_import())

        add_statistics.assert_not_called()
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "incomplete_fetch",
        )
        self.assertFalse(
            importer.diagnostic_status["last_fetch_completed"]
        )

    def test_initial_derived_fetch_retries_full_window_after_incomplete_run(
        self,
    ) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        hour_start = datetime.combine(
            today,
            datetime.min.time(),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        state = _HistoryState()
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            {
                "grid_import": types.SimpleNamespace(
                    id="grid",
                    name="Bezug",
                )
            },
            history_state=state,
        )
        importer._existing_statistics = AsyncMock(return_value=[])
        importer._fetch_hourly_energy = AsyncMock(
            side_effect=[
                ({"grid_import": [(hour_start, 0.25)]}, False),
                ({"grid_import": [(hour_start, 0.25)]}, True),
            ]
        )
        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(),
            async_block_till_done=AsyncMock(),
        )

        with (
            patch.object(
                derived_history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(
                derived_history,
                "async_add_external_statistics",
            ) as add_statistics,
        ):
            with self.assertLogs(derived_history._LOGGER, level="WARNING"):
                asyncio.run(importer.async_import())
            asyncio.run(importer.async_import())

        expected_start = today - history.timedelta(
            days=history.HISTORY_DAYS - 1
        )
        self.assertEqual(
            [
                awaited.args
                for awaited in importer._fetch_hourly_energy.await_args_list
            ],
            [
                (expected_start, today, local_tz),
                (expected_start, today, local_tz),
            ],
        )
        add_statistics.assert_called_once()
        self.assertTrue(
            state.is_complete(
                derived_history.statistic_id_for_role(
                    "grid_import",
                    "grid",
                ),
                derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
            )
        )

    def test_empty_role_preserves_existing_statistic_during_repair(self) -> None:
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
            asyncio.run(importer.async_import())

        fake_recorder.async_clear_statistics.assert_not_called()
        self.assertEqual(add_statistics.call_count, 2)
        imported_by_id = {
            call.kwargs["metadata"]["statistic_id"]: call.kwargs["statistics"]
            for call in add_statistics.call_args_list
        }
        wallbox_id = derived_history.statistic_id_for_role(
            "wallbox_consumption",
            "wallbox",
        )
        self.assertEqual(
            imported_by_id[wallbox_id][0]["state"],
            daily_record["state"],
        )
        self.assertEqual(
            importer.diagnostic_status["roles_without_replacement"],
            [],
        )
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "completed",
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

        importer._existing_statistics.assert_awaited_once_with(
            derived_history.statistic_id_for_role("grid_import", "grid"),
            2 * history.MAX_HOURLY_RECORDS_PER_DAY,
        )
        importer._fetch_hourly_energy.assert_not_awaited()
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "repair_pending",
        )

    def test_current_day_derived_refresh_waits_for_initial_backfill(
        self,
    ) -> None:
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
        importer._existing_statistics = AsyncMock(return_value=[])
        importer._fetch_hourly_energy = AsyncMock()

        asyncio.run(importer.async_import(1, repair=False))

        importer._fetch_hourly_energy.assert_not_awaited()
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "repair_pending",
        )

    def test_empty_derived_backfill_is_persisted_and_not_repeated(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        state = _HistoryState()
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
            history_state=state,
        )
        importer._existing_statistics = AsyncMock(return_value=[])
        importer._fetch_hourly_energy = AsyncMock(
            return_value=({"grid_import": []}, True)
        )
        recorder_instance = types.SimpleNamespace(
            async_block_till_done=AsyncMock(),
            async_clear_statistics=Mock(),
        )

        with patch.object(
            derived_history,
            "get_instance",
            return_value=recorder_instance,
        ):
            asyncio.run(importer.async_import())
            asyncio.run(importer.async_import())
            asyncio.run(importer.async_import(1, repair=False))

        statistic_id = derived_history.statistic_id_for_role(
            "grid_import",
            "grid",
        )
        self.assertTrue(
            state.is_complete(
                statistic_id,
                derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
            )
        )
        self.assertIs(
            state.data_presence(
                statistic_id,
                derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
            ),
            False,
        )
        self.assertEqual(
            [
                awaited.args[0]
                for awaited in importer._fetch_hourly_energy.await_args_list
            ],
            [
                today - history.timedelta(days=history.HISTORY_DAYS - 1),
                today - history.timedelta(days=history.REFRESH_DAYS - 1),
                today,
            ],
        )

    def test_empty_derived_rebuild_is_preserved_and_not_repeated(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        existing_start = datetime.combine(
            today - history.timedelta(days=4),
            datetime.min.time(),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        existing_record = {
            "start": existing_start.timestamp(),
            "state": 5.0,
            "sum": 20.0,
        }
        state = _HistoryState()
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            {
                "grid_import": types.SimpleNamespace(
                    id="grid",
                    name="Bezug",
                )
            },
            history_state=state,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[existing_record]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=({"grid_import": []}, True)
        )
        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(),
            async_block_till_done=AsyncMock(),
        )

        with (
            patch.object(
                derived_history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(
                derived_history,
                "async_add_external_statistics",
            ) as add_statistics,
        ):
            asyncio.run(importer.async_import())
            asyncio.run(importer.async_import())

        statistic_id = derived_history.statistic_id_for_role(
            "grid_import",
            "grid",
        )
        self.assertEqual(
            [
                awaited.args[0]
                for awaited in importer._fetch_hourly_energy.await_args_list
            ],
            [
                today - history.timedelta(days=history.HISTORY_DAYS - 1),
                today - history.timedelta(days=history.REFRESH_DAYS - 1),
            ],
        )
        recorder_instance.async_clear_statistics.assert_not_called()
        add_statistics.assert_called_once()
        imported = add_statistics.call_args.kwargs["statistics"]
        self.assertEqual(
            [(item["start"], item["state"], item["sum"]) for item in imported],
            [(existing_start, 5.0, 5.0)],
        )
        self.assertTrue(
            state.is_complete(
                statistic_id,
                derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
            )
        )
        self.assertIs(
            state.data_presence(
                statistic_id,
                derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
            ),
            True,
        )

    def test_empty_decreasing_derived_rebuild_normalizes_sums_once(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        starts = [
            datetime.combine(
                today - history.timedelta(days=4),
                datetime.min.time().replace(hour=hour),
                tzinfo=local_tz,
            ).astimezone(timezone.utc)
            for hour in (1, 2)
        ]
        original_records = [
            {"start": starts[0].timestamp(), "state": 1.0, "sum": 10.0},
            {"start": starts[1].timestamp(), "state": 1.0, "sum": 2.0},
        ]
        normalized_records = [
            {"start": starts[0].timestamp(), "state": 1.0, "sum": 1.0},
            {"start": starts[1].timestamp(), "state": 1.0, "sum": 2.0},
        ]
        state = _HistoryState()
        statistic_id = derived_history.statistic_id_for_role(
            "grid_import",
            "grid",
        )
        state.mark_complete(
            statistic_id,
            derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
            has_data=True,
        )
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            {
                "grid_import": types.SimpleNamespace(
                    id="grid",
                    name="Bezug",
                )
            },
            history_state=state,
        )
        importer._existing_statistics = AsyncMock(
            side_effect=[original_records, normalized_records]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=({"grid_import": []}, True)
        )
        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(),
            async_block_till_done=AsyncMock(),
        )

        with (
            patch.object(
                derived_history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(
                derived_history,
                "async_add_external_statistics",
            ) as add_statistics,
        ):
            asyncio.run(importer.async_import())
            asyncio.run(importer.async_import())

        self.assertEqual(
            [
                awaited.args[0]
                for awaited in importer._fetch_hourly_energy.await_args_list
            ],
            [
                today - history.timedelta(days=history.HISTORY_DAYS - 1),
                today - history.timedelta(days=history.REFRESH_DAYS - 1),
            ],
        )
        recorder_instance.async_clear_statistics.assert_not_called()
        add_statistics.assert_called_once()
        self.assertEqual(
            [item["sum"] for item in add_statistics.call_args.kwargs["statistics"]],
            [1.0, 2.0],
        )

    def test_missing_derived_statistics_trigger_one_full_rebuild(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        state = _HistoryState()
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            {
                "grid_import": types.SimpleNamespace(
                    id="grid",
                    name="Bezug",
                )
            },
            history_state=state,
        )
        statistic_id = derived_history.statistic_id_for_role(
            "grid_import",
            "grid",
        )
        state.mark_complete(
            statistic_id,
            derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
            has_data=True,
        )
        importer._existing_statistics = AsyncMock(return_value=[])
        importer._fetch_hourly_energy = AsyncMock(
            return_value=({"grid_import": []}, True)
        )
        recorder_instance = types.SimpleNamespace(
            async_block_till_done=AsyncMock(),
            async_clear_statistics=Mock(),
        )

        with patch.object(
            derived_history,
            "get_instance",
            return_value=recorder_instance,
        ):
            asyncio.run(importer.async_import())
            asyncio.run(importer.async_import())

        self.assertEqual(
            [
                awaited.args[0]
                for awaited in importer._fetch_hourly_energy.await_args_list
            ],
            [
                today - history.timedelta(days=history.HISTORY_DAYS - 1),
                today - history.timedelta(days=history.REFRESH_DAYS - 1),
            ],
        )
        self.assertIs(
            state.data_presence(
                statistic_id,
                derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
            ),
            False,
        )

    def test_derived_empty_marker_changes_when_data_appears(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        hour_start = datetime.combine(
            today,
            datetime.min.time(),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        state = _HistoryState()
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            {
                "grid_import": types.SimpleNamespace(
                    id="grid",
                    name="Bezug",
                )
            },
            history_state=state,
        )
        importer._existing_statistics = AsyncMock(return_value=[])
        importer._fetch_hourly_energy = AsyncMock(
            side_effect=[
                ({"grid_import": []}, True),
                ({"grid_import": [(hour_start, 0.25)]}, False),
            ]
        )
        recorder_instance = types.SimpleNamespace(
            async_block_till_done=AsyncMock(),
            async_clear_statistics=Mock(),
        )

        with (
            patch.object(
                derived_history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(
                derived_history,
                "async_add_external_statistics",
            ),
        ):
            asyncio.run(importer.async_import())
            asyncio.run(importer.async_import())

        statistic_id = derived_history.statistic_id_for_role(
            "grid_import",
            "grid",
        )
        self.assertIs(
            state.data_presence(
                statistic_id,
                derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
            ),
            True,
        )

    def test_derived_statistic_id_uses_installation_namespace(self) -> None:
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            {
                "grid_import": types.SimpleNamespace(
                    id="grid",
                    name="Bezug",
                )
            },
            statistics_namespace="installation-a",
        )
        hour_start = datetime(2026, 8, 3, tzinfo=timezone.utc)
        importer._existing_statistics = AsyncMock(return_value=[])
        importer._fetch_hourly_energy = AsyncMock(
            return_value=({"grid_import": [(hour_start, 0.25)]}, True)
        )

        with patch.object(
            derived_history,
            "async_add_external_statistics",
        ) as add_statistics:
            asyncio.run(importer.async_import())

        self.assertTrue(
            add_statistics.call_args.kwargs["metadata"]["statistic_id"].endswith(
                "_installation-a"
            )
        )

    def test_forced_empty_derived_rebuild_clears_and_marks_complete(
        self,
    ) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        hour_start = datetime.now(timezone.utc).replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        state = _HistoryState()
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            {
                "grid_import": types.SimpleNamespace(
                    id="grid",
                    name="Bezug",
                )
            },
            history_state=state,
            force_initial_rebuild=True,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[
                {
                    "start": hour_start.timestamp(),
                    "state": 99.0,
                    "sum": 99.0,
                }
            ]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=({"grid_import": []}, True)
        )
        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(
                side_effect=_complete_statistics_clear
            ),
            async_block_till_done=AsyncMock(),
        )

        with (
            patch.object(
                derived_history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(
                derived_history,
                "async_add_external_statistics",
            ) as add_stats,
        ):
            asyncio.run(importer.async_import())

        statistic_id = derived_history.statistic_id_for_role(
            "grid_import",
            "grid",
        )
        importer._fetch_hourly_energy.assert_awaited_once_with(
            today - history.timedelta(days=history.HISTORY_DAYS - 1),
            today,
            local_tz,
        )
        recorder_instance.async_clear_statistics.assert_called_once_with(
            [statistic_id],
            on_done=ANY,
        )
        add_stats.assert_not_called()
        self.assertTrue(
            state.is_complete(
                statistic_id,
                derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
            )
        )

    def test_forced_derived_rebuild_queues_all_replacements_before_timeout(
        self,
    ) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        hour_start = datetime.combine(
            today,
            datetime.min.time().replace(hour=12),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        state = _HistoryState()
        points = {
            "grid_import": types.SimpleNamespace(id="grid", name="Bezug"),
            "wallbox_consumption": types.SimpleNamespace(
                id="wallbox",
                name="Wallbox",
            ),
        }
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            points,
            history_state=state,
            force_initial_rebuild=True,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[
                {
                    "start": hour_start.timestamp(),
                    "state": 99.0,
                    "sum": 99.0,
                }
            ]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=(
                {
                    "grid_import": [(hour_start, 1.0)],
                    "wallbox_consumption": [],
                },
                True,
            )
        )
        operations: list[str] = []
        callbacks = []

        def clear_statistics(_statistic_ids, *, on_done) -> None:
            operations.append("clear")
            callbacks.append(on_done)

        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(side_effect=clear_statistics),
        )

        def add_statistics(*, metadata, **_kwargs) -> None:
            operations.append(f"import:{metadata['statistic_id']}")

        async def run_scenario() -> None:
            with (
                patch.object(
                    derived_history,
                    "get_instance",
                    return_value=recorder_instance,
                ),
                patch.object(
                    derived_history,
                    "async_add_external_statistics",
                    side_effect=add_statistics,
                ),
                patch.object(
                    recorder_helpers,
                    "RECORDER_OPERATION_TIMEOUT",
                    0.001,
                ),
            ):
                await importer.async_import()
            self.assertEqual(
                importer.diagnostic_status["last_result"],
                "clear_timeout",
            )
            callbacks[0]()
            await asyncio.sleep(0)

            grid_statistic_id = derived_history.statistic_id_for_role(
                "grid_import",
                points["grid_import"].id,
            )
            self.assertEqual(
                operations,
                ["clear", f"import:{grid_statistic_id}"],
            )
            for role_key, point in points.items():
                self.assertTrue(
                    state.is_complete(
                        derived_history.statistic_id_for_role(
                            role_key,
                            point.id,
                        ),
                        derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
                    )
                )

            await importer.async_import()

        asyncio.run(run_scenario())

        grid_statistic_id = derived_history.statistic_id_for_role(
            "grid_import",
            points["grid_import"].id,
        )
        self.assertEqual(importer.diagnostic_status["last_result"], "completed")
        self.assertEqual(
            importer._fetch_hourly_energy.await_args_list[-1].args[0],
            today - history.timedelta(days=history.REFRESH_DAYS - 1),
        )
        self.assertEqual(
            recorder_instance.async_clear_statistics.call_count,
            1,
        )
        for role_key, point in points.items():
            self.assertTrue(
                state.is_complete(
                    derived_history.statistic_id_for_role(
                        role_key,
                        point.id,
                    ),
                    derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
                )
            )

    def test_single_entry_adopts_valid_hourly_derived_history(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        existing_start = datetime.combine(
            today - history.timedelta(days=4),
            datetime.min.time().replace(hour=12),
            tzinfo=local_tz,
        ).astimezone(timezone.utc)
        state = _HistoryState()
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            {
                "grid_import": types.SimpleNamespace(
                    id="grid",
                    name="Bezug",
                )
            },
            history_state=state,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[
                {
                    "start": existing_start.timestamp(),
                    "state": 1.0,
                    "sum": 10.0,
                }
            ]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=({"grid_import": []}, True)
        )
        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(),
        )

        with patch.object(
            derived_history,
            "get_instance",
            return_value=recorder_instance,
        ):
            asyncio.run(importer.async_import())

        statistic_id = derived_history.statistic_id_for_role(
            "grid_import",
            "grid",
        )
        self.assertEqual(
            importer._fetch_hourly_energy.await_args.args[0],
            today - history.timedelta(days=history.REFRESH_DAYS - 1),
        )
        recorder_instance.async_clear_statistics.assert_not_called()
        self.assertTrue(
            state.is_complete(
                statistic_id,
                derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
            )
        )

    def test_forced_derived_rebuild_keeps_legacy_data_on_incomplete_fetch(
        self,
    ) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        hour_start = datetime.now(timezone.utc).replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        state = _HistoryState()
        importer = derived_history.Smart1DerivedEnergyImporter(
            types.SimpleNamespace(
                config=types.SimpleNamespace(time_zone="Europe/Berlin")
            ),
            types.SimpleNamespace(),
            {
                "grid_import": types.SimpleNamespace(
                    id="grid",
                    name="Bezug",
                )
            },
            history_state=state,
            force_initial_rebuild=True,
        )
        importer._existing_statistics = AsyncMock(
            return_value=[
                {
                    "start": hour_start.timestamp(),
                    "state": 99.0,
                    "sum": 99.0,
                }
            ]
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=({"grid_import": []}, False)
        )
        recorder_instance = types.SimpleNamespace(
            async_clear_statistics=Mock(),
            async_block_till_done=AsyncMock(),
        )

        with (
            patch.object(
                derived_history,
                "get_instance",
                return_value=recorder_instance,
            ),
            patch.object(
                derived_history,
                "async_add_external_statistics",
            ) as add_stats,
        ):
            asyncio.run(importer.async_import())

        statistic_id = derived_history.statistic_id_for_role(
            "grid_import",
            "grid",
        )
        importer._fetch_hourly_energy.assert_awaited_once_with(
            today - history.timedelta(days=history.HISTORY_DAYS - 1),
            today,
            local_tz,
        )
        recorder_instance.async_clear_statistics.assert_not_called()
        add_stats.assert_not_called()
        self.assertFalse(
            state.is_complete(
                statistic_id,
                derived_history.DERIVED_HISTORY_SCHEMA_VERSION,
            )
        )

    def test_current_day_derived_refresh_skips_uninitialized_role(
        self,
    ) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(local_tz).date()
        hour_start = datetime.combine(
            today,
            datetime.min.time(),
            tzinfo=local_tz,
        ).replace(hour=1).astimezone(timezone.utc)
        existing_record = {
            "start": hour_start.timestamp(),
            "state": 0.25,
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
                ),
                "wallbox_consumption": types.SimpleNamespace(
                    id="wallbox",
                    name="ECar Laden",
                ),
            },
        )
        importer._existing_statistics = AsyncMock(
            side_effect=([existing_record], []),
        )
        importer._fetch_hourly_energy = AsyncMock(
            return_value=(
                {"grid_import": [(hour_start, 0.5)]},
                True,
            )
        )

        with patch.object(
            derived_history,
            "async_add_external_statistics",
        ) as add_statistics:
            asyncio.run(importer.async_import(1, repair=False))

        importer._fetch_hourly_energy.assert_awaited_once_with(
            today,
            today,
            local_tz,
        )
        add_statistics.assert_called_once()
        self.assertEqual(
            add_statistics.call_args.kwargs["metadata"]["statistic_id"],
            derived_history.statistic_id_for_role("grid_import", "grid"),
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

    def test_hourly_merge_zeroes_stale_hour_for_replaced_day(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        noon = datetime(2026, 8, 3, 12, tzinfo=local_tz).astimezone(
            timezone.utc
        )
        one_pm = datetime(2026, 8, 3, 13, tzinfo=local_tz).astimezone(
            timezone.utc
        )

        merged = derived_history.merge_hourly_energy(
            [
                {"start": noon.timestamp(), "state": 4.0, "sum": 4.0},
                {"start": one_pm.timestamp(), "state": 6.0, "sum": 10.0},
            ],
            [(noon, 5.0)],
            date(2026, 8, 3),
            date(2026, 8, 3),
            local_tz,
        )
        statistics_result = derived_history.build_hourly_energy_statistics(
            merged,
            0.0,
        )

        self.assertEqual(merged, [(noon, 5.0), (one_pm, 0.0)])
        self.assertEqual(statistics_result[-1]["sum"], 5.0)

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
