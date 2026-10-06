from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).parents[1]

aiohttp = sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
if not hasattr(aiohttp, "ClientError"):
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

recorder = sys.modules.setdefault(
    "homeassistant.components.recorder",
    types.ModuleType("homeassistant.components.recorder"),
)
recorder.__path__ = []
if not hasattr(recorder, "get_instance"):
    recorder.get_instance = lambda hass: None


class StatisticData(dict):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)


class StatisticMetaData(dict):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)


models = sys.modules.setdefault(
    "homeassistant.components.recorder.models",
    types.ModuleType("homeassistant.components.recorder.models"),
)
models.StatisticData = getattr(models, "StatisticData", StatisticData)
models.StatisticMetaData = getattr(
    models,
    "StatisticMetaData",
    StatisticMetaData,
)
mean_type = getattr(models, "StatisticMeanType", types.SimpleNamespace())
mean_type.NONE = getattr(mean_type, "NONE", "none")
mean_type.ARITHMETIC = "arithmetic"
models.StatisticMeanType = mean_type

statistics = sys.modules.setdefault(
    "homeassistant.components.recorder.statistics",
    types.ModuleType("homeassistant.components.recorder.statistics"),
)
if not hasattr(statistics, "async_add_external_statistics"):
    statistics.async_add_external_statistics = lambda *args, **kwargs: None
if not hasattr(statistics, "get_last_statistics"):
    statistics.get_last_statistics = lambda *args, **kwargs: {}

const = sys.modules.setdefault(
    "homeassistant.const",
    types.ModuleType("homeassistant.const"),
)
const.UnitOfPower = types.SimpleNamespace(WATT="W")

core = sys.modules.setdefault(
    "homeassistant.core",
    types.ModuleType("homeassistant.core"),
)
core.HomeAssistant = getattr(core, "HomeAssistant", object)

util = sys.modules.setdefault(
    "homeassistant.util",
    types.ModuleType("homeassistant.util"),
)
util.__path__ = []
unit_conversion = sys.modules.setdefault(
    "homeassistant.util.unit_conversion",
    types.ModuleType("homeassistant.util.unit_conversion"),
)
unit_conversion.PowerConverter = types.SimpleNamespace(UNIT_CLASS="power")

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

power_history = importlib.import_module(
    "custom_components.smart1_ems.power_history"
)
point_module = importlib.import_module("custom_components.smart1_ems.point")
Smart1Point = point_module.Smart1Point


def point(point_id: str, name: str | None = None) -> Smart1Point:
    return Smart1Point(
        id=point_id,
        name=name or point_id,
        type="energy",
        source="counter",
    )


def rows_for_day(
    target_date: date,
    values: Mapping[str, float],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    start = datetime.combine(target_date, datetime.min.time())
    for offset in range(0, 24 * 60, 5):
        timestamp = start + timedelta(minutes=offset)
        for linear_id, value in values.items():
            rows.append(
                {
                    "LinearId": linear_id,
                    "Timestamp": timestamp.isoformat(sep=" "),
                    "Value1": str(value),
                }
            )
    return rows


class FakeApi:
    def __init__(self, values: Mapping[str, float]) -> None:
        self.values = dict(values)
        self.calls: list[tuple[tuple[str, ...], date, bool]] = []

    async def get_linear_detailed_rows(
        self,
        linear_ids: list[str],
        *,
        target_date: date,
        missing_ok: bool,
    ) -> list[dict[str, str]]:
        self.calls.append((tuple(linear_ids), target_date, missing_ok))
        return rows_for_day(
            target_date,
            {
                linear_id: self.values[linear_id]
                for linear_id in linear_ids
            },
        )


class FakeProgress:
    def __init__(
        self,
        *,
        checked_through: date,
        has_data: bool,
        source_time_zone: str,
        oldest_supported: date,
        incomplete_days: set[date] | None = None,
        complete_days: set[date] | None = None,
    ) -> None:
        self.checked_through = checked_through
        self.has_data = has_data
        self.source_time_zone = source_time_zone
        self.oldest_supported = oldest_supported
        self.incomplete_days = set(incomplete_days or ())
        self.complete_days = set(complete_days or ())


class FakeHistoryState:
    """Small state double preserving the importer's generation semantics."""

    def __init__(self) -> None:
        self.versions: dict[str, int] = {}
        self.presence: dict[tuple[str, int], bool] = {}
        self.coverage: dict[tuple[str, int], date] = {}
        self.zones: dict[str, str] = {}
        self.progress: dict[tuple[str, int], FakeProgress] = {}
        self.empty_days: dict[tuple[str, int], set[date]] = {}
        self.complete_days: dict[tuple[str, int], set[date]] = {}
        self.retry_runs: dict[tuple[str, int], date] = {}

    def is_complete(self, statistic_id: str, schema_version: int) -> bool:
        return self.versions.get(statistic_id, 0) >= schema_version

    def current_schema_version(self, statistic_id: str) -> int:
        return self.versions.get(statistic_id, 0)

    def data_presence(
        self,
        statistic_id: str,
        schema_version: int,
    ) -> bool | None:
        return self.presence.get((statistic_id, schema_version))

    def source_time_zone(self, statistic_id: str) -> str | None:
        return self.zones.get(statistic_id)

    def checked_through(
        self,
        statistic_id: str,
        schema_version: int,
    ) -> date | None:
        return self.coverage.get((statistic_id, schema_version))

    def incomplete_scan_progress(
        self,
        statistic_id: str,
        schema_version: int,
    ) -> FakeProgress | None:
        return self.progress.get((statistic_id, schema_version))

    def forget_statistics(self, statistic_ids: set[str]) -> None:
        for statistic_id in statistic_ids:
            self.versions.pop(statistic_id, None)
            self.zones.pop(statistic_id, None)
            for store in (
                self.presence,
                self.coverage,
                self.progress,
                self.empty_days,
                self.complete_days,
                self.retry_runs,
            ):
                for key in tuple(store):
                    if key[0] == statistic_id:
                        del store[key]

    def record_incomplete_scan_if_unchanged(
        self,
        statistic_id: str,
        schema_version: int,
        *,
        expected_version: int,
        checked_through: date,
        has_data: bool,
        oldest_supported: date,
        source_time_zone: str,
        incomplete_days: set[date] | None = None,
        complete_days: set[date] | None = None,
    ) -> bool:
        if self.versions.get(statistic_id, 0) != expected_version:
            return False
        key = (statistic_id, schema_version)
        prior = self.progress.get(key)
        accumulated_incomplete = set(incomplete_days or ())
        accumulated_complete = set(complete_days or ())
        if prior is not None and prior.source_time_zone == source_time_zone:
            checked_through = max(checked_through, prior.checked_through)
            has_data = has_data or prior.has_data
            oldest_supported = min(oldest_supported, prior.oldest_supported)
            accumulated_incomplete.update(prior.incomplete_days)
            accumulated_complete.update(prior.complete_days)
        accumulated_incomplete.difference_update(accumulated_complete)
        self.progress[key] = FakeProgress(
            checked_through=checked_through,
            has_data=has_data,
            source_time_zone=source_time_zone,
            oldest_supported=oldest_supported,
            incomplete_days=accumulated_incomplete,
            complete_days=accumulated_complete,
        )
        return True

    async def async_commit_scan_if_unchanged(
        self,
        statistic_id: str,
        schema_version: int,
        *,
        has_data: bool,
        expected_version: int,
        checked_through: date | None,
        source_time_zone: str,
        empty_days: set[date] | None = None,
        nonempty_days: set[date] | None = None,
        retried_day: date | None = None,
        checked_on: date | None = None,
        **_kwargs,
    ) -> bool:
        if self.versions.get(statistic_id, 0) != expected_version:
            return False
        key = (statistic_id, schema_version)
        prior = self.progress.pop(key, None)
        effective_empty = set(empty_days or ())
        effective_complete = set(nonempty_days or ())
        if prior is not None:
            has_data = has_data or prior.has_data
            if (
                checked_through is None
                or prior.checked_through > checked_through
            ):
                checked_through = prior.checked_through
            effective_empty.update(prior.incomplete_days)
            effective_complete.update(prior.complete_days)
        effective_empty.difference_update(effective_complete)
        if retried_day is not None:
            effective_empty.discard(retried_day)
            if checked_on is not None:
                self.retry_runs[key] = checked_on
        self.versions[statistic_id] = schema_version
        self.presence[key] = has_data
        prior_coverage = self.coverage.get(key)
        if checked_through is not None and (
            prior_coverage is None or checked_through > prior_coverage
        ):
            self.coverage[key] = checked_through
        self.zones[statistic_id] = source_time_zone
        self.empty_days[key] = effective_empty
        self.complete_days[key] = effective_complete
        return True

    def next_empty_retry_date(
        self,
        statistic_id: str,
        schema_version: int,
        *,
        oldest_supported: date,
        before: date,
        today: date,
    ) -> date | None:
        key = (statistic_id, schema_version)
        if self.retry_runs.get(key) == today:
            return None
        return next(
            (
                target_date
                for target_date in sorted(self.empty_days.get(key, set()))
                if oldest_supported <= target_date < before
            ),
            None,
        )


class FakeRecorder:
    async def async_block_till_done(self) -> None:
        return None

    async def async_add_executor_job(self, target, *args):
        return target(*args)


class FakeHass:
    def __init__(self, time_zone_name: str = "UTC") -> None:
        self.config = types.SimpleNamespace(time_zone=time_zone_name)

    async def async_add_executor_job(self, target, *args):
        return target(*args)


class FakeStatisticsStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[datetime, float]] = {}
        self.writes: list[tuple[dict, list[dict]]] = []

    def seed(self, statistic_id: str, start: datetime, mean: float) -> None:
        self.rows.setdefault(statistic_id, {})[start] = mean

    def write(self, metadata: dict, statistics_batch: list[dict]) -> None:
        copied = [dict(statistic) for statistic in statistics_batch]
        self.writes.append((dict(metadata), copied))
        statistic_id = metadata["statistic_id"]
        rows = self.rows.setdefault(statistic_id, {})
        for statistic in copied:
            rows[statistic["start"]] = statistic["mean"]

    async def read(
        self,
        statistic_id: str,
        record_count: int,
    ) -> list[Mapping[str, object]]:
        rows = sorted(self.rows.get(statistic_id, {}).items())[-record_count:]
        return [
            {"start": start, "mean": mean}
            for start, mean in rows
        ]

    async def persisted(
        self,
        statistic_id: str,
        statistics_batch: list[dict],
    ) -> bool:
        rows = self.rows.get(statistic_id, {})
        return all(
            rows.get(statistic["start"]) == statistic["mean"]
            for statistic in statistics_batch
        )


FetchFactory = Callable[
    [date, date, ZoneInfo, datetime, int],
    power_history.PowerHistoryFetchResult,
]


class ScriptedFetchImporter(power_history.Smart1PowerHistoryImporter):
    def __init__(self, *args, fetch_factory: FetchFactory, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._fetch_factory = fetch_factory
        self.windows: list[tuple[date, date]] = []

    async def _fetch_hourly_means(
        self,
        start_date: date,
        end_date: date,
        local_tz: ZoneInfo,
        *,
        completed_before: datetime,
        include_following_boundary: bool = False,
    ) -> power_history.PowerHistoryFetchResult:
        del include_following_boundary
        self.windows.append((start_date, end_date))
        return self._fetch_factory(
            start_date,
            end_date,
            local_tz,
            completed_before,
            len(self.windows),
        )


def successful_fetch(
    importer: power_history.Smart1PowerHistoryImporter,
    *,
    emit: bool = True,
) -> FetchFactory:
    def build(
        start_date: date,
        end_date: date,
        _local_tz: ZoneInfo,
        completed_before: datetime,
        _call_number: int,
    ) -> power_history.PowerHistoryFetchResult:
        start = completed_before.astimezone(timezone.utc).replace(
            minute=0,
            second=0,
            microsecond=0,
        ) - timedelta(hours=1)
        means = {
            channel.key: ((start, 100.0),) if emit else ()
            for channel in importer.channels
        }
        dates = frozenset(
            start_date + timedelta(days=offset)
            for offset in range((end_date - start_date).days + 1)
        )
        return power_history.PowerHistoryFetchResult(
            hourly_means=means,
            completed=True,
            checked_through=end_date,
            requested_days=(end_date - start_date).days + 2,
            emitted_hours=sum(len(value) for value in means.values()),
            replaceable_hours=(start,),
            incomplete_days={
                channel.key: frozenset() if emit else dates
                for channel in importer.channels
            },
            complete_days={
                channel.key: dates if emit else frozenset()
                for channel in importer.channels
            },
        )

    return build


def mark_complete(
    state: FakeHistoryState,
    importer: power_history.Smart1PowerHistoryImporter,
    *,
    has_data: bool,
    checked_through: date = date(2026, 10, 5),
) -> None:
    for channel in importer.channels:
        state.versions[channel.statistic_id] = (
            power_history.POWER_HISTORY_SCHEMA_VERSION
        )
        state.presence[
            (
                channel.statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            )
        ] = has_data
        state.coverage[
            (
                channel.statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            )
        ] = checked_through
        state.zones[channel.statistic_id] = (
            importer._configured_source_time_zone
        )


class PowerHistoryPureTest(unittest.TestCase):
    def test_builds_only_complete_channels_with_stable_source_ids(self) -> None:
        pv = point("pv")
        roles = {
            "grid_import": point("import"),
            "grid_export": point("export"),
            "battery_discharge": point("discharge"),
            "battery_charge": point("charge"),
        }

        channels = power_history.build_power_history_channels(
            pv,
            roles,
            statistics_namespace="plant",
        )

        self.assertEqual(
            [channel.key for channel in channels],
            ["pv", "grid", "battery"],
        )
        self.assertTrue(
            all(
                channel.statistic_id.startswith("smart1_ems:")
                and channel.statistic_id.endswith("_plant")
                for channel in channels
            )
        )
        incomplete = power_history.build_power_history_channels(
            None,
            {"grid_import": roles["grid_import"]},
        )
        self.assertEqual(incomplete, ())

    def test_time_zone_is_part_of_non_destructive_statistic_identity(
        self,
    ) -> None:
        utc = power_history.build_power_history_channels(
            point("pv"),
            {},
            statistics_namespace="plant",
            source_time_zone="UTC",
        )[0]
        berlin = power_history.build_power_history_channels(
            point("pv"),
            {},
            statistics_namespace="plant",
            source_time_zone="Europe/Berlin",
        )[0]

        self.assertNotEqual(utc.statistic_id, berlin.statistic_id)
        self.assertEqual(utc.name, "smart1 EMS PV power (UTC)")
        self.assertEqual(
            berlin.name,
            "smart1 EMS PV power (Europe/Berlin)",
        )

    def test_emits_only_fully_covered_completed_utc_hours(self) -> None:
        samples = tuple(
            (
                datetime(2026, 10, 5, tzinfo=timezone.utc)
                + timedelta(minutes=offset),
                1000.0,
            )
            for offset in range(0, 125, 5)
        )

        means = power_history.complete_hourly_power_means(
            samples,
            completed_before=datetime(
                2026,
                10,
                5,
                1,
                30,
                tzinfo=timezone.utc,
            ),
        )

        self.assertEqual(
            means,
            {datetime(2026, 10, 5, tzinfo=timezone.utc): 1000.0},
        )

    def test_rejects_an_hour_with_a_large_internal_gap(self) -> None:
        samples = tuple(
            (
                datetime(2026, 10, 5, tzinfo=timezone.utc)
                + timedelta(minutes=offset),
                1000.0,
            )
            for offset in (*range(0, 30, 5), *range(50, 65, 5))
        )

        means = power_history.complete_hourly_power_means(
            samples,
            completed_before=datetime(
                2026,
                10,
                5,
                2,
                tzinfo=timezone.utc,
            ),
        )

        self.assertEqual(means, {})

    def test_ignores_invalid_recorder_timestamps(self) -> None:
        invalid_starts = (
            None,
            "not-a-timestamp",
            float("nan"),
            float("inf"),
            float("-inf"),
            1e100,
            10**1000,
            datetime.min.replace(
                tzinfo=timezone(timedelta(hours=14))
            ),
            datetime.max.replace(
                tzinfo=timezone(-timedelta(hours=14))
            ),
        )

        for raw_start in invalid_starts:
            with self.subTest(raw_start=raw_start):
                self.assertIsNone(
                    power_history.Smart1PowerHistoryImporter._record_start(
                        {"start": raw_start}
                    )
                )

    def test_saturated_read_requires_an_old_boundary_witness(self) -> None:
        supported_start = datetime(2025, 10, 5, 23, tzinfo=timezone.utc)
        newer = {"start": supported_start + timedelta(days=1)}
        older = {"start": supported_start - timedelta(seconds=1)}
        corrupt = {"start": float("inf")}
        importer_type = power_history.Smart1PowerHistoryImporter
        classify = importer_type._saturated_read_may_hide_supported_rows

        self.assertTrue(
            classify(
                [newer, corrupt],
                record_limit=2,
                supported_start=supported_start,
            )
        )
        self.assertFalse(
            classify(
                [newer, older],
                record_limit=2,
                supported_start=supported_start,
            )
        )
        self.assertFalse(
            classify(
                [corrupt, older],
                record_limit=2,
                supported_start=supported_start,
            )
        )
        self.assertFalse(
            classify(
                [newer],
                record_limit=2,
                supported_start=supported_start,
            )
        )


class PowerHistoryImporterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.recorder = FakeRecorder()
        self.get_instance = patch.object(
            power_history,
            "get_instance",
            return_value=self.recorder,
        )
        self.get_instance.start()
        self.addCleanup(self.get_instance.stop)

    async def test_fetches_each_source_day_once_and_signs_composites(self) -> None:
        api = FakeApi(
            {
                "pv": 1000.0,
                "import": 300.0,
                "export": 100.0,
                "discharge": 50.0,
                "charge": 200.0,
            }
        )
        importer = power_history.Smart1PowerHistoryImporter(
            FakeHass(),
            api,
            pv_power_point=point("pv"),
            role_points={
                "grid_import": point("import"),
                "grid_export": point("export"),
                "battery_discharge": point("discharge"),
                "battery_charge": point("charge"),
            },
        )

        result = await importer._fetch_hourly_means(
            date(2026, 1, 2),
            date(2026, 1, 3),
            ZoneInfo("UTC"),
            completed_before=datetime(2026, 1, 4, tzinfo=timezone.utc),
        )

        self.assertTrue(result.completed)
        self.assertEqual(result.requested_days, 3)
        self.assertEqual(len(api.calls), 3)
        self.assertTrue(all(call[2] for call in api.calls))
        self.assertTrue(
            all(set(call[0]) == set(api.values) for call in api.calls)
        )
        # The extra first bucket is the exact cross-midnight interval from the
        # preceding source day; the final source-day hour remains incomplete.
        self.assertEqual(len(result.hourly_means["pv"]), 48)
        self.assertEqual(len(result.hourly_means["grid"]), 48)
        self.assertEqual(len(result.hourly_means["battery"]), 48)
        self.assertAlmostEqual(result.hourly_means["pv"][0][1], 1000.0)
        self.assertAlmostEqual(result.hourly_means["grid"][0][1], 200.0)
        self.assertAlmostEqual(
            result.hourly_means["battery"][0][1],
            -150.0,
        )
        self.assertEqual(
            result.hourly_means["pv"][0][0],
            datetime(2026, 1, 1, 23, tzinfo=timezone.utc),
        )
        self.assertEqual(
            result.hourly_means["pv"][-1][0],
            datetime(2026, 1, 3, 22, tzinfo=timezone.utc),
        )

    async def test_preceding_day_completes_fractional_offset_hour(self) -> None:
        api = FakeApi({"pv": 1000.0})
        importer = power_history.Smart1PowerHistoryImporter(
            FakeHass("Asia/Kathmandu"),
            api,
            pv_power_point=point("pv"),
            role_points={},
        )

        result = await importer._fetch_hourly_means(
            date(2026, 1, 2),
            date(2026, 1, 2),
            ZoneInfo("Asia/Kathmandu"),
            completed_before=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )

        self.assertEqual(len(api.calls), 2)
        self.assertEqual(len(result.hourly_means["pv"]), 24)
        self.assertEqual(
            result.hourly_means["pv"][0],
            (datetime(2026, 1, 1, 18, tzinfo=timezone.utc), 1000.0),
        )

    async def test_exact_local_midnight_boundary_hour_is_included(self) -> None:
        importer = power_history.Smart1PowerHistoryImporter(
            FakeHass("UTC"),
            FakeApi({"pv": 1000.0}),
            pv_power_point=point("pv"),
            role_points={},
        )

        result = await importer._fetch_hourly_means(
            date(2026, 1, 2),
            date(2026, 1, 2),
            ZoneInfo("UTC"),
            completed_before=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )

        starts = {start for start, _mean in result.hourly_means["pv"]}
        self.assertIn(datetime(2026, 1, 1, 23, tzinfo=timezone.utc), starts)
        self.assertIn(
            datetime(2026, 1, 1, 23, tzinfo=timezone.utc),
            result.replaceable_hours,
        )
        self.assertEqual(len(starts), 24)

    async def test_open_source_day_stays_incomplete_until_next_day(
        self,
    ) -> None:
        target_date = date(2026, 1, 2)
        importer = power_history.Smart1PowerHistoryImporter(
            FakeHass("UTC"),
            FakeApi({"pv": 1000.0}),
            pv_power_point=point("pv"),
            role_points={},
        )

        midday = await importer._fetch_hourly_means(
            target_date,
            target_date,
            ZoneInfo("UTC"),
            completed_before=datetime(
                2026,
                1,
                2,
                12,
                tzinfo=timezone.utc,
            ),
        )
        next_day = await importer._fetch_hourly_means(
            target_date,
            target_date,
            ZoneInfo("UTC"),
            completed_before=datetime(
                2026,
                1,
                3,
                12,
                tzinfo=timezone.utc,
            ),
        )

        self.assertIn(target_date, midday.incomplete_days["pv"])
        self.assertNotIn(target_date, midday.complete_days["pv"])
        self.assertIn(target_date, next_day.complete_days["pv"])
        self.assertNotIn(target_date, next_day.incomplete_days["pv"])

    async def test_failed_following_day_keeps_last_scanned_day_incomplete(
        self,
    ) -> None:
        target_date = date(2026, 1, 2)
        failed_date = target_date + timedelta(days=1)

        class FailingBoundaryApi(FakeApi):
            async def get_linear_detailed_rows(
                self,
                linear_ids: list[str],
                *,
                target_date: date,
                missing_ok: bool,
            ) -> list[dict[str, str]]:
                if target_date == failed_date:
                    self.calls.append(
                        (tuple(linear_ids), target_date, missing_ok)
                    )
                    raise aiohttp.ClientError("temporary failure")
                return await super().get_linear_detailed_rows(
                    linear_ids,
                    target_date=target_date,
                    missing_ok=missing_ok,
                )

        importer = power_history.Smart1PowerHistoryImporter(
            FakeHass("UTC"),
            FailingBoundaryApi({"pv": 1000.0}),
            pv_power_point=point("pv"),
            role_points={},
        )

        with patch.object(
            power_history.asyncio,
            "sleep",
            new=AsyncMock(),
        ):
            result = await importer._fetch_hourly_means(
                target_date,
                failed_date,
                ZoneInfo("UTC"),
                completed_before=datetime(
                    2026,
                    1,
                    4,
                    tzinfo=timezone.utc,
                ),
            )

        self.assertFalse(result.completed)
        self.assertEqual(result.checked_through, target_date)
        self.assertNotIn(
            datetime(2026, 1, 2, 23, tzinfo=timezone.utc),
            dict(result.hourly_means["pv"]),
        )
        self.assertEqual(
            result.incomplete_days["pv"],
            frozenset({target_date}),
        )
        self.assertEqual(result.complete_days["pv"], frozenset())

    async def test_spring_dst_day_keeps_all_23_real_hours(self) -> None:
        target_date = date(2026, 3, 29)
        importer = power_history.Smart1PowerHistoryImporter(
            FakeHass("Europe/Berlin"),
            FakeApi({"pv": 1000.0}),
            pv_power_point=point("pv"),
            role_points={},
        )

        result = await importer._fetch_hourly_means(
            target_date,
            target_date,
            ZoneInfo("Europe/Berlin"),
            completed_before=datetime(2026, 3, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(len(result.replaceable_hours), 23)
        self.assertEqual(len(result.hourly_means["pv"]), 23)
        self.assertEqual(result.incomplete_days["pv"], frozenset())
        self.assertEqual(
            result.complete_days["pv"],
            frozenset({target_date}),
        )

    async def test_fall_dst_day_without_second_fold_is_not_marked_complete(
        self,
    ) -> None:
        target_date = date(2026, 10, 25)
        importer = power_history.Smart1PowerHistoryImporter(
            FakeHass("Europe/Berlin"),
            FakeApi({"pv": 1000.0}),
            pv_power_point=point("pv"),
            role_points={},
        )

        result = await importer._fetch_hourly_means(
            target_date,
            target_date,
            ZoneInfo("Europe/Berlin"),
            completed_before=datetime(2026, 10, 26, tzinfo=timezone.utc),
        )

        self.assertEqual(len(result.replaceable_hours), 25)
        self.assertLess(len(result.hourly_means["pv"]), 25)
        self.assertEqual(
            result.incomplete_days["pv"],
            frozenset({target_date}),
        )
        self.assertEqual(result.complete_days["pv"], frozenset())

    async def test_composite_channel_is_incomplete_when_one_term_has_gap(
        self,
    ) -> None:
        target_date = date(2026, 1, 2)

        class GapApi(FakeApi):
            async def get_linear_detailed_rows(
                self,
                linear_ids: list[str],
                *,
                target_date: date,
                missing_ok: bool,
            ) -> list[dict[str, str]]:
                rows = await super().get_linear_detailed_rows(
                    linear_ids,
                    target_date=target_date,
                    missing_ok=missing_ok,
                )
                return [
                    row
                    for row in rows
                    if not (
                        row["LinearId"] == "export"
                        and " 10:20:00" <= row["Timestamp"][-9:]
                        <= " 10:40:00"
                    )
                ]

        importer = power_history.Smart1PowerHistoryImporter(
            FakeHass("UTC"),
            GapApi({"import": 300.0, "export": 100.0}),
            pv_power_point=None,
            role_points={
                "grid_import": point("import"),
                "grid_export": point("export"),
            },
        )

        result = await importer._fetch_hourly_means(
            target_date,
            target_date,
            ZoneInfo("UTC"),
            completed_before=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )

        starts = {start for start, _mean in result.hourly_means["grid"]}
        self.assertNotIn(
            datetime(2026, 1, 2, 10, tzinfo=timezone.utc),
            starts,
        )
        self.assertEqual(
            result.incomplete_days["grid"],
            frozenset({target_date}),
        )

    async def test_uses_365_days_initially_then_three_days(self) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass("Europe/Berlin"),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={
                "grid_import": point("import"),
                "grid_export": point("export"),
                "battery_discharge": point("discharge"),
                "battery_charge": point("charge"),
            },
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(importer)(*args),
        )
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

        await importer.async_import(now=now)
        await importer.async_import(now=now)

        today = now.astimezone(ZoneInfo("Europe/Berlin")).date()
        self.assertEqual(
            importer.windows,
            [
                (today - timedelta(days=364), today),
                (today - timedelta(days=2), today),
            ],
        )
        self.assertEqual(len(store.writes), 6)
        self.assertEqual(len(state.versions), 3)
        self.assertTrue(
            all(
                version == power_history.POWER_HISTORY_SCHEMA_VERSION
                for version in state.versions.values()
            )
        )
        self.assertTrue(all(state.presence.values()))
        self.assertEqual(set(state.zones.values()), {"Europe/Berlin"})
        for metadata, statistics_batch in store.writes:
            self.assertEqual(metadata["mean_type"], "arithmetic")
            self.assertFalse(metadata["has_sum"])
            self.assertEqual(metadata["unit_class"], "power")
            self.assertEqual(metadata["unit_of_measurement"], "W")
            self.assertIn("mean", statistics_batch[0])

    async def test_repair_catches_up_from_last_verified_day_after_outage(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass("Europe/Berlin"),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(importer)(*args),
        )
        last_verified = date(2026, 9, 25)
        mark_complete(
            state,
            importer,
            has_data=False,
            checked_through=last_verified,
        )
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

        await importer.async_import(now=now)

        self.assertEqual(
            importer.windows,
            [(last_verified + timedelta(days=1), date(2026, 10, 5))],
        )
        self.assertEqual(
            importer.diagnostic_status["last_mode"],
            "catch_up",
        )
        statistic_id = importer.channels[0].statistic_id
        self.assertEqual(
            state.checked_through(
                statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            ),
            date(2026, 10, 5),
        )

    async def test_hourly_refresh_does_not_skip_unchecked_coverage_gap(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(importer)(*args),
        )
        last_verified = date(2026, 10, 5)
        mark_complete(
            state,
            importer,
            has_data=False,
            checked_through=last_verified,
        )
        now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
        statistic_id = importer.channels[0].statistic_id

        # The hourly timer refreshes only today. It must not claim that the
        # unqueried dates between durable coverage and today were checked.
        await importer.async_import(1, repair=False, now=now)

        self.assertEqual(
            importer.windows,
            [(date(2026, 10, 12), date(2026, 10, 12))],
        )
        self.assertEqual(
            state.checked_through(
                statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            ),
            last_verified,
        )

        # The scheduled repair must then join that prefix and query every
        # missing day, rather than limiting itself to its three-day refresh.
        await importer.async_import(3, repair=True, now=now)

        self.assertEqual(
            importer.windows,
            [
                (date(2026, 10, 12), date(2026, 10, 12)),
                (date(2026, 10, 6), date(2026, 10, 12)),
            ],
        )
        self.assertEqual(
            state.checked_through(
                statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            ),
            date(2026, 10, 12),
        )

    async def test_successful_gap_refresh_then_rollback_rebuilds_window(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(importer)(*args),
        )
        mark_complete(
            state,
            importer,
            has_data=False,
            checked_through=date(2026, 10, 5),
        )
        statistic_id = importer.channels[0].statistic_id
        key = (statistic_id, power_history.POWER_HISTORY_SCHEMA_VERSION)

        await importer.async_import(
            1,
            repair=False,
            now=datetime(2026, 10, 12, 12, tzinfo=timezone.utc),
        )

        progress = state.incomplete_scan_progress(*key)
        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.checked_through, date(2026, 10, 5))
        self.assertEqual(progress.oldest_supported, date(2025, 10, 13))

        # Before the ordinary catch-up can run, the clock moves backward.
        # The persisted old window boundary must invalidate the prefix and
        # expose the newly supported 2025-10-06..12 dates to a full scan.
        await importer.async_import(
            3,
            repair=True,
            now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(
            importer.windows,
            [
                (date(2026, 10, 12), date(2026, 10, 12)),
                (date(2025, 10, 6), date(2026, 10, 5)),
            ],
        )
        self.assertIsNone(state.incomplete_scan_progress(*key))
        self.assertEqual(state.checked_through(*key), date(2026, 10, 5))

    async def test_partial_refresh_with_expired_prefix_forces_full_repair(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter

        def fetch(
            start_date: date,
            end_date: date,
            local_tz: ZoneInfo,
            completed_before: datetime,
            call_number: int,
        ) -> power_history.PowerHistoryFetchResult:
            if call_number > 1:
                return successful_fetch(importer)(
                    start_date,
                    end_date,
                    local_tz,
                    completed_before,
                    call_number,
                )
            start = completed_before.replace(
                minute=0,
                second=0,
                microsecond=0,
            ) - timedelta(hours=1)
            return power_history.PowerHistoryFetchResult(
                hourly_means={"pv": ((start, 100.0),)},
                completed=False,
                checked_through=end_date,
                requested_days=1,
                emitted_hours=1,
                replaceable_hours=(start,),
                incomplete_days={"pv": frozenset()},
                complete_days={"pv": frozenset({end_date})},
            )

        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=fetch,
        )
        mark_complete(
            state,
            importer,
            has_data=False,
            checked_through=date(2025, 10, 5),
        )
        statistic_id = importer.channels[0].statistic_id
        key = (statistic_id, power_history.POWER_HISTORY_SCHEMA_VERSION)
        now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)

        # The prefix has aged out of the 365-day support window. A partial
        # isolated refresh must not persist that invalid date as scan progress.
        await importer.async_import(1, repair=False, now=now)

        self.assertFalse(state.is_complete(*key))
        self.assertIsNone(state.incomplete_scan_progress(*key))
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "fetch_incomplete",
        )

        await importer.async_import(3, repair=True, now=now)

        self.assertEqual(
            importer.windows,
            [
                (date(2026, 10, 12), date(2026, 10, 12)),
                (date(2025, 10, 13), date(2026, 10, 12)),
            ],
        )
        self.assertTrue(state.is_complete(*key))
        self.assertEqual(state.checked_through(*key), now.date())

    async def test_partial_hourly_refresh_does_not_record_progress_past_gap(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter

        def fetch(
            start_date: date,
            end_date: date,
            local_tz: ZoneInfo,
            completed_before: datetime,
            call_number: int,
        ) -> power_history.PowerHistoryFetchResult:
            if call_number > 1:
                return successful_fetch(importer)(
                    start_date,
                    end_date,
                    local_tz,
                    completed_before,
                    call_number,
                )
            start = completed_before.replace(
                minute=0,
                second=0,
                microsecond=0,
            ) - timedelta(hours=1)
            return power_history.PowerHistoryFetchResult(
                hourly_means={"pv": ((start, 100.0),)},
                completed=False,
                checked_through=end_date,
                requested_days=1,
                emitted_hours=1,
                replaceable_hours=(start,),
                incomplete_days={"pv": frozenset()},
                complete_days={"pv": frozenset({end_date})},
            )

        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=fetch,
        )
        last_verified = date(2026, 10, 5)
        mark_complete(
            state,
            importer,
            has_data=False,
            checked_through=last_verified,
        )
        now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
        statistic_id = importer.channels[0].statistic_id
        key = (statistic_id, power_history.POWER_HISTORY_SCHEMA_VERSION)

        await importer.async_import(1, repair=False, now=now)

        progress = state.incomplete_scan_progress(*key)
        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.checked_through, last_verified)

        await importer.async_import(3, repair=True, now=now)

        self.assertEqual(
            importer.windows,
            [
                (date(2026, 10, 12), date(2026, 10, 12)),
                (date(2026, 10, 6), date(2026, 10, 12)),
            ],
        )
        self.assertIsNone(state.incomplete_scan_progress(*key))
        self.assertEqual(state.checked_through(*key), date(2026, 10, 12))

    async def test_partial_connected_refresh_does_not_regress_prefix(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter

        def fetch(
            _start_date: date,
            end_date: date,
            _local_tz: ZoneInfo,
            completed_before: datetime,
            _call_number: int,
        ) -> power_history.PowerHistoryFetchResult:
            start = completed_before.replace(
                minute=0,
                second=0,
                microsecond=0,
            ) - timedelta(hours=1)
            return power_history.PowerHistoryFetchResult(
                hourly_means={"pv": ((start, 100.0),)},
                completed=False,
                checked_through=end_date - timedelta(days=3),
                requested_days=1,
                emitted_hours=1,
                replaceable_hours=(start,),
                incomplete_days={"pv": frozenset()},
                complete_days={"pv": frozenset()},
            )

        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=fetch,
        )
        last_verified = date(2026, 10, 10)
        mark_complete(
            state,
            importer,
            has_data=False,
            checked_through=last_verified,
        )
        statistic_id = importer.channels[0].statistic_id
        key = (statistic_id, power_history.POWER_HISTORY_SCHEMA_VERSION)

        await importer.async_import(
            3,
            repair=True,
            now=datetime(2026, 10, 12, 12, tzinfo=timezone.utc),
        )

        progress = state.incomplete_scan_progress(*key)
        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.checked_through, last_verified)

    async def test_partial_refresh_with_mixed_prefixes_forces_full_repair(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter

        def fetch(
            start_date: date,
            end_date: date,
            local_tz: ZoneInfo,
            completed_before: datetime,
            call_number: int,
        ) -> power_history.PowerHistoryFetchResult:
            if call_number > 1:
                return successful_fetch(importer)(
                    start_date,
                    end_date,
                    local_tz,
                    completed_before,
                    call_number,
                )
            start = completed_before.replace(
                minute=0,
                second=0,
                microsecond=0,
            ) - timedelta(hours=1)
            return power_history.PowerHistoryFetchResult(
                hourly_means={
                    channel.key: ((start, 100.0),)
                    for channel in importer.channels
                },
                completed=False,
                checked_through=end_date,
                requested_days=1,
                emitted_hours=len(importer.channels),
                replaceable_hours=(start,),
                incomplete_days={
                    channel.key: frozenset()
                    for channel in importer.channels
                },
                complete_days={
                    channel.key: frozenset({end_date})
                    for channel in importer.channels
                },
            )

        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={
                "grid_import": point("import"),
                "grid_export": point("export"),
                "battery_discharge": point("discharge"),
                "battery_charge": point("charge"),
            },
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=fetch,
        )
        last_verified = date(2026, 10, 5)
        mark_complete(
            state,
            importer,
            has_data=False,
            checked_through=last_verified,
        )
        missing_prefix_channel = importer.channels[1]
        missing_prefix_key = (
            missing_prefix_channel.statistic_id,
            power_history.POWER_HISTORY_SCHEMA_VERSION,
        )
        del state.coverage[missing_prefix_key]
        now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)

        await importer.async_import(1, repair=False, now=now)

        self.assertTrue(
            all(
                state.incomplete_scan_progress(
                    channel.statistic_id,
                    power_history.POWER_HISTORY_SCHEMA_VERSION,
                )
                is None
                for channel in importer.channels
            )
        )
        self.assertTrue(
            all(
                not state.is_complete(
                    channel.statistic_id,
                    power_history.POWER_HISTORY_SCHEMA_VERSION,
                )
                for channel in importer.channels
            )
        )

        await importer.async_import(3, repair=True, now=now)

        self.assertEqual(
            importer.windows,
            [
                (date(2026, 10, 12), date(2026, 10, 12)),
                (date(2025, 10, 13), date(2026, 10, 12)),
            ],
        )
        self.assertEqual(importer.diagnostic_status["last_mode"], "initial")
        for channel in importer.channels:
            key = (
                channel.statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            )
            self.assertEqual(state.checked_through(*key), now.date())

    async def test_complete_refresh_with_mixed_prefixes_forces_full_repair(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={
                "grid_import": point("import"),
                "grid_export": point("export"),
                "battery_discharge": point("discharge"),
                "battery_charge": point("charge"),
            },
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(importer)(*args),
        )
        last_verified = date(2026, 10, 5)
        mark_complete(
            state,
            importer,
            has_data=False,
            checked_through=last_verified,
        )
        missing_prefix_channel = importer.channels[1]
        missing_prefix_key = (
            missing_prefix_channel.statistic_id,
            power_history.POWER_HISTORY_SCHEMA_VERSION,
        )
        del state.coverage[missing_prefix_key]
        now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)

        await importer.async_import(1, repair=False, now=now)

        self.assertTrue(
            all(
                not state.is_complete(
                    channel.statistic_id,
                    power_history.POWER_HISTORY_SCHEMA_VERSION,
                )
                for channel in importer.channels
            )
        )

        await importer.async_import(3, repair=True, now=now)

        self.assertEqual(
            importer.windows,
            [
                (date(2026, 10, 12), date(2026, 10, 12)),
                (date(2025, 10, 13), date(2026, 10, 12)),
            ],
        )
        self.assertEqual(importer.diagnostic_status["last_mode"], "initial")
        for channel in importer.channels:
            key = (
                channel.statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            )
            self.assertEqual(state.checked_through(*key), now.date())

    async def test_withholds_schema_marker_until_persistence_is_confirmed(
        self,
    ) -> None:
        state = FakeHistoryState()
        importer: ScriptedFetchImporter

        async def not_persisted(_statistic_id, _statistics) -> bool:
            return False

        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=lambda _metadata, _statistics: None,
            persistence_checker=not_persisted,
            statistics_reader=AsyncMock(return_value=[]),
            fetch_factory=lambda *args: successful_fetch(importer)(*args),
        )

        await importer.async_import(
            now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        )

        self.assertEqual(state.versions, {})
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "persistence_pending",
        )

    async def test_zone_change_uses_new_id_and_never_clears_old_rows(
        self,
    ) -> None:
        utc_store = FakeStatisticsStore()
        utc_importer = power_history.Smart1PowerHistoryImporter(
            FakeHass("UTC"),
            FakeApi({"pv": 1000.0}),
            pv_power_point=point("pv"),
            role_points={},
            statistics_namespace="plant",
            statistics_writer=utc_store.write,
            persistence_checker=utc_store.persisted,
            statistics_reader=utc_store.read,
        )
        old_id = utc_importer.channels[0].statistic_id
        old_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        utc_store.seed(old_id, old_start, 123.0)

        berlin = power_history.Smart1PowerHistoryImporter(
            FakeHass("Europe/Berlin"),
            FakeApi({"pv": 1000.0}),
            pv_power_point=point("pv"),
            role_points={},
            statistics_namespace="plant",
        )

        self.assertNotEqual(old_id, berlin.channels[0].statistic_id)
        self.assertEqual(utc_store.rows[old_id][old_start], 123.0)
        self.assertFalse(hasattr(power_history, "async_clear_statistics"))

    async def test_completed_marker_with_missing_rows_forces_full_repair(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(importer)(*args),
        )
        mark_complete(state, importer, has_data=True)
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

        await importer.async_import(now=now)

        self.assertEqual(
            importer.windows,
            [(date(2025, 10, 6), date(2026, 10, 5))],
        )
        self.assertEqual(importer.diagnostic_status["last_mode"], "repair")
        self.assertTrue(store.writes)

    async def test_corrupt_recorder_timestamp_does_not_abort_import(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        reader = AsyncMock(
            return_value=[
                {"start": float("inf"), "mean": 999.0},
                {
                    "start": datetime(
                        2026,
                        9,
                        1,
                        12,
                        tzinfo=timezone.utc,
                    ),
                    "mean": 420.0,
                },
            ]
        )
        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=reader,
            fetch_factory=lambda *args: successful_fetch(importer)(*args),
        )
        mark_complete(state, importer, has_data=True)
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

        await importer.async_import(1, repair=False, now=now)

        self.assertEqual(
            importer.windows,
            [(date(2026, 10, 5), date(2026, 10, 5))],
        )
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "completed",
        )

    async def test_empty_restore_repair_clears_stale_data_presence(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(
                importer,
                emit=False,
            )(*args),
        )
        mark_complete(state, importer, has_data=True)
        statistic_id = importer.channels[0].statistic_id
        schema_key = (
            statistic_id,
            power_history.POWER_HISTORY_SCHEMA_VERSION,
        )
        # A legitimate row just before the rolling support window must not
        # keep a stale non-empty marker alive after an empty portal rebuild.
        store.seed(
            statistic_id,
            datetime(2025, 10, 5, 22, tzinfo=timezone.utc),
            420.0,
        )
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

        await importer.async_import(1, repair=False, now=now)
        await importer.async_import(1, repair=False, now=now)

        self.assertEqual(
            importer.windows,
            [
                (date(2025, 10, 6), date(2026, 10, 5)),
                (date(2026, 10, 5), date(2026, 10, 5)),
            ],
        )
        self.assertFalse(state.presence[schema_key])
        self.assertTrue(state.is_complete(*schema_key))

    async def test_incomplete_channel_does_not_preserve_stale_presence(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={
                "grid_import": point("grid_import"),
                "grid_export": point("grid_export"),
            },
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(
                importer,
                emit=False,
            )(*args),
        )
        mark_complete(state, importer, has_data=False)
        pv_channel = next(
            channel for channel in importer.channels if channel.key == "pv"
        )
        grid_channel = next(
            channel for channel in importer.channels if channel.key == "grid"
        )
        schema_version = power_history.POWER_HISTORY_SCHEMA_VERSION
        state.presence[(pv_channel.statistic_id, schema_version)] = True
        # A second, incomplete channel makes this a full scan before the
        # stale PV marker is inspected.
        state.versions.pop(grid_channel.statistic_id)
        store.seed(
            pv_channel.statistic_id,
            datetime(2025, 10, 5, 22, tzinfo=timezone.utc),
            420.0,
        )
        store.seed(
            grid_channel.statistic_id,
            datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            120.0,
        )
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

        await importer.async_import(1, repair=False, now=now)
        await importer.async_import(1, repair=False, now=now)

        self.assertEqual(
            importer.windows,
            [
                (date(2025, 10, 6), date(2026, 10, 5)),
                (date(2026, 10, 5), date(2026, 10, 5)),
            ],
        )
        pv_key = (pv_channel.statistic_id, schema_version)
        grid_key = (grid_channel.statistic_id, schema_version)
        self.assertFalse(state.presence[pv_key])
        self.assertTrue(state.presence[grid_key])
        self.assertTrue(state.is_complete(*pv_key))
        self.assertTrue(state.is_complete(*grid_key))

    async def test_restore_check_reads_full_window_before_resetting(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(
                importer,
                emit=False,
            )(*args),
        )
        mark_complete(state, importer, has_data=True)
        statistic_id = importer.channels[0].statistic_id
        store.seed(
            statistic_id,
            datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            420.0,
        )
        # More than the short refresh read's capacity of future rows can hide
        # the valid supported row until the full-window confirmation read.
        for offset in range(76):
            store.seed(
                statistic_id,
                datetime(2026, 10, 6, tzinfo=timezone.utc)
                + timedelta(hours=offset),
                500.0,
            )
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

        await importer.async_import(1, repair=False, now=now)

        self.assertEqual(
            importer.windows,
            [(date(2026, 10, 5), date(2026, 10, 5))],
        )
        key = (statistic_id, power_history.POWER_HISTORY_SCHEMA_VERSION)
        self.assertTrue(state.presence[key])
        self.assertTrue(state.is_complete(*key))

    async def test_saturated_future_read_does_not_force_annual_scan(
        self,
    ) -> None:
        state = FakeHistoryState()
        read_limits: list[int] = []
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

        async def read_future_rows(
            _statistic_id: str,
            record_count: int,
        ) -> list[Mapping[str, object]]:
            read_limits.append(record_count)
            return [
                {
                    "start": now + timedelta(hours=offset + 1),
                    "mean": 500.0,
                }
                for offset in range(record_count)
            ]

        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_reader=read_future_rows,
            fetch_factory=lambda *args: successful_fetch(
                importer,
                emit=False,
            )(*args),
        )
        mark_complete(state, importer, has_data=True)

        await importer.async_import(1, repair=False, now=now)
        await importer.async_import(1, repair=False, now=now)

        full_record_count = (
            power_history.POWER_HISTORY_DAYS
            * power_history.MAX_HOURLY_RECORDS_PER_DAY
        )
        self.assertEqual(
            read_limits,
            [75, full_record_count, 75, full_record_count],
        )
        self.assertEqual(
            importer.windows,
            [(now.date(), now.date()), (now.date(), now.date())],
        )
        key = (
            importer.channels[0].statistic_id,
            power_history.POWER_HISTORY_SCHEMA_VERSION,
        )
        self.assertTrue(state.presence[key])
        self.assertTrue(state.is_complete(*key))

    async def test_saturated_old_read_forces_one_annual_repair(
        self,
    ) -> None:
        state = FakeHistoryState()
        read_limits: list[int] = []
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        old_start = datetime(2025, 10, 5, 22, tzinfo=timezone.utc)
        full_record_count = (
            power_history.POWER_HISTORY_DAYS
            * power_history.MAX_HOURLY_RECORDS_PER_DAY
        )

        async def read_old_rows(
            _statistic_id: str,
            record_count: int,
        ) -> list[Mapping[str, object]]:
            read_limits.append(record_count)
            return [
                {
                    "start": old_start - timedelta(hours=offset),
                    "mean": 500.0,
                }
                for offset in range(record_count)
            ]

        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_reader=read_old_rows,
            fetch_factory=lambda *args: successful_fetch(
                importer,
                emit=False,
            )(*args),
        )
        mark_complete(state, importer, has_data=True)

        await importer.async_import(repair=False, now=now)

        self.assertEqual(read_limits, [125, full_record_count])
        self.assertEqual(
            importer.windows,
            [(date(2025, 10, 6), now.date())],
        )
        self.assertEqual(importer.diagnostic_status["last_mode"], "repair")
        key = (
            importer.channels[0].statistic_id,
            power_history.POWER_HISTORY_SCHEMA_VERSION,
        )
        self.assertFalse(state.presence[key])
        self.assertTrue(state.is_complete(*key))

        await importer.async_import(repair=False, now=now)

        self.assertEqual(read_limits, [125, full_record_count, 125])
        self.assertEqual(
            importer.windows,
            [
                (date(2025, 10, 6), now.date()),
                (date(2026, 10, 3), now.date()),
            ],
        )
        self.assertEqual(importer.diagnostic_status["last_mode"], "refresh")

    async def test_saturated_mixed_range_proves_supported_rows_absent(
        self,
    ) -> None:
        state = FakeHistoryState()
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        old_start = datetime(2025, 10, 5, 22, tzinfo=timezone.utc)
        full_record_count = (
            power_history.POWER_HISTORY_DAYS
            * power_history.MAX_HOURLY_RECORDS_PER_DAY
        )

        async def read_mixed_rows(
            _statistic_id: str,
            record_count: int,
        ) -> list[Mapping[str, object]]:
            rows = [
                {
                    "start": now + timedelta(hours=offset + 1),
                    "mean": 500.0,
                }
                for offset in range(record_count)
            ]
            if record_count == full_record_count:
                rows[-1] = {"start": old_start, "mean": 500.0}
            return rows

        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_reader=read_mixed_rows,
            fetch_factory=lambda *args: successful_fetch(
                importer,
                emit=False,
            )(*args),
        )
        mark_complete(state, importer, has_data=True)

        await importer.async_import(1, repair=False, now=now)

        self.assertEqual(
            importer.windows,
            [(date(2025, 10, 6), now.date())],
        )
        self.assertEqual(importer.diagnostic_status["last_mode"], "repair")

    async def test_unsaturated_future_read_still_forces_annual_scan(
        self,
    ) -> None:
        state = FakeHistoryState()
        read_limits: list[int] = []
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        full_record_count = (
            power_history.POWER_HISTORY_DAYS
            * power_history.MAX_HOURLY_RECORDS_PER_DAY
        )

        async def read_future_rows(
            _statistic_id: str,
            record_count: int,
        ) -> list[Mapping[str, object]]:
            read_limits.append(record_count)
            returned_count = (
                record_count - 1
                if record_count == full_record_count
                else record_count
            )
            return [
                {
                    "start": now + timedelta(hours=offset + 1),
                    "mean": 500.0,
                }
                for offset in range(returned_count)
            ]

        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_reader=read_future_rows,
            fetch_factory=lambda *args: successful_fetch(
                importer,
                emit=False,
            )(*args),
        )
        mark_complete(state, importer, has_data=True)

        await importer.async_import(1, repair=False, now=now)

        self.assertEqual(read_limits, [75, full_record_count])
        self.assertEqual(
            importer.windows,
            [(date(2025, 10, 6), now.date())],
        )
        key = (
            importer.channels[0].statistic_id,
            power_history.POWER_HISTORY_SCHEMA_VERSION,
        )
        self.assertFalse(state.presence[key])
        self.assertTrue(state.is_complete(*key))

    async def test_saturated_channel_does_not_mask_definitive_stale_channel(
        self,
    ) -> None:
        state = FakeHistoryState()
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        full_record_count = (
            power_history.POWER_HISTORY_DAYS
            * power_history.MAX_HOURLY_RECORDS_PER_DAY
        )
        pv_statistic_id: str

        async def read_future_rows(
            statistic_id: str,
            record_count: int,
        ) -> list[Mapping[str, object]]:
            returned_count = record_count
            if (
                statistic_id != pv_statistic_id
                and record_count == full_record_count
            ):
                returned_count -= 1
            return [
                {
                    "start": now + timedelta(hours=offset + 1),
                    "mean": 500.0,
                }
                for offset in range(returned_count)
            ]

        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={
                "grid_import": point("grid_import"),
                "grid_export": point("grid_export"),
            },
            history_state=state,
            statistics_reader=read_future_rows,
            fetch_factory=lambda *args: successful_fetch(
                importer,
                emit=False,
            )(*args),
        )
        pv_statistic_id = next(
            channel.statistic_id
            for channel in importer.channels
            if channel.key == "pv"
        )
        mark_complete(state, importer, has_data=True)

        await importer.async_import(1, repair=False, now=now)

        self.assertEqual(
            importer.windows,
            [(date(2025, 10, 6), now.date())],
        )
        presence_by_key = {
            channel.key: state.presence[
                (
                    channel.statistic_id,
                    power_history.POWER_HISTORY_SCHEMA_VERSION,
                )
            ]
            for channel in importer.channels
        }
        self.assertEqual(presence_by_key, {"pv": True, "grid": False})
        self.assertTrue(
            all(
                state.is_complete(
                    channel.statistic_id,
                    power_history.POWER_HISTORY_SCHEMA_VERSION,
                )
                for channel in importer.channels
            )
        )

    async def test_future_saturation_does_not_mask_old_saturated_channel(
        self,
    ) -> None:
        state = FakeHistoryState()
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        old_start = datetime(2025, 10, 5, 22, tzinfo=timezone.utc)
        pv_statistic_id: str

        async def read_rows(
            statistic_id: str,
            record_count: int,
        ) -> list[Mapping[str, object]]:
            anchor = now + timedelta(hours=record_count)
            step = -1
            if statistic_id != pv_statistic_id:
                anchor = old_start - timedelta(hours=record_count)
                step = 1
            return [
                {
                    "start": anchor + timedelta(hours=step * offset),
                    "mean": 500.0,
                }
                for offset in range(record_count)
            ]

        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={
                "grid_import": point("grid_import"),
                "grid_export": point("grid_export"),
            },
            history_state=state,
            statistics_reader=read_rows,
            fetch_factory=lambda *args: successful_fetch(
                importer,
                emit=False,
            )(*args),
        )
        pv_statistic_id = next(
            channel.statistic_id
            for channel in importer.channels
            if channel.key == "pv"
        )
        mark_complete(state, importer, has_data=True)

        await importer.async_import(1, repair=False, now=now)

        self.assertEqual(
            importer.windows,
            [(date(2025, 10, 6), now.date())],
        )
        self.assertEqual(importer.diagnostic_status["last_mode"], "repair")
        presence_by_key = {
            channel.key: state.presence[
                (
                    channel.statistic_id,
                    power_history.POWER_HISTORY_SCHEMA_VERSION,
                )
            ]
            for channel in importer.channels
        }
        self.assertEqual(presence_by_key, {"pv": True, "grid": False})
        self.assertTrue(
            all(
                state.is_complete(
                    channel.statistic_id,
                    power_history.POWER_HISTORY_SCHEMA_VERSION,
                )
                for channel in importer.channels
            )
        )

    async def test_refresh_preserves_previously_stored_missing_hour(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        # The cross-midnight D-1 boundary is particularly likely to be absent
        # from a partial portal response and must not be treated as zero.
        missing_hour = datetime(2026, 10, 4, 23, tzinfo=timezone.utc)

        def fetch(
            _start_date: date,
            end_date: date,
            _local_tz: ZoneInfo,
            _completed_before: datetime,
            _call_number: int,
        ) -> power_history.PowerHistoryFetchResult:
            return power_history.PowerHistoryFetchResult(
                hourly_means={"pv": ()},
                completed=True,
                checked_through=end_date,
                requested_days=2,
                emitted_hours=0,
                replaceable_hours=(missing_hour,),
                incomplete_days={"pv": frozenset({end_date})},
                complete_days={"pv": frozenset()},
            )

        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=fetch,
        )
        mark_complete(state, importer, has_data=True)
        statistic_id = importer.channels[0].statistic_id
        store.seed(statistic_id, missing_hour, 750.0)

        await importer.async_import(
            now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        )

        self.assertEqual(store.rows[statistic_id][missing_hour], 750.0)
        self.assertFalse(
            any(
                statistic == {"start": missing_hour, "mean": 0.0}
                for _metadata, batch in store.writes
                for statistic in batch
            )
        )

    async def test_markerless_full_scan_preserves_existing_data_presence(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(
                importer,
                emit=False,
            )(*args),
        )
        statistic_id = importer.channels[0].statistic_id
        existing_hour = datetime(2026, 10, 4, 23, tzinfo=timezone.utc)
        store.seed(statistic_id, existing_hour, 420.0)

        await importer.async_import(
            now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        )

        key = (statistic_id, power_history.POWER_HISTORY_SCHEMA_VERSION)
        self.assertEqual(store.rows[statistic_id][existing_hour], 420.0)
        self.assertTrue(state.presence[key])
        self.assertTrue(state.is_complete(*key))

    async def test_pending_persistence_retries_batch_without_portal_refetch(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        checks = 0

        async def confirm_on_retry(
            _statistic_id: str,
            _statistics: list[dict],
        ) -> bool:
            nonlocal checks
            checks += 1
            return checks >= 2

        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=confirm_on_retry,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(importer)(*args),
        )
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

        await importer.async_import(1, repair=False, now=now)
        self.assertEqual(len(importer.windows), 1)
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "persistence_pending",
        )
        self.assertTrue(
            importer.diagnostic_status["persistence_confirmation_pending"]
        )

        await importer.async_import(
            1,
            repair=False,
            now=now + timedelta(hours=1),
        )

        self.assertEqual(len(importer.windows), 1)
        self.assertEqual(checks, 1)
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "persistence_pending",
        )
        self.assertTrue(
            importer.diagnostic_status["persistence_confirmation_pending"]
        )
        self.assertFalse(
            state.is_complete(
                importer.channels[0].statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            )
        )

        await importer.async_import(
            3,
            repair=True,
            now=now + timedelta(hours=6),
        )

        self.assertEqual(len(importer.windows), 1)
        self.assertEqual(checks, 2)
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "completed",
        )
        self.assertFalse(
            importer.diagnostic_status["persistence_confirmation_pending"]
        )
        self.assertTrue(
            state.is_complete(
                importer.channels[0].statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            )
        )

    async def test_pending_hourly_refresh_preserves_coverage_gap(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        checks = 0

        async def confirm_on_retry(
            _statistic_id: str,
            _statistics: list[dict],
        ) -> bool:
            nonlocal checks
            checks += 1
            return checks >= 2

        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=confirm_on_retry,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(importer)(*args),
        )
        last_verified = date(2026, 10, 5)
        mark_complete(
            state,
            importer,
            has_data=False,
            checked_through=last_verified,
        )
        now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
        statistic_id = importer.channels[0].statistic_id
        key = (statistic_id, power_history.POWER_HISTORY_SCHEMA_VERSION)

        await importer.async_import(1, repair=False, now=now)

        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "persistence_pending",
        )
        self.assertEqual(state.checked_through(*key), last_verified)

        # A repair first confirms the retained Recorder batch. Finalizing that
        # isolated current-day result must still leave the gap visible.
        await importer.async_import(
            3,
            repair=True,
            now=now + timedelta(hours=6),
        )

        self.assertEqual(len(importer.windows), 1)
        self.assertEqual(state.checked_through(*key), last_verified)
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "completed",
        )

        await importer.async_import(
            3,
            repair=True,
            now=now + timedelta(hours=6),
        )

        self.assertEqual(
            importer.windows,
            [
                (date(2026, 10, 12), date(2026, 10, 12)),
                (date(2026, 10, 6), date(2026, 10, 12)),
            ],
        )
        self.assertEqual(state.checked_through(*key), now.date())

    async def test_pending_persistence_retries_after_clock_rollback(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        checks = 0

        async def confirm_on_retry(
            _statistic_id: str,
            _statistics: list[dict],
        ) -> bool:
            nonlocal checks
            checks += 1
            return checks >= 2

        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=confirm_on_retry,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(importer)(*args),
        )
        mark_complete(
            state,
            importer,
            has_data=False,
            checked_through=date(2026, 10, 5),
        )

        await importer.async_import(
            1,
            repair=False,
            now=datetime(2026, 10, 12, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(len(importer.windows), 1)
        self.assertEqual(checks, 1)
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "persistence_pending",
        )

        await importer.async_import(
            3,
            repair=True,
            now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(checks, 2)
        self.assertEqual(len(importer.windows), 1)
        self.assertEqual(len(store.writes), 2)
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "completed",
        )
        self.assertFalse(
            importer.diagnostic_status["persistence_confirmation_pending"]
        )
        self.assertIsNone(
            state.checked_through(
                importer.channels[0].statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            )
        )

        # The retained write is durable, but its scan state is invalidated
        # because the rollback moved the supported window. The next repair
        # therefore rebuilds that complete window rather than trusting the
        # pending batch's old date range.
        await importer.async_import(
            3,
            repair=True,
            now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(checks, 3)
        self.assertEqual(
            importer.windows[-1],
            (date(2025, 10, 6), date(2026, 10, 5)),
        )
        self.assertEqual(
            state.checked_through(
                importer.channels[0].statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            ),
            date(2026, 10, 5),
        )
        self.assertEqual(importer.diagnostic_status["last_mode"], "initial")

    async def test_pending_persistence_never_refetches_portal_in_runtime(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()

        async def never_confirmed(
            _statistic_id: str,
            _statistics: list[dict],
        ) -> bool:
            return False

        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=never_confirmed,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(importer)(*args),
        )
        now = datetime(2026, 10, 5, 4, tzinfo=timezone.utc)

        await importer.async_import(1, repair=False, now=now)
        await importer.async_import(
            1,
            repair=False,
            now=now + timedelta(hours=7),
        )
        self.assertEqual(len(importer.windows), 1)

        await importer.async_import(
            3,
            repair=True,
            now=now + timedelta(hours=7),
        )

        self.assertEqual(len(importer.windows), 1)
        self.assertEqual(len(store.writes), 2)
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "persistence_pending",
        )

    async def test_failed_persistence_skips_immediate_coalesced_refetch(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        confirmation_started = asyncio.Event()
        release_confirmation = asyncio.Event()

        async def delayed_failure(
            _statistic_id: str,
            _statistics: list[dict],
        ) -> bool:
            confirmation_started.set()
            await release_confirmation.wait()
            return False

        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=delayed_failure,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(importer)(*args),
        )
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

        first = asyncio.create_task(
            importer.async_import(1, repair=False, now=now)
        )
        await confirmation_started.wait()
        await importer.async_import(3, repair=True, now=now)
        release_confirmation.set()
        await first

        self.assertEqual(len(importer.windows), 1)
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "persistence_pending",
        )

    async def test_late_failure_records_progress_and_next_run_resumes(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        first_checked: date | None = None

        def fetch(
            start_date: date,
            end_date: date,
            _local_tz: ZoneInfo,
            completed_before: datetime,
            call_number: int,
        ) -> power_history.PowerHistoryFetchResult:
            nonlocal first_checked
            start = completed_before.replace(
                minute=0,
                second=0,
                microsecond=0,
            ) - timedelta(hours=1)
            if call_number == 1:
                first_checked = start_date + timedelta(days=4)
                return power_history.PowerHistoryFetchResult(
                    hourly_means={"pv": ((start, 250.0),)},
                    completed=False,
                    checked_through=first_checked,
                    requested_days=6,
                    emitted_hours=1,
                    replaceable_hours=(start,),
                    incomplete_days={"pv": frozenset()},
                    complete_days={"pv": frozenset()},
                )
            return power_history.PowerHistoryFetchResult(
                hourly_means={"pv": ((start, 250.0),)},
                completed=True,
                checked_through=end_date,
                requested_days=(end_date - start_date).days + 2,
                emitted_hours=1,
                replaceable_hours=(start,),
                incomplete_days={"pv": frozenset()},
                complete_days={"pv": frozenset()},
            )

        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=fetch,
        )
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

        await importer.async_import(now=now)
        statistic_id = importer.channels[0].statistic_id
        progress = state.incomplete_scan_progress(
            statistic_id,
            power_history.POWER_HISTORY_SCHEMA_VERSION,
        )
        self.assertIsNotNone(progress)
        assert progress is not None and first_checked is not None
        self.assertEqual(progress.checked_through, first_checked)
        self.assertEqual(
            importer.diagnostic_status["last_result"],
            "fetch_incomplete",
        )

        await importer.async_import(now=now)

        self.assertEqual(
            importer.windows[1][0],
            first_checked + timedelta(days=1),
        )
        self.assertIsNone(
            state.incomplete_scan_progress(
                statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            )
        )
        self.assertTrue(
            state.is_complete(
                statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            )
        )

    async def test_invalid_incomplete_progress_restarts_supported_window(
        self,
    ) -> None:
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        today = now.date()
        oldest_supported = today - timedelta(
            days=power_history.POWER_HISTORY_DAYS - 1
        )
        for days_ahead in (0, 7):
            with self.subTest(days_ahead=days_ahead):
                state = FakeHistoryState()
                store = FakeStatisticsStore()
                importer: ScriptedFetchImporter
                importer = ScriptedFetchImporter(
                    FakeHass(),
                    FakeApi({}),
                    pv_power_point=point("pv"),
                    role_points={},
                    history_state=state,
                    statistics_writer=store.write,
                    persistence_checker=store.persisted,
                    statistics_reader=store.read,
                    fetch_factory=lambda *args: successful_fetch(importer)(
                        *args
                    ),
                )
                statistic_id = importer.channels[0].statistic_id
                key = (
                    statistic_id,
                    power_history.POWER_HISTORY_SCHEMA_VERSION,
                )
                invalid_date = today + timedelta(days=days_ahead)
                mark_complete(
                    state,
                    importer,
                    has_data=False,
                    checked_through=invalid_date,
                )
                state.progress[key] = FakeProgress(
                    checked_through=invalid_date,
                    has_data=False,
                    source_time_zone="UTC",
                    oldest_supported=oldest_supported,
                )

                await importer.async_import(now=now)

                self.assertEqual(
                    importer.windows,
                    [(oldest_supported, today)],
                )
                self.assertIsNone(state.incomplete_scan_progress(*key))
                self.assertEqual(state.checked_through(*key), today)
                self.assertEqual(
                    importer.diagnostic_status["last_result"],
                    "completed",
                )

    async def test_rollback_restarts_mixed_progress_before_saved_window(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={
                "grid_import": point("import"),
                "grid_export": point("export"),
                "battery_discharge": point("discharge"),
                "battery_charge": point("charge"),
            },
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(importer)(*args),
        )
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        oldest_supported = date(2025, 10, 6)
        saved_oldest_supported = date(2025, 10, 13)
        saved_checked_through = date(2026, 1, 1)
        for index, channel in enumerate(importer.channels):
            key = (
                channel.statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            )
            state.progress[key] = FakeProgress(
                checked_through=saved_checked_through,
                has_data=False,
                source_time_zone="UTC",
                oldest_supported=(
                    saved_oldest_supported
                    if index == 0
                    else oldest_supported
                ),
            )

        await importer.async_import(now=now)

        self.assertEqual(
            importer.windows,
            [(oldest_supported, now.date())],
        )
        for channel in importer.channels:
            key = (
                channel.statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            )
            self.assertIsNone(state.incomplete_scan_progress(*key))
            self.assertEqual(state.checked_through(*key), now.date())

    async def test_rollback_restarts_completed_future_coverage(
        self,
    ) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={
                "grid_import": point("import"),
                "grid_export": point("export"),
                "battery_discharge": point("discharge"),
                "battery_charge": point("charge"),
            },
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(importer)(*args),
        )
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        future_channel = importer.channels[0]
        future_key = (
            future_channel.statistic_id,
            power_history.POWER_HISTORY_SCHEMA_VERSION,
        )
        state.versions[future_channel.statistic_id] = future_key[1]
        state.presence[future_key] = False
        state.coverage[future_key] = date(2026, 10, 12)
        state.zones[future_channel.statistic_id] = "UTC"

        await importer.async_import(now=now)

        self.assertEqual(
            importer.windows,
            [(date(2025, 10, 6), now.date())],
        )
        self.assertEqual(importer.diagnostic_status["last_mode"], "repair")
        for channel in importer.channels:
            key = (
                channel.statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
            )
            self.assertTrue(state.is_complete(*key))
            self.assertEqual(state.checked_through(*key), now.date())

    async def test_successful_empty_initial_days_are_retryable(self) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        importer: ScriptedFetchImporter
        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=lambda *args: successful_fetch(
                importer,
                emit=False,
            )(*args),
        )
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

        await importer.async_import(now=now)

        statistic_id = importer.channels[0].statistic_id
        key = (statistic_id, power_history.POWER_HISTORY_SCHEMA_VERSION)
        self.assertFalse(state.presence[key])
        self.assertEqual(len(state.empty_days[key]), 365)
        self.assertEqual(
            state.next_empty_retry_date(
                statistic_id,
                power_history.POWER_HISTORY_SCHEMA_VERSION,
                oldest_supported=date(2025, 10, 6),
                before=date(2026, 10, 4),
                today=date(2026, 10, 5),
            ),
            date(2025, 10, 6),
        )

    async def test_becoming_inactive_after_fetch_prevents_writes(self) -> None:
        active = True
        writes = AsyncMock()
        importer: ScriptedFetchImporter

        def fetch(*args) -> power_history.PowerHistoryFetchResult:
            nonlocal active
            result = successful_fetch(importer)(*args)
            active = False
            return result

        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            statistics_writer=writes,
            persistence_checker=AsyncMock(return_value=True),
            statistics_reader=AsyncMock(return_value=[]),
            active_check=lambda: active,
            fetch_factory=fetch,
        )

        await importer.async_import(
            now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        )

        writes.assert_not_called()
        self.assertEqual(importer.diagnostic_status["last_result"], "inactive")

    async def test_concurrent_wider_repair_is_coalesced(self) -> None:
        state = FakeHistoryState()
        store = FakeStatisticsStore()
        started = asyncio.Event()
        release = asyncio.Event()
        importer: ScriptedFetchImporter

        async def blocking_fetch(
            start_date: date,
            end_date: date,
            local_tz: ZoneInfo,
            *,
            completed_before: datetime,
        ) -> power_history.PowerHistoryFetchResult:
            importer.windows.append((start_date, end_date))
            if len(importer.windows) == 1:
                started.set()
                await release.wait()
            return successful_fetch(importer, emit=False)(
                start_date,
                end_date,
                local_tz,
                completed_before,
                len(importer.windows),
            )

        importer = ScriptedFetchImporter(
            FakeHass(),
            FakeApi({}),
            pv_power_point=point("pv"),
            role_points={},
            history_state=state,
            statistics_writer=store.write,
            persistence_checker=store.persisted,
            statistics_reader=store.read,
            fetch_factory=lambda *_args: (_ for _ in ()).throw(
                AssertionError("blocking method replacement was not used")
            ),
        )
        importer._fetch_hourly_means = blocking_fetch  # type: ignore[method-assign]
        mark_complete(state, importer, has_data=False)
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

        first = asyncio.create_task(
            importer.async_import(1, repair=False, now=now)
        )
        await started.wait()
        await importer.async_import(3, repair=True, now=now)
        release.set()
        await first

        self.assertEqual(
            importer.windows,
            [
                (date(2026, 10, 5), date(2026, 10, 5)),
                (date(2026, 10, 3), date(2026, 10, 5)),
            ],
        )


if __name__ == "__main__":
    unittest.main()
