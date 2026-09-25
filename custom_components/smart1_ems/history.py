"""Import historical smart1 photovoltaic production into Home Assistant."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
import logging
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import ClientError

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import EnergyConverter

from .api import Smart1Api, Smart1ApiError, describe_api_error
from .const import DOMAIN
from .history_state import (
    PV_DAILY_HISTORY_SCHEMA_VERSION,
    PV_HISTORY_SCHEMA_VERSION,
    Smart1HistoryState,
    scoped_statistic_id,
)
from .point import Smart1Point
from .power_integration import PowerIntegrationResult, integrate_power_rows
from .recorder_helpers import (
    async_clear_statistics,
    async_wait_for_recorder_commit,
    async_wait_for_statistics_readback,
    statistics_are_persisted,
    validate_energy_statistics_imports,
)

_LOGGER = logging.getLogger(__name__)

HISTORY_DAYS = 365
REFRESH_DAYS = 3
MAX_HOURLY_RECORDS_PER_DAY = 25
PV_STATISTIC_ID = f"{DOMAIN}:pv_production"
PV_FETCH_ATTEMPTS = 3
PV_STATISTICS_LOOKBACK = HISTORY_DAYS * MAX_HOURLY_RECORDS_PER_DAY


def pv_statistic_id(statistics_namespace: str = "") -> str:
    """Return the installation-scoped PV statistic ID."""
    return scoped_statistic_id(PV_STATISTIC_ID, statistics_namespace)


def _statistics_start(record: Mapping[str, Any]) -> datetime:
    """Return a statistics record start as an aware UTC datetime."""
    start = record["start"]
    if isinstance(start, datetime):
        return start.astimezone(timezone.utc)
    return datetime.fromtimestamp(float(start), timezone.utc)


def _statistics_date(record: Mapping[str, Any], local_tz: ZoneInfo) -> date:
    """Return the local date represented by a statistics record."""
    return _statistics_start(record).astimezone(local_tz).date()


def determine_import_window(
    records: list[Mapping[str, Any]],
    today: date,
    local_tz: ZoneInfo,
    refresh_days: int = REFRESH_DAYS,
    *,
    initial_backfill_complete: bool = False,
) -> tuple[date, float]:
    """Return the first date to refresh and its preceding cumulative sum."""
    if not records:
        if initial_backfill_complete:
            return today - timedelta(days=refresh_days - 1), 0.0
        return today - timedelta(days=HISTORY_DAYS - 1), 0.0

    refresh_start = today - timedelta(days=refresh_days - 1)
    preceding = [
        record
        for record in records
        if record.get("sum") is not None
        and _statistics_date(record, local_tz) < refresh_start
    ]

    if preceding:
        baseline = max(
            preceding,
            key=_statistics_start,
        )
        return refresh_start, float(baseline["sum"])

    oldest_date = min(_statistics_date(record, local_tz) for record in records)
    return min(oldest_date, refresh_start), 0.0


def build_daily_energy_statistics(
    daily_energy: list[tuple[date, float]],
    baseline_sum: float,
    local_tz: ZoneInfo,
) -> list[StatisticData]:
    """Build cumulative Home Assistant statistics from daily kWh values."""
    statistics: list[StatisticData] = []
    cumulative_sum = baseline_sum

    for target_date, energy_kwh in sorted(daily_energy):
        cumulative_sum += energy_kwh
        statistics.append(
            StatisticData(
                start=_first_utc_hour_in_local_day(target_date, local_tz),
                state=energy_kwh,
                sum=cumulative_sum,
            )
        )

    return statistics


def _first_utc_hour_in_local_day(
    target_date: date,
    local_tz: ZoneInfo,
) -> datetime:
    """Return the first whole UTC-hour boundary inside a local date."""
    local_start = datetime.combine(
        target_date,
        time.min,
        tzinfo=local_tz,
    ).astimezone(timezone.utc)
    utc_hour = local_start.replace(minute=0, second=0, microsecond=0)
    if utc_hour < local_start:
        utc_hour += timedelta(hours=1)
    return utc_hour


def merge_daily_energy(
    records: list[Mapping[str, Any]],
    fetched_energy: list[tuple[date, float]],
    start_date: date,
    end_date: date,
    local_tz: ZoneInfo,
) -> list[tuple[date, float]]:
    """Merge refreshed values with existing data for temporarily missing dates."""
    daily_energy: dict[date, float] = {}
    existing_start_by_date: dict[date, datetime] = {}

    for record in records:
        energy_kwh = record.get("state")
        if energy_kwh is None:
            continue

        target_date = _statistics_date(record, local_tz)
        record_start = _statistics_start(record)
        existing_start = existing_start_by_date.get(target_date)
        if (
            start_date <= target_date <= end_date
            and (existing_start is None or record_start >= existing_start)
        ):
            daily_energy[target_date] = float(energy_kwh)
            existing_start_by_date[target_date] = record_start

    daily_energy.update(fetched_energy)
    return sorted(daily_energy.items())


def needs_hourly_pv_migration(
    records: list[Mapping[str, Any]],
    local_tz: ZoneInfo,
) -> bool:
    """Return whether exact PV totals still occupy daily midnight buckets."""
    return any(
        _statistics_start(record).astimezone(local_tz).hour == 0
        and float(record.get("state") or 0) > 1e-9
        for record in records
    )


def needs_utc_hour_alignment_rebuild(
    records: list[Mapping[str, Any]],
) -> bool:
    """Return whether Recorder contains a PV bucket off a UTC hour."""
    return any(not _is_utc_hour_aligned(record) for record in records)


def _is_utc_hour_aligned(record: Mapping[str, Any]) -> bool:
    """Return whether a Recorder row starts on a whole UTC hour."""
    start = _statistics_start(record)
    return not (start.minute or start.second or start.microsecond)


def _utc_hour_aligned_records(
    records: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Return Recorder rows that can safely be imported again."""
    return [record for record in records if _is_utc_hour_aligned(record)]


def _has_hourly_profile_semantics(
    records: list[Mapping[str, Any]],
    local_tz: ZoneInfo,
) -> bool:
    """Return whether Recorder rows represent an hourly PV profile."""
    aligned_counts: dict[date, int] = {}
    misaligned_zero_dates: set[date] = set()
    for record in records:
        target_date = _statistics_date(record, local_tz)
        if not _is_utc_hour_aligned(record):
            state = record.get("state")
            try:
                is_zero = state is not None and abs(float(state)) <= 1e-9
            except (TypeError, ValueError):
                is_zero = False
            if is_zero:
                misaligned_zero_dates.add(target_date)
            continue

        aligned_counts[target_date] = aligned_counts.get(target_date, 0) + 1
        if _statistics_start(record) != _first_utc_hour_in_local_day(
            target_date,
            local_tz,
        ):
            return True
    return any(count > 1 for count in aligned_counts.values()) or any(
        target_date in aligned_counts
        for target_date in misaligned_zero_dates
    )


def _merge_profile_history_for_daily_rebuild(
    records: list[Mapping[str, Any]],
    fetched_energy: list[tuple[date, float]],
    start_date: date,
    end_date: date,
    local_tz: ZoneInfo,
) -> list[tuple[datetime, float]]:
    """Merge a stored hourly profile into a daily-only replacement batch."""
    fetched_by_date = dict(fetched_energy)
    records_by_date: dict[date, list[Mapping[str, Any]]] = {}
    for record in records:
        target_date = _statistics_date(record, local_tz)
        if start_date <= target_date <= end_date:
            records_by_date.setdefault(target_date, []).append(record)

    energy_by_hour: dict[datetime, float] = {}
    for target_date in sorted(set(records_by_date) | set(fetched_by_date)):
        if target_date in fetched_by_date:
            energy_by_hour[
                _first_utc_hour_in_local_day(target_date, local_tz)
            ] = fetched_by_date[target_date]
            continue

        day_records = records_by_date.get(target_date, [])
        aligned_records = _utc_hour_aligned_records(day_records)
        if aligned_records:
            for record in aligned_records:
                state = record.get("state")
                if state is not None:
                    energy_by_hour[_statistics_start(record)] = float(state)
            continue

        # No hourly profile exists for this successful-but-missing portal day.
        # Preserve a lone positive exact-daily fallback but discard artificial
        # zero buckets and ambiguous duplicate legacy rows.
        if len(day_records) != 1:
            continue
        state = day_records[0].get("state")
        if state is None:
            continue
        energy_kwh = float(state)
        if energy_kwh <= 1e-9:
            continue
        energy_by_hour[
            _first_utc_hour_in_local_day(target_date, local_tz)
        ] = energy_kwh

    return sorted(energy_by_hour.items())


def _preserved_pv_statistics_before(
    records: list[Mapping[str, Any]],
    start_date: date,
    local_tz: ZoneInfo,
) -> list[StatisticData]:
    """Keep profile rows preceding the supported rebuild window.

    Whole-UTC-hour rows are genuine hourly profile buckets and remain
    unchanged.  A lone positive local-midnight row is a legacy exact-daily
    fallback; remap it only when its local day has no hourly profile at all.
    """
    preserved: dict[datetime, StatisticData] = {}
    records_by_date: dict[date, list[Mapping[str, Any]]] = {}
    for record in records:
        target_date = _statistics_date(record, local_tz)
        if target_date >= start_date:
            continue
        records_by_date.setdefault(target_date, []).append(record)

    for target_date, day_records in records_by_date.items():
        aligned_records = _utc_hour_aligned_records(day_records)
        candidates = aligned_records
        remap_singleton = not aligned_records and len(day_records) == 1
        if remap_singleton:
            candidates = day_records

        for record in candidates:
            original_start = _statistics_start(record)
            start = (
                _first_utc_hour_in_local_day(target_date, local_tz)
                if remap_singleton
                else original_start
            )
            state = record.get("state")
            cumulative_sum = record.get("sum")
            if state is None or cumulative_sum is None:
                continue
            try:
                energy_kwh = float(state)
                sum_kwh = float(cumulative_sum)
            except (TypeError, ValueError):
                continue
            if remap_singleton and energy_kwh <= 1e-9:
                continue
            preserved[start] = StatisticData(
                start=start,
                state=energy_kwh,
                sum=sum_kwh,
            )
    return [preserved[start] for start in sorted(preserved)]


def _preserved_daily_pv_statistics_before(
    records: list[Mapping[str, Any]],
    start_date: date,
    local_tz: ZoneInfo,
) -> list[StatisticData]:
    """Remap exact daily PV rows preceding the supported rebuild window."""
    preserved: dict[date, tuple[datetime, StatisticData]] = {}
    for record in records:
        original_start = _statistics_start(record)
        target_date = original_start.astimezone(local_tz).date()
        if target_date >= start_date:
            continue
        state = record.get("state")
        cumulative_sum = record.get("sum")
        if state is None or cumulative_sum is None:
            continue
        try:
            energy_kwh = float(state)
            sum_kwh = float(cumulative_sum)
        except (TypeError, ValueError):
            continue
        if energy_kwh < 0:
            continue

        # Daily-only history has exactly one semantic value per local date.
        # If legacy and repaired rows coexist, the later row is the repaired
        # UTC-aligned representation and therefore wins deterministically.
        current = preserved.get(target_date)
        if current is not None and current[0] >= original_start:
            continue
        preserved[target_date] = (
            original_start,
            StatisticData(
                start=_first_utc_hour_in_local_day(target_date, local_tz),
                state=energy_kwh,
                sum=sum_kwh,
            ),
        )

    return [preserved[target_date][1] for target_date in sorted(preserved)]


def determine_hourly_pv_import_window(
    records: list[Mapping[str, Any]],
    today: date,
    local_tz: ZoneInfo,
    refresh_days: int = REFRESH_DAYS,
    *,
    initial_backfill_complete: bool = False,
    force_initial_rebuild: bool = False,
    force_full_refresh: bool = False,
) -> tuple[date, float]:
    """Return the hourly PV refresh window and preceding cumulative sum."""
    full_start = today - timedelta(days=HISTORY_DAYS - 1)
    if force_initial_rebuild:
        return full_start, 0.0
    if not records:
        if initial_backfill_complete:
            return today - timedelta(days=refresh_days - 1), 0.0
        return full_start, 0.0
    if force_full_refresh or (
        not initial_backfill_complete
        and needs_hourly_pv_migration(records, local_tz)
    ):
        preceding = [
            record
            for record in records
            if record.get("sum") is not None
            and _statistics_date(record, local_tz) < full_start
        ]
        if preceding:
            baseline = max(preceding, key=_statistics_start)
            return full_start, float(baseline["sum"])

        boundary = [
            record
            for record in records
            if record.get("sum") is not None
            and record.get("state") is not None
            and _statistics_date(record, local_tz) == full_start
        ]
        if boundary:
            first = min(boundary, key=_statistics_start)
            return full_start, float(first["sum"]) - float(first["state"])
        return full_start, 0.0
    return determine_import_window(
        records,
        today,
        local_tz,
        refresh_days,
        initial_backfill_complete=initial_backfill_complete,
    )


def distribute_exact_pv_energy(
    target_date: date,
    exact_energy_kwh: float | None,
    integration: PowerIntegrationResult,
    local_tz: ZoneInfo,
) -> tuple[list[tuple[datetime, float]], bool]:
    """Scale measured hourly PV distribution to its exact daily total."""
    if exact_energy_kwh is None or exact_energy_kwh < 0:
        return [], False

    fallback_hour = _first_utc_hour_in_local_day(target_date, local_tz)
    hourly_energy = dict(integration.hourly_energy_kwh)
    integrated_total = sum(hourly_energy.values())

    if exact_energy_kwh == 0:
        return [(fallback_hour, 0.0)], True
    if integrated_total <= 0:
        return [(fallback_hour, exact_energy_kwh)], False

    scale = exact_energy_kwh / integrated_total
    scaled: dict[datetime, float] = {}
    for start, energy_kwh in hourly_energy.items():
        utc_start = start.astimezone(timezone.utc)
        # UTC hour buckets that straddle local midnight start on the previous
        # local date in fractional-offset zones. Keep their energy in the
        # requested day by folding that boundary fragment into the first
        # complete UTC hour contained by the day.
        bucket_start = (
            utc_start
            if utc_start.astimezone(local_tz).date() == target_date
            else fallback_hour
        )
        scaled[bucket_start] = (
            scaled.get(bucket_start, 0.0) + energy_kwh * scale
        )
    peak_hour = max(scaled, key=scaled.get)
    scaled[peak_hour] += exact_energy_kwh - sum(scaled.values())
    return sorted(scaled.items()), True


def merge_hourly_pv_energy(
    records: list[Mapping[str, Any]],
    fetched_energy: list[tuple[datetime, float]],
    start_date: date,
    end_date: date,
    local_tz: ZoneInfo,
) -> list[tuple[datetime, float]]:
    """Replace fetched local days while preserving temporarily missing days.

    Home Assistant's external-statistics API upserts records but does not
    delete obsolete buckets. Explicit zero-value tombstones therefore replace
    stale hours when a day's distribution changes or falls back to one bucket.
    """
    energy_by_hour: dict[datetime, float] = {}
    fetched_by_hour = {
        start.astimezone(timezone.utc): energy_kwh
        for start, energy_kwh in fetched_energy
    }
    replacement_dates = {
        start.astimezone(local_tz).date() for start in fetched_by_hour
    }

    for record in records:
        energy_kwh = record.get("state")
        if energy_kwh is None:
            continue
        start = _statistics_start(record)
        target_date = start.astimezone(local_tz).date()
        if start_date <= target_date <= end_date:
            energy_by_hour[start] = (
                0.0
                if target_date in replacement_dates
                and start not in fetched_by_hour
                else float(energy_kwh)
            )

    energy_by_hour.update(fetched_by_hour)
    return sorted(energy_by_hour.items())


def build_hourly_pv_statistics(
    hourly_energy: list[tuple[datetime, float]],
    baseline_sum: float,
) -> list[StatisticData]:
    """Build cumulative exact PV statistics from hourly energy values."""
    statistics: list[StatisticData] = []
    cumulative_sum = baseline_sum

    for start, energy_kwh in sorted(hourly_energy):
        cumulative_sum += energy_kwh
        statistics.append(
            StatisticData(
                start=start,
                state=energy_kwh,
                sum=cumulative_sum,
            )
        )

    return statistics


def _daily_exact_fallback(
    records: list[Mapping[str, Any]],
    local_tz: ZoneInfo,
) -> dict[date, float]:
    """Return exact totals from dates that still have one daily record."""
    records_by_date: dict[date, list[Mapping[str, Any]]] = {}
    for record in records:
        records_by_date.setdefault(
            _statistics_date(record, local_tz),
            [],
        ).append(record)

    return {
        target_date: float(day_records[0]["state"])
        for target_date, day_records in records_by_date.items()
        if len(day_records) == 1
        and day_records[0].get("state") is not None
        and _statistics_start(day_records[0]).astimezone(local_tz).hour == 0
    }


class Smart1PvHistoryImporter:
    """Import exact PV production with an hourly measured distribution."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: Smart1Api,
        pv_power_point: Smart1Point | None = None,
        *,
        statistics_namespace: str = "",
        history_state: Smart1HistoryState | None = None,
        force_initial_rebuild: bool = False,
    ) -> None:
        self.hass = hass
        self.api = api
        self.pv_power_point = pv_power_point
        self.statistic_id = pv_statistic_id(statistics_namespace)
        self.schema_version = (
            PV_HISTORY_SCHEMA_VERSION
            if pv_power_point is not None
            else PV_DAILY_HISTORY_SCHEMA_VERSION
        )
        self.history_state = history_state
        self.force_initial_rebuild = force_initial_rebuild
        self._lock = asyncio.Lock()
        self._last_result = "not_started"
        self._last_refresh_days: int | None = None
        self._last_fetched_days = 0
        self._last_distributed_days = 0
        self._last_daily_fallback_days = 0
        self._migration_required = False
        self._persistence_tasks: set[asyncio.Task[Any]] = set()
        self._persistence_generation = 0

    @property
    def diagnostic_status(self) -> dict[str, Any]:
        """Return value-free runtime status for diagnostics."""
        return {
            "type": "pv_history",
            "last_result": self._last_result,
            "last_refresh_days": self._last_refresh_days,
            "last_fetched_days": self._last_fetched_days,
            "last_distributed_days": self._last_distributed_days,
            "last_daily_fallback_days": self._last_daily_fallback_days,
            "hourly_distribution_available": self.pv_power_point is not None,
            "migration_required": self._migration_required,
            "initial_backfill_complete": self._is_complete(),
            "forced_initial_rebuild": self.force_initial_rebuild,
        }

    def _is_complete(self) -> bool:
        """Return whether this statistic completed its current backfill."""
        return bool(
            self.history_state
            and self.history_state.is_current_schema(
                self.statistic_id,
                self.schema_version,
            )
        )

    def _data_presence(self) -> bool | None:
        """Return whether the completed PV backfill produced recorder rows."""
        if not self.history_state:
            return None
        return self.history_state.data_presence(
            self.statistic_id,
            self.schema_version,
        )

    def _mark_complete(self, *, has_data: bool) -> None:
        """Persist completion of the current PV history schema."""
        if self.history_state:
            self.history_state.mark_complete(
                self.statistic_id,
                self.schema_version,
                has_data=has_data,
            )

    def _schedule_persistence_task(self, coroutine: Any) -> None:
        """Keep a late Recorder finalizer alive until it finishes."""
        create_task = getattr(self.hass, "async_create_task", None)
        task = (
            create_task(
                coroutine,
                "smart1 EMS PV history persistence finalizer",
            )
            if create_task is not None
            else asyncio.create_task(coroutine)
        )
        self._persistence_tasks.add(task)
        task.add_done_callback(self._persistence_tasks.discard)

    async def _async_confirm_statistics_persistence(
        self,
        recorder: Any,
        statistics: list[StatisticData],
        expected_marker_version: int,
        persistence_generation: int,
    ) -> bool:
        """Verify a queued PV import before completing its schema marker."""
        if not self.history_state:
            return True
        if persistence_generation != self._persistence_generation:
            return False
        # The Recorder barrier is advisory: a slow task may time out after
        # leaving the queue while its transaction is still in flight. The
        # bounded readback below is the authoritative completion check.
        await async_wait_for_recorder_commit(recorder)
        if persistence_generation != self._persistence_generation:
            return False

        persisted = await async_wait_for_statistics_readback(
            statistics,
            lambda: self._existing_statistics(
                PV_STATISTICS_LOOKBACK + len(statistics) + 1
            ),
            matcher=statistics_are_persisted,
        )
        if not persisted:
            return False
        if persistence_generation != self._persistence_generation:
            return False

        marked = self.history_state.mark_complete_if_unchanged(
            self.statistic_id,
            self.schema_version,
            has_data=True,
            expected_version=expected_marker_version,
        )
        if not marked:
            # A newer run may have completed the same marker while this late
            # finalizer was waiting. Treat that current state as success but
            # never overwrite a different generation.
            marked = bool(
                self.history_state.is_current_schema(
                    self.statistic_id,
                    self.schema_version,
                )
                and self.history_state.data_presence(
                    self.statistic_id,
                    self.schema_version,
                )
                is True
            )
        if marked:
            self._migration_required = False
        return marked

    async def _async_finalize_late_clear(
        self,
        recorder: Any,
        statistics: list[StatisticData],
        expected_marker_version: int,
        persistence_generation: int,
    ) -> None:
        """Finalize a replacement whose clear callback arrived late."""
        confirmed = await self._async_confirm_statistics_persistence(
            recorder,
            statistics,
            expected_marker_version,
            persistence_generation,
        )
        if persistence_generation != self._persistence_generation:
            return
        if self._last_result not in {"clear_timeout", "persistence_pending"}:
            return
        self._last_result = "completed" if confirmed else "persistence_pending"

    async def _existing_statistics(
        self,
        record_count: int = PV_STATISTICS_LOOKBACK,
    ) -> list[Mapping[str, Any]]:
        """Return enough recent records to establish a refresh baseline."""
        result = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            record_count,
            self.statistic_id,
            True,
            {"state", "sum"},
        )
        return result.get(self.statistic_id, [])

    async def _fetch_daily_energy(
        self,
        start_date: date,
        end_date: date,
    ) -> tuple[list[tuple[date, float]], bool]:
        """Fetch daily production without failing on dates with no data."""
        daily_energy: list[tuple[date, float]] = []
        target_date = start_date

        while target_date <= end_date:
            for attempt in range(PV_FETCH_ATTEMPTS):
                try:
                    value = await self.api.get_pv_cumulative_energy(
                        target_date=target_date,
                        missing_ok=True,
                    )
                    break
                except (ClientError, Smart1ApiError, TimeoutError) as err:
                    if attempt == PV_FETCH_ATTEMPTS - 1:
                        _LOGGER.warning(
                            "Unable to import smart1 PV history from %s "
                            "after %d attempts: %s",
                            target_date,
                            PV_FETCH_ATTEMPTS,
                            describe_api_error(err),
                        )
                        return daily_energy, False
                    await asyncio.sleep(2**attempt)

            if value is not None:
                daily_energy.append((target_date, value))

            target_date += timedelta(days=1)

        return daily_energy, True

    async def _fetch_hourly_energy(
        self,
        start_date: date,
        end_date: date,
        local_tz: ZoneInfo,
        exact_fallback: Mapping[date, float],
    ) -> tuple[list[tuple[datetime, float]], bool, int, int]:
        """Fetch and normalize exact PV totals into measured hourly buckets."""
        assert self.pv_power_point is not None
        hourly_energy: list[tuple[datetime, float]] = []
        completed = True
        distributed_days = 0
        daily_fallback_days = 0
        target_date = start_date

        while target_date <= end_date:
            exact_energy_kwh = exact_fallback.get(target_date)
            if exact_energy_kwh is None:
                for attempt in range(PV_FETCH_ATTEMPTS):
                    try:
                        exact_energy_kwh = (
                            await self.api.get_pv_cumulative_energy(
                                target_date=target_date,
                                missing_ok=True,
                            )
                        )
                        break
                    except (
                        ClientError,
                        Smart1ApiError,
                        TimeoutError,
                    ) as err:
                        if attempt == PV_FETCH_ATTEMPTS - 1:
                            _LOGGER.warning(
                                "Unable to import exact smart1 PV energy from "
                                "%s after %d attempts: %s",
                                target_date,
                                PV_FETCH_ATTEMPTS,
                                describe_api_error(err),
                            )
                            completed = False
                            break
                        await asyncio.sleep(2**attempt)
                if not completed:
                    break

            if exact_energy_kwh is None:
                target_date += timedelta(days=1)
                continue

            if exact_energy_kwh == 0:
                integration = PowerIntegrationResult(0, (), 0, 0, 0, 0)
            else:
                for attempt in range(PV_FETCH_ATTEMPTS):
                    try:
                        rows = await self.api.get_linear_detailed_rows(
                            [self.pv_power_point.id],
                            target_date=target_date,
                            missing_ok=True,
                        )
                        break
                    except (
                        ClientError,
                        Smart1ApiError,
                        TimeoutError,
                    ) as err:
                        if attempt == PV_FETCH_ATTEMPTS - 1:
                            _LOGGER.warning(
                                "Unable to import smart1 PV distribution from "
                                "%s after %d attempts: %s",
                                target_date,
                                PV_FETCH_ATTEMPTS,
                                describe_api_error(err),
                            )
                            completed = False
                            break
                        await asyncio.sleep(2**attempt)
                if not completed:
                    break
                integration = integrate_power_rows(
                    rows,
                    self.pv_power_point.id,
                    local_tz,
                )

            day_energy, distributed = distribute_exact_pv_energy(
                target_date,
                exact_energy_kwh,
                integration,
                local_tz,
            )
            hourly_energy.extend(day_energy)
            if distributed:
                distributed_days += 1
            elif exact_energy_kwh > 0:
                daily_fallback_days += 1
            target_date += timedelta(days=1)

        return (
            hourly_energy,
            completed,
            distributed_days,
            daily_fallback_days,
        )

    async def async_import(
        self,
        refresh_days: int = REFRESH_DAYS,
        *,
        repair: bool = True,
    ) -> None:
        """Import initial history or refresh the most recent days."""
        if self._lock.locked() and not repair:
            return

        async with self._lock:
            self._last_result = "running"
            self._last_refresh_days = refresh_days
            local_tz = ZoneInfo(self.hass.config.time_zone)
            today = datetime.now(local_tz).date()
            record_count = (
                PV_STATISTICS_LOOKBACK
                if repair
                else (refresh_days + 1) * MAX_HOURLY_RECORDS_PER_DAY
            )
            records = await self._existing_statistics(record_count)
            initial_backfill_complete = self._is_complete()
            # A completed non-empty backfill must be recreated if its recorder
            # rows later disappear. Only an explicitly empty successful
            # backfill may retain the short refresh window without records.
            if (
                initial_backfill_complete
                and not records
                and self._data_presence() is not False
            ):
                initial_backfill_complete = False
            forced_initial_rebuild = (
                self.force_initial_rebuild
                and not initial_backfill_complete
            )
            alignment_rebuild_required = needs_utc_hour_alignment_rebuild(
                records
            )
            switching_from_daily_schema = bool(
                self.pv_power_point is not None
                and self.history_state
                and self.history_state.is_current_schema(
                    self.statistic_id,
                    PV_DAILY_HISTORY_SCHEMA_VERSION,
                )
            )
            stored_schema_version = (
                self.history_state.current_schema_version(self.statistic_id)
                if self.history_state
                else 0
            )
            switching_from_hourly_schema = bool(
                self.pv_power_point is None
                and stored_schema_version
                in {
                    PV_HISTORY_SCHEMA_VERSION - 1,
                    PV_HISTORY_SCHEMA_VERSION,
                }
            )
            current_daily_schema = (
                stored_schema_version == PV_DAILY_HISTORY_SCHEMA_VERSION
            )
            stored_profile_semantics = bool(
                self.pv_power_point is None
                and not current_daily_schema
                and (
                    switching_from_hourly_schema
                    or _has_hourly_profile_semantics(records, local_tz)
                )
            )
            profile_schema_rebuild_required = stored_profile_semantics
            destructive_rebuild_required = (
                forced_initial_rebuild
                or alignment_rebuild_required
                or profile_schema_rebuild_required
            )
            if destructive_rebuild_required:
                # Invalidate finalizers belonging to an older timed-out clear
                # before this run performs any further network awaits.
                self._persistence_generation += 1
            persistence_generation = self._persistence_generation
            self._migration_required = (
                alignment_rebuild_required
                or profile_schema_rebuild_required
                or (
                    not initial_backfill_complete
                    and (
                        forced_initial_rebuild
                        or (
                            self.pv_power_point is not None
                            and (
                                switching_from_daily_schema
                                or needs_hourly_pv_migration(
                                    records,
                                    local_tz,
                                )
                            )
                        )
                    )
                )
            )

            # Existing hourly data predates the persistent completion marker.
            # It already represents the current schema and can be adopted
            # without another 365-day request sweep.
            if (
                records
                and not initial_backfill_complete
                and not self._migration_required
                and not forced_initial_rebuild
            ):
                self._mark_complete(has_data=True)
                initial_backfill_complete = True

            if not repair and (
                self._migration_required
                or (not records and not initial_backfill_complete)
            ):
                self._last_result = "repair_pending"
                return

            start_date, baseline_sum = determine_hourly_pv_import_window(
                records,
                today,
                local_tz,
                refresh_days,
                initial_backfill_complete=initial_backfill_complete,
                force_initial_rebuild=forced_initial_rebuild,
                force_full_refresh=(
                    switching_from_daily_schema
                    or alignment_rebuild_required
                    or profile_schema_rebuild_required
                ),
            )

            if self.pv_power_point is None:
                fetched_daily_energy, fetch_completed = (
                    await self._fetch_daily_energy(
                        start_date,
                        today,
                    )
                )
                portal_daily_fallback_days = sum(
                    energy_kwh > 0
                    for _target_date, energy_kwh in fetched_daily_energy
                )
                if stored_profile_semantics:
                    fetched_hourly_energy = (
                        _merge_profile_history_for_daily_rebuild(
                            records,
                            fetched_daily_energy,
                            start_date,
                            today,
                            local_tz,
                        )
                    )
                else:
                    if alignment_rebuild_required:
                        # Legacy daily-only rows are exact day totals even
                        # when their local-midnight timestamps are not valid
                        # UTC-hour buckets. Keep them as fallbacks for
                        # successful ``missing_ok`` days; fresh portal values
                        # override by local date in ``merge_daily_energy``.
                        fetched_daily_energy = merge_daily_energy(
                            records,
                            fetched_daily_energy,
                            start_date,
                            today,
                            local_tz,
                        )
                    fetched_hourly_energy = [
                        (
                            _first_utc_hour_in_local_day(
                                target_date,
                                local_tz,
                            ),
                            energy_kwh,
                        )
                        for target_date, energy_kwh in fetched_daily_energy
                    ]
                distributed_days = 0
                daily_fallback_days = (
                    portal_daily_fallback_days
                    if stored_profile_semantics
                    else sum(
                        energy_kwh > 0
                        for _target_date, energy_kwh in fetched_daily_energy
                    )
                )
            else:
                exact_fallback = (
                    _daily_exact_fallback(records, local_tz)
                    if self._migration_required
                    and not forced_initial_rebuild
                    else {}
                )
                (
                    fetched_hourly_energy,
                    fetch_completed,
                    distributed_days,
                    daily_fallback_days,
                ) = await self._fetch_hourly_energy(
                    start_date,
                    today,
                    local_tz,
                    exact_fallback,
                )

            self._last_fetched_days = len(
                {
                    start.astimezone(local_tz).date()
                    for start, _energy in fetched_hourly_energy
                }
            )
            self._last_distributed_days = distributed_days
            self._last_daily_fallback_days = daily_fallback_days

            if (
                not initial_backfill_complete
                or alignment_rebuild_required
                or profile_schema_rebuild_required
            ) and not fetch_completed:
                _LOGGER.warning(
                    "Deferring initial smart1 PV history import because "
                    "the history fetch did not complete",
                )
                self._last_result = "incomplete_fetch"
                return

            if forced_initial_rebuild or stored_profile_semantics:
                hourly_energy = sorted(fetched_hourly_energy)
            else:
                merge_records = (
                    _utc_hour_aligned_records(records)
                    if alignment_rebuild_required
                    else records
                )
                hourly_energy = merge_hourly_pv_energy(
                    merge_records,
                    fetched_hourly_energy,
                    start_date,
                    today,
                    local_tz,
                )
            if (
                alignment_rebuild_required
                or profile_schema_rebuild_required
            ):
                preserved_statistics = (
                    _preserved_daily_pv_statistics_before(
                        records,
                        start_date,
                        local_tz,
                    )
                    if self.pv_power_point is None
                    and not stored_profile_semantics
                    else _preserved_pv_statistics_before(
                        records,
                        start_date,
                        local_tz,
                    )
                )
            else:
                preserved_statistics = []
            if preserved_statistics:
                # A forced legacy rebuild normally starts at zero.  When the
                # same statistic also contains misaligned rows, however, the
                # valid pre-window rows are deliberately reimported.  Continue
                # from their last cumulative value so the replacement cannot
                # introduce a falling sum at the rebuild boundary.
                baseline_sum = max(
                    baseline_sum,
                    float(preserved_statistics[-1]["sum"]),
                )
            statistics = build_hourly_pv_statistics(
                hourly_energy,
                baseline_sum,
            )
            if (
                alignment_rebuild_required
                or profile_schema_rebuild_required
            ):
                statistics = [
                    *preserved_statistics,
                    *statistics,
                ]
            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name="smart1 EMS PV production",
                source=DOMAIN,
                statistic_id=self.statistic_id,
                unit_class=EnergyConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            expected_marker_version = (
                self.history_state.current_schema_version(self.statistic_id)
                if self.history_state
                else 0
            )

            def _enqueue_statistics() -> None:
                async_add_external_statistics(
                    hass=self.hass,
                    metadata=metadata,
                    statistics=statistics,
                )

            statistics_queued = False
            recorder = get_instance(self.hass)
            if destructive_rebuild_required:

                def _mark_rebuild_complete() -> None:
                    # A successful clear is the complete Recorder operation
                    # only when there is no replacement batch.  Non-empty
                    # replacements are marked after queue synchronization and
                    # an explicit statistics readback below.
                    if (
                        persistence_generation != self._persistence_generation
                    ):
                        return
                    if not statistics and self.history_state and (
                        self.history_state.mark_complete_if_unchanged(
                            self.statistic_id,
                            self.schema_version,
                            has_data=False,
                            expected_version=expected_marker_version,
                        )
                    ):
                        self._migration_required = False

                def _finalize_late_rebuild() -> None:
                    if not statistics:
                        return
                    self._schedule_persistence_task(
                        self._async_finalize_late_clear(
                            recorder,
                            statistics,
                            expected_marker_version,
                            persistence_generation,
                        )
                    )

                if not await async_clear_statistics(
                    recorder,
                    [self.statistic_id],
                    validate_followup=lambda: (
                        validate_energy_statistics_imports(
                            [(metadata, statistics)]
                        )
                    ),
                    enqueue_followup=(
                        _enqueue_statistics if statistics else None
                    ),
                    on_done=_mark_rebuild_complete,
                    on_late_done=_finalize_late_rebuild,
                ):
                    _LOGGER.warning(
                        "Timed out while clearing smart1 PV history before "
                        "a required rebuild; replacement data is already "
                        "queued"
                    )
                    self._last_result = "clear_timeout"
                    return
                records = []
                statistics_queued = bool(statistics)

            if not statistics:
                _LOGGER.debug("No smart1 PV history available for import")
                if fetch_completed and not initial_backfill_complete:
                    self._mark_complete(has_data=bool(records))
                    self._migration_required = False
                self._last_result = "no_data"
                return

            if not statistics_queued:
                _enqueue_statistics()
            _LOGGER.info(
                "Imported %d days of smart1 PV history",
                len(statistics),
            )
            completion_needs_update = (
                self.history_state is not None
                and (
                    (fetch_completed and not initial_backfill_complete)
                    or self._data_presence() is not True
                    or alignment_rebuild_required
                )
            )
            if completion_needs_update:
                persistence_confirmed = (
                    await self._async_confirm_statistics_persistence(
                        recorder,
                        statistics,
                        expected_marker_version,
                        persistence_generation,
                    )
                )
                if not persistence_confirmed:
                    _LOGGER.warning(
                        "Deferring smart1 PV history completion marker until "
                        "Recorder persistence can be verified"
                    )
                    self._last_result = "persistence_pending"
                    if persistence_generation == self._persistence_generation:
                        self._schedule_persistence_task(
                            self._async_finalize_late_clear(
                                recorder,
                                list(statistics),
                                expected_marker_version,
                                persistence_generation,
                            )
                        )
                    return
            elif self.history_state is None:
                # Without persistent schema state there is no marker to
                # protect.  The queued import remains the complete operation.
                self._migration_required = False

            if daily_fallback_days:
                self._last_result = "completed_with_daily_fallback"
            elif fetch_completed:
                self._last_result = "completed"
            else:
                self._last_result = "completed_with_partial_fetch"
