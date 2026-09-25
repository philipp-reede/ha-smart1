"""Import energy derived from selected smart1 five-minute power points."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
import logging
from math import fsum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from .energy_roles import ENERGY_ROLES_BY_KEY, statistic_id_for_role
from .history import (
    HISTORY_DAYS,
    MAX_HOURLY_RECORDS_PER_DAY,
    REFRESH_DAYS,
    history_date_range,
    history_repair_start,
)
from .history_state import (
    DERIVED_FRACTIONAL_OFFSET_SCHEMA_VERSION,
    DERIVED_HISTORY_SCHEMA_VERSION,
    Smart1HistoryState,
    recorded_source_time_zone,
    source_time_zone_requires_audit,
    source_time_zone_requires_rebuild,
)
from .point import Smart1Point
from .power_integration import (
    integrate_power_samples,
    normalize_power_rows,
)
from .recorder_helpers import (
    async_clear_statistics,
    async_wait_for_recorder_commit,
    async_wait_for_statistics_readback,
    statistics_are_persisted,
    validate_energy_statistics_imports,
)

_LOGGER = logging.getLogger(__name__)

# ``get_last_statistics`` counts records rather than days.  Derived history is
# hourly, so retain enough rows to reconstruct the complete supported history
# when a legacy statistic must be cleared and written again.
STATISTICS_LOOKBACK = HISTORY_DAYS * MAX_HOURLY_RECORDS_PER_DAY
FETCH_ATTEMPTS = 3


def _local_day_bounds_utc(
    target_date: date,
    local_tz: ZoneInfo,
) -> tuple[datetime, datetime]:
    """Return one local day's half-open UTC interval."""
    start = datetime.combine(
        target_date,
        datetime.min.time(),
        tzinfo=local_tz,
    ).astimezone(timezone.utc)
    end = datetime.combine(
        target_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=local_tz,
    ).astimezone(timezone.utc)
    return start, end


def _hour_overlaps_local_date_range(
    start: datetime,
    start_date: date,
    end_date: date,
    local_tz: ZoneInfo,
) -> bool:
    """Return whether one UTC-hour bucket intersects the local date range."""
    range_start, _unused = _local_day_bounds_utc(start_date, local_tz)
    _unused, range_end = _local_day_bounds_utc(end_date, local_tz)
    utc_start = start.astimezone(timezone.utc)
    return utc_start < range_end and utc_start + timedelta(hours=1) > range_start


def _utc_hours_overlapping_local_dates(
    local_dates: set[date],
    local_tz: ZoneInfo,
) -> set[datetime]:
    """Return UTC-hour starts intersecting any supplied local source day."""
    result: set[datetime] = set()
    for target_date in local_dates:
        day_start, day_end = _local_day_bounds_utc(target_date, local_tz)
        hour_start = day_start.replace(minute=0, second=0, microsecond=0)
        while hour_start < day_end:
            result.add(hour_start)
            hour_start += timedelta(hours=1)
    return result


def _local_dates_with_interval_energy(
    start: datetime,
    end: datetime,
    local_tz: ZoneInfo,
) -> set[date]:
    """Return local dates intersected by a positive-duration interval."""
    utc_start = start.astimezone(timezone.utc)
    utc_end = end.astimezone(timezone.utc)
    if utc_end <= utc_start:
        return set()

    result: set[date] = set()
    target_date = utc_start.astimezone(local_tz).date()
    final_date = utc_end.astimezone(local_tz).date()
    while target_date <= final_date:
        day_start, day_end = _local_day_bounds_utc(target_date, local_tz)
        if max(utc_start, day_start) < min(utc_end, day_end):
            result.add(target_date)
        target_date += timedelta(days=1)
    return result


def _boundary_context_dates(
    target_date: date,
    local_tz: ZoneInfo,
) -> set[date]:
    """Return adjacent source days needed for complete UTC boundary hours."""
    day_start, day_end = _local_day_bounds_utc(target_date, local_tz)
    context: set[date] = set()
    if day_start.minute or day_start.second or day_start.microsecond:
        context.add(target_date - timedelta(days=1))
    if day_end.minute or day_end.second or day_end.microsecond:
        context.add(target_date + timedelta(days=1))
    return context


def incomplete_boundary_hours(
    records: list[Mapping[str, Any]],
    source_days: set[date],
    completed_boundary_dates: set[date],
    start_date: date,
    end_date: date,
    local_tz: ZoneInfo,
) -> set[datetime]:
    """Return stored UTC buckets without a fresh cross-midnight interval."""
    existing_hours = {_statistics_start(record) for record in records}
    result: set[datetime] = set()
    boundary_date = start_date
    # Check the opening boundary of every requested day and the closing
    # boundary after the final day. Fractional-offset zones share the UTC
    # bucket containing local midnight. In whole-hour zones the cross-day
    # interval belongs to the preceding UTC bucket instead.
    while boundary_date <= end_date + timedelta(days=1):
        boundary_start, _boundary_end = _local_day_bounds_utc(
            boundary_date,
            local_tz,
        )
        fractional_boundary = (
            boundary_start.minute
            or boundary_start.second
            or boundary_start.microsecond
        )
        has_fresh_data = (
            boundary_date - timedelta(days=1) in source_days
            or boundary_date in source_days
        )
        if fractional_boundary:
            boundary_hour = boundary_start.replace(
                minute=0,
                second=0,
                microsecond=0,
            )
        else:
            boundary_hour = boundary_start - timedelta(hours=1)
        if (
            has_fresh_data
            and boundary_date not in completed_boundary_dates
            and boundary_hour in existing_hours
        ):
            result.add(boundary_hour)
        boundary_date += timedelta(days=1)
    return result


def missing_whole_hour_boundary_context(
    records: list[Mapping[str, Any]],
    source_days: set[date],
    completed_boundary_dates: set[date],
    start_date: date,
    end_date: date,
    local_tz: ZoneInfo,
) -> bool:
    """Return whether stored neighbouring data lacks fresh retry context."""
    existing_dates = {
        _statistics_start(record).astimezone(local_tz).date()
        for record in records
        if record.get("state") is not None
    }
    target_date = start_date
    while target_date <= end_date:
        day_start, _day_end = _local_day_bounds_utc(target_date, local_tz)
        if (
            target_date in source_days
            and day_start.minute == 0
            and day_start.second == 0
            and day_start.microsecond == 0
            and (
                (
                    target_date not in completed_boundary_dates
                    and target_date - timedelta(days=1) in existing_dates
                )
                or (
                    target_date + timedelta(days=1)
                    not in completed_boundary_dates
                    and target_date + timedelta(days=1) in existing_dates
                )
            )
        ):
            return True
        target_date += timedelta(days=1)
    return False


def _uses_fractional_utc_offset(
    local_tz: ZoneInfo,
    start_date: date,
    end_date: date,
) -> bool:
    """Return whether any local day uses a non-whole-hour UTC offset."""
    target_date = start_date
    # Include the midnight immediately after the range: a transition can make
    # only the closing boundary of the final local day fractional.
    while target_date <= end_date + timedelta(days=1):
        local_midnight = datetime.combine(
            target_date,
            datetime.min.time(),
            tzinfo=local_tz,
        )
        offset = local_midnight.utcoffset()
        if offset is not None and offset.total_seconds() % 3600:
            return True
        target_date += timedelta(days=1)
    return False


def normalize_hourly_energy(
    hourly_energy: list[tuple[datetime, float]],
) -> list[tuple[datetime, float]]:
    """Add all fragments that belong to the same UTC-hour bucket."""
    fragments_by_hour: defaultdict[datetime, list[float]] = defaultdict(list)
    for start, energy_kwh in hourly_energy:
        utc_start = start.astimezone(timezone.utc)
        fragments_by_hour[utc_start].append(energy_kwh)
    return sorted(
        (start, fsum(fragments))
        for start, fragments in fragments_by_hour.items()
    )


def _statistics_start(record: Mapping[str, Any]) -> datetime:
    """Return a statistics record start as an aware UTC datetime."""
    start = record["start"]
    if isinstance(start, datetime):
        return start.astimezone(timezone.utc)
    return datetime.fromtimestamp(float(start), timezone.utc)


def _has_hourly_resolution(
    records: list[Mapping[str, Any]],
    local_tz: ZoneInfo,
) -> bool:
    """Return whether records contain at least one sub-daily timestamp."""
    return any(
        _statistics_start(record).astimezone(local_tz).hour != 0
        for record in records
    )


def _has_decreasing_sum(records: list[Mapping[str, Any]]) -> bool:
    """Return whether cumulative statistics decrease between records."""
    sums = [
        (start, float(record["sum"]))
        for record in records
        if record.get("sum") is not None
        for start in (_statistics_start(record),)
    ]
    ordered_sums = [value for _start, value in sorted(sums)]
    return any(
        current < previous - 1e-9
        for previous, current in zip(
            ordered_sums,
            ordered_sums[1:],
            strict=False,
        )
    )


def _supported_history_records(
    records: list[Mapping[str, Any]],
    today: date,
    local_tz: ZoneInfo,
) -> tuple[list[Mapping[str, Any]], Mapping[str, Any] | None]:
    """Return the repairable window and its one cumulative predecessor."""
    full_start = today - timedelta(days=HISTORY_DAYS - 1)
    window_records = [
        record
        for record in records
        if full_start
        <= _statistics_start(record).astimezone(local_tz).date()
        <= today
    ]
    preceding = [
        record
        for record in records
        if record.get("sum") is not None
        and _statistics_start(record).astimezone(local_tz).date()
        < full_start
    ]
    predecessor = max(preceding, key=_statistics_start) if preceding else None
    return window_records, predecessor


def needs_hourly_rebuild(
    records: list[Mapping[str, Any]],
    today: date,
    local_tz: ZoneInfo,
    *,
    initial_backfill_complete: bool = False,
) -> bool:
    """Return whether supported derived history requires a full repair."""
    if not records:
        return False

    window_records, predecessor = _supported_history_records(
        records,
        today,
        local_tz,
    )
    if not window_records:
        return not initial_backfill_complete

    sum_records = (
        [predecessor, *window_records]
        if predecessor is not None
        else window_records
    )
    return (
        _has_decreasing_sum(sum_records)
        or (
            not initial_backfill_complete
            and not _has_hourly_resolution(window_records, local_tz)
        )
    )


def determine_hourly_import_window(
    records: list[Mapping[str, Any]],
    today: date,
    local_tz: ZoneInfo,
    refresh_days: int = REFRESH_DAYS,
    *,
    initial_backfill_complete: bool = False,
    force_initial_rebuild: bool = False,
    force_supported_rebuild: bool = False,
    required_start: date | None = None,
) -> tuple[date, float]:
    """Return the hourly refresh start and preceding cumulative sum.

    Pre-marker daily-only statistics trigger a full import under the same
    statistic ID so Energy Dashboard configuration remains intact. Once the
    current backfill is complete, sparse midnight hours are valid.
    """
    initial_start = today - timedelta(days=HISTORY_DAYS - 1)
    if force_initial_rebuild:
        return initial_start, 0.0
    if force_supported_rebuild:
        window_start, _window_end = _local_day_bounds_utc(
            initial_start,
            local_tz,
        )
        preceding = [
            record
            for record in records
            if record.get("sum") is not None
            and _statistics_start(record) < window_start
        ]
        if preceding:
            predecessor = max(preceding, key=_statistics_start)
            return initial_start, float(predecessor["sum"])
        boundary = [
            record
            for record in records
            if record.get("sum") is not None
            and record.get("state") is not None
            and _statistics_start(record) >= window_start
        ]
        if boundary:
            first = min(boundary, key=_statistics_start)
            return initial_start, float(first["sum"]) - float(first["state"])
        return initial_start, 0.0
    if not records:
        if initial_backfill_complete:
            refresh_start = today - timedelta(days=refresh_days - 1)
            return min(refresh_start, required_start or refresh_start), 0.0
        return initial_start, 0.0
    if needs_hourly_rebuild(
        records,
        today,
        local_tz,
        initial_backfill_complete=initial_backfill_complete,
    ):
        _window_records, predecessor = _supported_history_records(
            records,
            today,
            local_tz,
        )
        if predecessor is not None:
            return initial_start, float(predecessor["sum"])

        boundary = [
            record
            for record in records
            if record.get("sum") is not None
            and record.get("state") is not None
            and _statistics_start(record).astimezone(local_tz).date()
            == initial_start
        ]
        if boundary:
            first = min(boundary, key=_statistics_start)
            return initial_start, float(first["sum"]) - float(first["state"])
        return initial_start, 0.0

    refresh_start = today - timedelta(days=refresh_days - 1)
    if required_start is not None:
        refresh_start = min(refresh_start, required_start)
    preceding = [
        record
        for record in records
        if record.get("sum") is not None
        and _statistics_start(record).astimezone(local_tz).date()
        < refresh_start
    ]
    if preceding:
        baseline = max(preceding, key=_statistics_start)
        return refresh_start, float(baseline["sum"])

    if required_start is not None and refresh_start == required_start:
        following = [
            record
            for record in records
            if record.get("sum") is not None
            and record.get("state") is not None
            and _statistics_start(record).astimezone(local_tz).date()
            >= refresh_start
        ]
        if following:
            first = min(following, key=_statistics_start)
            return (
                refresh_start,
                float(first["sum"]) - float(first["state"]),
            )

    oldest_date = min(
        _statistics_start(record).astimezone(local_tz).date()
        for record in records
    )
    return min(oldest_date, refresh_start), 0.0


def merge_hourly_energy(
    records: list[Mapping[str, Any]],
    fetched_energy: list[tuple[datetime, float]],
    start_date: date,
    end_date: date,
    local_tz: ZoneInfo,
    *,
    replacement_dates: set[date] | None = None,
    preserve_hours: set[datetime] | None = None,
    records_tz: ZoneInfo | None = None,
) -> list[tuple[datetime, float]]:
    """Replace fetched local days while preserving temporarily missing days.

    Home Assistant's external-statistics API upserts records but does not
    delete obsolete buckets. Explicit zero-value tombstones therefore replace
    stale hours when a day's derived distribution changes.
    """
    energy_by_hour: dict[datetime, float] = {}
    preserve_hours = {
        start.astimezone(timezone.utc) for start in (preserve_hours or set())
    }
    fetched_by_hour = {
        start: energy
        for start, energy in normalize_hourly_energy(fetched_energy)
        if start not in preserve_hours
    }
    if replacement_dates is None:
        replacement_dates = {
            start.astimezone(local_tz).date() for start in fetched_by_hour
        }
    records_tz = records_tz or local_tz
    replacement_hours = _utc_hours_overlapping_local_dates(
        replacement_dates,
        local_tz,
    )
    if records_tz != local_tz:
        replacement_hours |= _utc_hours_overlapping_local_dates(
            replacement_dates,
            records_tz,
        )
    replacement_hours -= preserve_hours

    for record in records:
        energy_kwh = record.get("state")
        if energy_kwh is None:
            continue

        start = _statistics_start(record)
        if (
            _hour_overlaps_local_date_range(
                start,
                start_date,
                end_date,
                local_tz,
            )
            or (
                records_tz != local_tz
                and _hour_overlaps_local_date_range(
                    start,
                    start_date,
                    end_date,
                    records_tz,
                )
            )
        ):
            energy_by_hour[start] = (
                0.0
                if start in replacement_hours and start not in fetched_by_hour
                else float(energy_kwh)
            )

    energy_by_hour.update(fetched_by_hour)
    return sorted(energy_by_hour.items())


def build_hourly_energy_statistics(
    hourly_energy: list[tuple[datetime, float]],
    baseline_sum: float,
) -> list[StatisticData]:
    """Build cumulative Home Assistant statistics from hourly kWh values."""
    statistics: list[StatisticData] = []
    cumulative_sum = baseline_sum

    for start, energy_kwh in normalize_hourly_energy(hourly_energy):
        cumulative_sum += energy_kwh
        statistics.append(
            StatisticData(
                start=start,
                state=energy_kwh,
                sum=cumulative_sum,
            )
        )

    return statistics


def baseline_before_first_hour(
    records: list[Mapping[str, Any]],
    hourly_energy: list[tuple[datetime, float]],
    fallback: float,
) -> float:
    """Return the cumulative sum immediately before the first imported hour."""
    if not hourly_energy:
        return fallback
    first_start = min(start.astimezone(timezone.utc) for start, _ in hourly_energy)
    same_hour = [
        record
        for record in records
        if record.get("sum") is not None
        and record.get("state") is not None
        and _statistics_start(record) == first_start
    ]
    if same_hour:
        first = same_hour[0]
        return float(first["sum"]) - float(first["state"])
    preceding = [
        record
        for record in records
        if record.get("sum") is not None
        and _statistics_start(record) < first_start
    ]
    if preceding:
        return float(max(preceding, key=_statistics_start)["sum"])
    return fallback


class Smart1DerivedEnergyImporter:
    """Import external statistics for explicitly selected power points."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: Smart1Api,
        role_points: dict[str, Smart1Point],
        *,
        statistics_namespace: str = "",
        history_state: Smart1HistoryState | None = None,
        force_initial_rebuild: bool = False,
    ) -> None:
        self.hass = hass
        self.api = api
        self.role_points = role_points
        self.statistics_namespace = statistics_namespace
        self.history_state = history_state
        self.force_initial_rebuild = force_initial_rebuild
        self._lock = asyncio.Lock()
        self._last_result = "not_started"
        self._last_mode: str | None = None
        self._last_fetch_completed: bool | None = None
        self._detected_rebuild_roles: tuple[str, ...] = ()
        self._cleared_rebuild_roles: tuple[str, ...] = ()
        self._roles_without_replacement: tuple[str, ...] = ()
        self._persistence_tasks: set[asyncio.Task[Any]] = set()
        self._persistence_generation = 0
        self._schema_version = DERIVED_HISTORY_SCHEMA_VERSION

    def _is_complete(self, statistic_id: str) -> bool:
        """Return whether a statistic completed its current backfill."""
        return bool(
            self.history_state
            and self.history_state.is_complete(
                statistic_id,
                self._schema_version,
            )
        )

    def _data_presence(self, statistic_id: str) -> bool | None:
        """Return whether a completed role backfill produced recorder rows."""
        if not self.history_state:
            return None
        return self.history_state.data_presence(
            statistic_id,
            self._schema_version,
        )

    def _checked_through(self, statistic_id: str) -> date | None:
        """Return the last fully queried day for one derived statistic."""
        if not self.history_state:
            return None
        return self.history_state.checked_through(
            statistic_id,
            self._schema_version,
        )

    def _mark_complete(
        self,
        statistic_id: str,
        *,
        has_data: bool,
        checked_through: date | None = None,
    ) -> None:
        """Persist completion of one derived statistic backfill."""
        if self.history_state:
            self.history_state.mark_complete(
                statistic_id,
                self._schema_version,
                has_data=has_data,
                checked_through=checked_through,
            )

    def _next_empty_retry_date(
        self,
        statistic_id: str,
        *,
        today: date,
        before: date,
    ) -> date | None:
        """Return at most one old empty day for one role this run."""
        if not self.history_state:
            return None
        return self.history_state.next_empty_retry_date(
            statistic_id,
            self._schema_version,
            oldest_supported=today - timedelta(days=HISTORY_DAYS - 1),
            before=before,
            today=today,
        )

    def _schedule_persistence_task(self, coroutine: Any) -> None:
        """Keep a late Recorder finalizer alive until it finishes."""
        create_task = getattr(self.hass, "async_create_task", None)
        task = (
            create_task(
                coroutine,
                "smart1 EMS derived history persistence finalizer",
            )
            if create_task is not None
            else asyncio.create_task(coroutine)
        )
        self._persistence_tasks.add(task)
        task.add_done_callback(self._persistence_tasks.discard)

    async def _async_confirm_statistics_persistence(
        self,
        recorder: Any,
        role_keys: set[str],
        statistic_ids: Mapping[str, str],
        statistics_by_role: Mapping[str, list[StatisticData]],
        expected_marker_versions: Mapping[str, int],
        persistence_generation: int,
        checked_through_by_role: Mapping[str, date | None] | None = None,
        empty_days_by_role: Mapping[str, set[date]] | None = None,
        nonempty_days_by_role: Mapping[str, set[date]] | None = None,
        retried_days: Mapping[str, date] | None = None,
        today: date | None = None,
        source_time_zone: str | None = None,
    ) -> set[str]:
        """Verify queued derived imports and complete only persisted roles."""
        if not role_keys or not self.history_state:
            return set()
        if persistence_generation != self._persistence_generation:
            return set()
        # Treat the queue barrier as advisory. A slow import may already be
        # running outside the queue when the barrier times out; only readback
        # proves whether its transaction committed.
        await async_wait_for_recorder_commit(recorder)
        if persistence_generation != self._persistence_generation:
            return set()

        async def _confirm_role(role_key: str) -> tuple[str, bool]:
            statistics = statistics_by_role[role_key]
            persisted = await async_wait_for_statistics_readback(
                statistics,
                lambda: self._existing_statistics(
                    statistic_ids[role_key],
                    STATISTICS_LOOKBACK + len(statistics) + 1,
                ),
                matcher=statistics_are_persisted,
            )
            if not persisted:
                return role_key, False
            if persistence_generation != self._persistence_generation:
                return role_key, False

            checked_through = (
                checked_through_by_role.get(role_key)
                if checked_through_by_role
                else None
            )
            empty_days = (
                empty_days_by_role.get(role_key, set())
                if empty_days_by_role
                else set()
            )
            nonempty_days = (
                nonempty_days_by_role.get(role_key, set())
                if nonempty_days_by_role
                else set()
            )
            retried_day = (
                retried_days.get(role_key) if retried_days else None
            )
            marked = self.history_state.commit_scan_if_unchanged(
                statistic_ids[role_key],
                self._schema_version,
                has_data=True,
                expected_version=expected_marker_versions[role_key],
                checked_through=checked_through,
                empty_days=empty_days,
                nonempty_days=nonempty_days,
                oldest_supported=(
                    today - timedelta(days=HISTORY_DAYS - 1)
                    if today is not None
                    else date.min
                ),
                retried_day=(
                    retried_day
                    if retried_day in (empty_days | nonempty_days)
                    else None
                ),
                checked_on=(today if retried_day is not None else None),
                source_time_zone=source_time_zone,
            )
            if not marked:
                marked = bool(
                    self.history_state.is_complete(
                        statistic_ids[role_key],
                        self._schema_version,
                    )
                    and self.history_state.data_presence(
                        statistic_ids[role_key],
                        self._schema_version,
                    )
                    is True
                    and (
                        checked_through is None
                        or (
                            self.history_state.checked_through(
                                statistic_ids[role_key],
                                self._schema_version,
                            )
                            or date.min
                        )
                        >= checked_through
                    )
                    and (
                        source_time_zone is None
                        or not source_time_zone_requires_audit(
                            self.history_state,
                            statistic_ids[role_key],
                            source_time_zone,
                        )
                    )
                )
            return role_key, marked

        results = await asyncio.gather(
            *(_confirm_role(role_key) for role_key in sorted(role_keys))
        )
        return {
            role_key for role_key, persisted in results if persisted
        }

    async def _async_finalize_late_clear(
        self,
        recorder: Any,
        role_keys: set[str],
        cleared_roles: set[str],
        statistic_ids: Mapping[str, str],
        statistics_by_role: Mapping[str, list[StatisticData]],
        expected_marker_versions: Mapping[str, int],
        persistence_generation: int,
        checked_through_by_role: Mapping[str, date | None] | None = None,
        empty_days_by_role: Mapping[str, set[date]] | None = None,
        nonempty_days_by_role: Mapping[str, set[date]] | None = None,
        retried_days: Mapping[str, date] | None = None,
        today: date | None = None,
        source_time_zone: str | None = None,
    ) -> None:
        """Finalize replacements after a delayed clear callback."""
        persisted_roles = await self._async_confirm_statistics_persistence(
            recorder,
            role_keys,
            statistic_ids,
            statistics_by_role,
            expected_marker_versions,
            persistence_generation,
            checked_through_by_role,
            empty_days_by_role,
            nonempty_days_by_role,
            retried_days,
            today,
            source_time_zone,
        )
        if persistence_generation != self._persistence_generation:
            return
        if role_keys <= persisted_roles:
            self._cleared_rebuild_roles = tuple(sorted(cleared_roles))
        if self._last_result not in {"clear_timeout", "persistence_pending"}:
            return
        self._last_result = (
            "completed"
            if role_keys <= persisted_roles
            else "persistence_pending"
        )

    @property
    def diagnostic_status(self) -> dict[str, Any]:
        """Return identifier-free runtime status for diagnostics."""
        return {
            "type": "derived_energy_history",
            "last_result": self._last_result,
            "last_mode": self._last_mode,
            "last_fetch_completed": self._last_fetch_completed,
            "detected_rebuild_roles": list(self._detected_rebuild_roles),
            "cleared_rebuild_roles": list(self._cleared_rebuild_roles),
            "roles_without_replacement": list(
                self._roles_without_replacement
            ),
            "forced_initial_rebuild": self.force_initial_rebuild,
        }

    async def _existing_statistics(
        self,
        statistic_id: str,
        record_count: int = STATISTICS_LOOKBACK,
    ) -> list[Mapping[str, Any]]:
        """Return recent records for one derived statistic."""
        result = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            record_count,
            statistic_id,
            True,
            {"state", "sum"},
        )
        return result.get(statistic_id, [])

    async def _fetch_hourly_energy(
        self,
        start_date: date,
        end_date: date,
        local_tz: ZoneInfo,
        *,
        include_following_boundary: bool = False,
    ) -> tuple[
        dict[str, list[tuple[datetime, float]]],
        bool,
        date | None,
        dict[str, set[date]],
        dict[str, set[date]],
    ]:
        """Fetch local days and integrate complete UTC boundary buckets.

        Adjacent responses are stitched with one carried sample so the
        five-minute interval across local midnight is retained without keeping
        a supported year's raw rows in memory. A fractional-offset zone loads
        the preceding local day to complete the first shared UTC bucket. A
        single-day retry loads both adjacent days in every time zone so its
        opening and closing intervals can be rebuilt.
        """
        hourly_energy = {role_key: [] for role_key in self.role_points}
        nonempty_days = {role_key: set() for role_key in self.role_points}
        completed_boundary_dates = {
            role_key: set() for role_key in self.role_points
        }
        carry_samples: dict[str, tuple[datetime, float]] = {}
        checked_through: date | None = None
        linear_ids = list(
            dict.fromkeys(point.id for point in self.role_points.values())
        )
        fractional_offset = _uses_fractional_utc_offset(
            local_tz,
            start_date - timedelta(days=1),
            end_date + timedelta(days=1),
        )
        # Fractional local midnight shares a UTC bucket with the preceding
        # source day. A bounded single-day retry also needs that predecessor
        # in every time zone: when D was previously empty, the final
        # D-1 23:55-to-D 00:00 interval could not have been integrated.
        # Ordinary whole-hour scans still start exactly at ``start_date``.
        target_date = (
            start_date - timedelta(days=1)
            if fractional_offset or include_following_boundary
            else start_date
        )
        fetch_end_date = end_date + (
            timedelta(days=1)
            if include_following_boundary
            else timedelta()
        )

        while target_date <= fetch_end_date:
            for attempt in range(FETCH_ATTEMPTS):
                try:
                    rows = await self.api.get_linear_detailed_rows(
                        linear_ids,
                        target_date=target_date,
                        missing_ok=True,
                    )
                    break
                except (ClientError, Smart1ApiError, TimeoutError) as err:
                    if attempt == FETCH_ATTEMPTS - 1:
                        _LOGGER.warning(
                            "Unable to import derived smart1 energy from "
                            "%s after %d attempts: %s",
                            target_date,
                            FETCH_ATTEMPTS,
                            describe_api_error(err),
                        )
                        return (
                            {
                                role_key: normalize_hourly_energy(energy)
                                for role_key, energy in hourly_energy.items()
                            },
                            False,
                            checked_through,
                            nonempty_days,
                            completed_boundary_dates,
                        )
                    await asyncio.sleep(2**attempt)

            for role_key, point in self.role_points.items():
                samples = normalize_power_rows(rows, point.id, local_tz)
                samples = tuple(
                    sample
                    for sample in samples
                    if sample[0].astimezone(local_tz).date() == target_date
                )
                if not samples:
                    continue

                integration = integrate_power_samples(samples)
                if integration.integrated_intervals:
                    nonempty_days[role_key].add(target_date)
                    hourly_energy[role_key].extend(
                        integration.hourly_energy_kwh
                    )

                carry = carry_samples.get(role_key)
                if carry is not None:
                    boundary_integration = integrate_power_samples(
                        (carry, samples[0])
                    )
                    if boundary_integration.integrated_intervals:
                        carry_date = carry[0].astimezone(local_tz).date()
                        sample_date = samples[0][0].astimezone(local_tz).date()
                        if carry_date != sample_date:
                            # Store the right-hand local date as the identity
                            # of this exact cross-midnight boundary. Day-level
                            # data presence cannot prove that the carry pair
                            # itself was within the permitted gap.
                            completed_boundary_dates[role_key].add(sample_date)
                        nonempty_days[role_key].update(
                            _local_dates_with_interval_energy(
                                carry[0],
                                samples[0][0],
                                local_tz,
                            )
                        )
                        hourly_energy[role_key].extend(
                            boundary_integration.hourly_energy_kwh
                        )

                carry_samples[role_key] = samples[-1]

            if start_date <= target_date <= end_date:
                checked_through = target_date
            target_date += timedelta(days=1)

        for role_key, energy in hourly_energy.items():
            hourly_energy[role_key] = normalize_hourly_energy(energy)
        return (
            hourly_energy,
            True,
            checked_through,
            nonempty_days,
            completed_boundary_dates,
        )

    async def async_import(
        self,
        refresh_days: int = REFRESH_DAYS,
        *,
        repair: bool = True,
    ) -> None:
        """Import initial derived history or refresh recent days."""
        if not self.role_points:
            return
        if self._lock.locked() and not repair:
            return

        async with self._lock:
            # Supersede pending readback finalizers from an older repair even
            # when both runs use the same schema.  This prevents stale empty-
            # day evidence from being committed after a newer repair found
            # data for that day.
            if repair:
                self._persistence_generation += 1
            persistence_generation = self._persistence_generation
            self._last_result = "running"
            self._last_mode = "history_repair" if repair else "current_day"
            self._last_fetch_completed = None
            source_time_zone = self.hass.config.time_zone
            local_tz = ZoneInfo(source_time_zone)
            today = datetime.now(local_tz).date()
            statistic_ids = {
                role_key: statistic_id_for_role(
                    role_key,
                    point.id,
                    self.statistics_namespace,
                )
                for role_key, point in self.role_points.items()
            }
            records_tz_by_role: dict[str, ZoneInfo] = {}
            for role_key, statistic_id in statistic_ids.items():
                recorded_time_zone = recorded_source_time_zone(
                    self.history_state,
                    statistic_id,
                    source_time_zone,
                )
                try:
                    records_tz_by_role[role_key] = ZoneInfo(
                        recorded_time_zone
                    )
                except ZoneInfoNotFoundError:
                    records_tz_by_role[role_key] = local_tz
            required_schema_version = DERIVED_HISTORY_SCHEMA_VERSION
            # A later Home Assistant time-zone change must never downgrade a
            # statistic that already completed the boundary-safe schema.
            persisted_schema_version = max(
                (
                    self.history_state.current_schema_version(statistic_id)
                    for statistic_id in statistic_ids.values()
                ),
                default=0,
            ) if self.history_state else 0
            self._schema_version = max(
                required_schema_version,
                persisted_schema_version,
            )
            record_count = (
                STATISTICS_LOOKBACK
                if repair
                else (refresh_days + 1) * MAX_HOURLY_RECORDS_PER_DAY
            )
            records_by_role = {
                role_key: await self._existing_statistics(
                    statistic_id,
                    record_count,
                )
                for role_key, statistic_id in statistic_ids.items()
            }
            complete_roles = {
                role_key
                for role_key, statistic_id in statistic_ids.items()
                if self._is_complete(statistic_id)
                and (
                    records_by_role[role_key]
                    or self._data_presence(statistic_id) is False
                )
            }
            roles_without_statistics = {
                role_key
                for role_key, records in records_by_role.items()
                if not records
            }
            detected_rebuild_roles = {
                role_key
                for role_key, records in records_by_role.items()
                if needs_hourly_rebuild(
                    records,
                    today,
                    local_tz,
                    initial_backfill_complete=(
                        role_key in complete_roles
                    ),
                )
            }
            # Legacy isolation is independent of later schema upgrades. Since
            # completion markers and this migration were introduced together,
            # any persisted marker proves that role crossed the one-time
            # isolation boundary already.
            legacy_forced_rebuild_roles = {
                role_key
                for role_key, statistic_id in statistic_ids.items()
                if self.force_initial_rebuild
                and (
                    not self.history_state
                    or self.history_state.current_schema_version(statistic_id)
                    == 0
                )
            }
            # Version 4 integrates the 23:55-to-00:00 interval across portal
            # day responses. Every older schema needs one supported-window
            # upsert, including whole-hour time zones and the fractional v3.
            schema_upgrade_roles = {
                role_key
                for role_key, statistic_id in statistic_ids.items()
                if self.history_state
                and self.history_state.current_schema_version(statistic_id)
                    < DERIVED_HISTORY_SCHEMA_VERSION
            }
            time_zone_rebuild_roles = {
                role_key
                for role_key, statistic_id in statistic_ids.items()
                if self.history_state
                and source_time_zone_requires_rebuild(
                    self.history_state,
                    statistic_id,
                    source_time_zone,
                )
            }
            time_zone_audit_roles = {
                role_key
                for role_key, statistic_id in statistic_ids.items()
                if self.history_state
                and source_time_zone_requires_audit(
                    self.history_state,
                    statistic_id,
                    source_time_zone,
                )
            }
            # Schema and time-zone migrations are supported-window upserts,
            # not whole-ID clears: older Recorder history remains intact.
            forced_rebuild_roles = legacy_forced_rebuild_roles
            detected_rebuild_roles |= (
                forced_rebuild_roles
                | schema_upgrade_roles
                | time_zone_audit_roles
            )
            self._detected_rebuild_roles = tuple(
                sorted(detected_rebuild_roles)
            )

            # Adopt valid pre-marker hourly storage. The independent coverage
            # cursor still requests one supported-window audit when needed;
            # legacy/rebuild data takes the full replacement path below.
            for role_key, records in records_by_role.items():
                if (
                    records
                    and role_key not in complete_roles
                    and role_key not in detected_rebuild_roles
                    and role_key not in forced_rebuild_roles
                ):
                    self._mark_complete(
                        statistic_ids[role_key],
                        has_data=True,
                    )
                    complete_roles.add(role_key)

            if repair:
                rebuild_roles = detected_rebuild_roles
                refreshable_roles = set(self.role_points)
            else:
                rebuild_roles = set()
                refreshable_roles = (
                    set(self.role_points)
                    - detected_rebuild_roles
                    - (roles_without_statistics - complete_roles)
                )
                if not refreshable_roles:
                    self._last_result = "repair_pending"
                    return
            forced_clear_roles = forced_rebuild_roles & rebuild_roles
            required_starts = {
                role_key: (
                    history_repair_start(
                        today,
                        self._checked_through(statistic_ids[role_key]),
                    )
                    if repair and self.history_state
                    else None
                )
                for role_key in refreshable_roles
            }
            windows = {
                role_key: determine_hourly_import_window(
                    records,
                    today,
                    local_tz,
                    refresh_days,
                    initial_backfill_complete=(
                        role_key in complete_roles
                    ),
                    force_initial_rebuild=(
                        role_key in forced_rebuild_roles
                    ),
                    force_supported_rebuild=(
                        role_key in schema_upgrade_roles
                        or role_key in time_zone_audit_roles
                    ),
                    required_start=required_starts[role_key],
                )
                for role_key, records in records_by_role.items()
                if role_key in refreshable_roles
            }
            main_start_date = min(window[0] for window in windows.values())
            retry_dates = {
                role_key: retry_date
                for role_key in refreshable_roles
                if repair
                and role_key in complete_roles
                and role_key not in rebuild_roles
                and (
                    retry_date := self._next_empty_retry_date(
                        statistic_ids[role_key],
                        today=today,
                        before=windows[role_key][0],
                    )
                )
                is not None
            }
            main_fetch_result = await self._fetch_hourly_energy(
                main_start_date,
                today,
                local_tz,
            )
            (
                fetched_by_role,
                fetch_completed,
                *main_checked,
            ) = main_fetch_result
            main_checked_through = (
                main_checked[0]
                if main_checked
                else (today if fetch_completed else None)
            )
            main_nonempty_source_days = (
                {
                    role_key: set(source_days)
                    for role_key, source_days in main_checked[1].items()
                }
                if len(main_checked) > 1
                else {}
            )
            main_completed_boundary_dates = (
                {
                    role_key: set(boundary_dates)
                    for role_key, boundary_dates in main_checked[2].items()
                }
                if len(main_checked) > 2
                else {}
            )
            preserve_boundary_hours_by_role: dict[str, set[datetime]] = {
                role_key: set() for role_key in refreshable_roles
            }
            retry_opening_hours_by_role: dict[str, set[datetime]] = {
                role_key: set() for role_key in refreshable_roles
            }
            fractional_window = _uses_fractional_utc_offset(
                local_tz,
                main_start_date - timedelta(days=1),
                today,
            )
            for role_key in refreshable_roles:
                replacement_days = main_nonempty_source_days.get(role_key)
                if replacement_days is None:
                    continue
                role_start = windows[role_key][0]
                # One successful-empty API source day leaves its neighbouring
                # bucket partial. Fractional-offset zones share the UTC hour
                # containing local midnight; whole-hour zones lose the final
                # five-minute interval in the preceding hour. Preserve an
                # existing complete value in either direction.
                preserve_boundary_hours_by_role[role_key].update(
                    incomplete_boundary_hours(
                        records_by_role[role_key],
                        replacement_days,
                        main_completed_boundary_dates.get(role_key, set()),
                        role_start,
                        # The regular refresh has no D+1 context for
                        # ``today``. Do not preserve tomorrow's shared
                        # boundary: its current-day fragment is still
                        # expected to advance during late-evening polls.
                        today - timedelta(days=1),
                        local_tz,
                    )
                )
            main_fetched_by_role = {
                role_key: list(fetched_energy)
                for role_key, fetched_energy in fetched_by_role.items()
            }
            completed_retry_roles: set[str] = set()
            partial_retry_roles: set[str] = set()
            retry_nonempty_source_days: dict[str, set[date]] = {
                role_key: set() for role_key in refreshable_roles
            }
            if fetch_completed:
                for retry_date in sorted(set(retry_dates.values())):
                    retry_fetch_result = await self._fetch_hourly_energy(
                        retry_date,
                        retry_date,
                        local_tz,
                        include_following_boundary=True,
                    )
                    (
                        retry_fetched_by_role,
                        retry_completed,
                        *retry_details,
                    ) = retry_fetch_result
                    retry_nonempty_days_by_role = (
                        {
                            role_key: set(source_days)
                            for role_key, source_days in retry_details[1].items()
                        }
                        if len(retry_details) > 1
                        else {}
                    )
                    retry_completed_boundaries_by_role = (
                        {
                            role_key: set(boundary_dates)
                            for role_key, boundary_dates in (
                                retry_details[2].items()
                            )
                        }
                        if len(retry_details) > 2
                        else {}
                    )
                    if not retry_completed:
                        continue
                    for role_key, role_retry_date in retry_dates.items():
                        if role_retry_date != retry_date:
                            continue
                        role_nonempty_days = retry_nonempty_days_by_role.get(
                            role_key
                        )
                        has_source_provenance = role_nonempty_days is not None
                        if role_nonempty_days is None:
                            # Compatibility for tests and third-party wrappers
                            # mocking the legacy three-item fetch result.
                            role_nonempty_days = {
                                retry_date
                                for start, _energy in (
                                    retry_fetched_by_role[role_key]
                                )
                                if start.astimezone(local_tz).date()
                                == retry_date
                            }
                        completed_boundary_dates = (
                            retry_completed_boundaries_by_role.get(
                                role_key, set()
                            )
                        )
                        completed_retry_roles.add(role_key)
                        retry_nonempty_source_days[role_key].update(
                            role_nonempty_days
                        )
                        if retry_date not in role_nonempty_days:
                            continue

                        retry_preserve_hours: set[datetime] = set()
                        if has_source_provenance:
                            # Sparse consumers commonly have legitimately
                            # empty neighbouring days. Their absence must not
                            # discard the safe interior hours recovered for D;
                            # preserve only existing UTC buckets whose two
                            # local-day fragments are not both available.
                            retry_preserve_hours = incomplete_boundary_hours(
                                records_by_role[role_key],
                                role_nonempty_days,
                                completed_boundary_dates,
                                retry_date,
                                retry_date,
                                local_tz,
                            )
                            preserve_boundary_hours_by_role[role_key].update(
                                retry_preserve_hours
                            )
                            boundary_context_missing = (
                                missing_whole_hour_boundary_context(
                                    records_by_role[role_key],
                                    role_nonempty_days,
                                    completed_boundary_dates,
                                    retry_date,
                                    retry_date,
                                    local_tz,
                                )
                            )
                            if (
                                retry_preserve_hours
                                or boundary_context_missing
                            ):
                                # The safe interior buckets can be imported,
                                # but keep D in the bounded retry rotation
                                # until both boundary fragments can be rebuilt.
                                partial_retry_roles.add(role_key)

                            day_start, _day_end = _local_day_bounds_utc(
                                retry_date,
                                local_tz,
                            )
                            if (
                                day_start.minute == 0
                                and day_start.second == 0
                                and day_start.microsecond == 0
                                and retry_date in completed_boundary_dates
                            ):
                                # In a whole-hour zone the opening interval
                                # belongs to the final UTC bucket of D-1, so
                                # it does not geometrically overlap D. Keep it
                                # explicitly when both source days are known.
                                retry_opening_hours_by_role[role_key].add(
                                    day_start - timedelta(hours=1)
                                )

                        # The context fetch contains D-1/D/D+1. Replace
                        # buckets intersecting D plus the whole-hour opening
                        # bucket from D-1, using fully combined values so an
                        # upsert cannot discard either neighbour.
                        retry_buckets = {
                            start: energy
                            for start, energy in normalize_hourly_energy(
                                retry_fetched_by_role[role_key]
                            )
                            if _hour_overlaps_local_date_range(
                                start,
                                retry_date,
                                retry_date,
                                local_tz,
                            )
                            or start
                            in retry_opening_hours_by_role[role_key]
                        }
                        if has_source_provenance:
                            # The main scan can preserve a boundary solely
                            # because it starts after D. A context-rich retry
                            # for D supersedes that earlier uncertainty for
                            # every bucket it rebuilt completely. Retain only
                            # boundaries that the retry itself still proved
                            # incomplete.
                            preserve_boundary_hours_by_role[
                                role_key
                            ].difference_update(
                                set(retry_buckets) - retry_preserve_hours
                            )
                        combined = dict(
                            normalize_hourly_energy(
                                fetched_by_role[role_key]
                            )
                        )
                        combined.update(retry_buckets)
                        fetched_by_role[role_key] = sorted(combined.items())
            for role_key, energy in fetched_by_role.items():
                fetched_by_role[role_key] = normalize_hourly_energy(energy)
            self._last_fetch_completed = fetch_completed
            initial_backfill_roles = refreshable_roles - complete_roles
            if initial_backfill_roles and not fetch_completed:
                _LOGGER.warning(
                    "Deferring initial smart1 derived energy import because "
                    "the history fetch did not complete",
                )
                self._last_result = "incomplete_fetch"
                return

            coverage_targets = {
                role_key: main_checked_through
                for role_key in refreshable_roles
                if main_checked_through is not None
                and main_checked_through >= windows[role_key][0]
            }
            empty_days_by_role: dict[str, set[date]] = {}
            nonempty_days_by_role: dict[str, set[date]] = {}
            checked_days_by_role: dict[str, set[date]] = {}
            nonempty_retry_roles: set[str] = set()
            for role_key in refreshable_roles:
                source_days = main_nonempty_source_days.get(role_key)
                if source_days is None:
                    # Compatibility for mocked legacy fetch results. Real API
                    # imports always use exact source-day evidence above.
                    source_days = {
                        start.astimezone(local_tz).date()
                        for start, _energy in main_fetched_by_role[role_key]
                    }
                main_nonempty_days = {
                    source_day
                    for source_day in source_days
                    if source_day >= windows[role_key][0]
                }
                checked_days = (
                    history_date_range(
                        windows[role_key][0],
                        main_checked_through,
                    )
                    if role_key in coverage_targets
                    else set()
                )
                checked_days_by_role[role_key] = checked_days
                retry_nonempty_days = (
                    {retry_dates[role_key]}
                    if role_key in completed_retry_roles
                    and retry_dates[role_key]
                    in retry_nonempty_source_days[role_key]
                    else set()
                )
                empty_days = checked_days - main_nonempty_days
                if role_key in completed_retry_roles:
                    if retry_nonempty_days:
                        nonempty_retry_roles.add(role_key)
                        retry_required_start = retry_dates[role_key]
                        if retry_opening_hours_by_role[role_key]:
                            retry_required_start = max(
                                retry_required_start - timedelta(days=1),
                                today - timedelta(days=HISTORY_DAYS - 1),
                            )
                        windows[role_key] = determine_hourly_import_window(
                            records_by_role[role_key],
                            today,
                            local_tz,
                            refresh_days,
                            initial_backfill_complete=True,
                            required_start=retry_required_start,
                        )
                        if role_key in partial_retry_roles:
                            empty_days.add(retry_dates[role_key])
                    else:
                        empty_days.add(retry_dates[role_key])
                empty_days_by_role[role_key] = empty_days
                nonempty_days_by_role[role_key] = (
                    main_nonempty_days
                    | (
                        set()
                        if role_key in partial_retry_roles
                        else retry_nonempty_days
                    )
                )
            if rebuild_roles and not fetch_completed:
                _LOGGER.warning(
                    "Keeping existing smart1 energy statistics because the "
                    "replacement history fetch did not complete",
                )
                self._last_result = "incomplete_fetch"
                return

            # A completed repair can legitimately contain empty days (or be
            # completely empty) because the detailed endpoint is queried with
            # ``missing_ok=True``. Only explicit legacy rebuilds clear the
            # complete statistic. Detected daily/decreasing data is repaired
            # by upserting the supported window so older valid history stays
            # untouched.
            rebuildable_roles = rebuild_roles
            if repair:
                self._roles_without_replacement = ()
                self._cleared_rebuild_roles = ()

            statistics_by_role: dict[str, list[StatisticData]] = {}
            metadata_by_role: dict[str, StatisticMetaData] = {}
            roles_with_statistics: set[str] = set()
            for role_key, point in self.role_points.items():
                if role_key not in refreshable_roles:
                    continue
                role_start, baseline_sum = windows[role_key]
                fetched_energy = [
                    item
                    for item in fetched_by_role[role_key]
                    if _hour_overlaps_local_date_range(
                        item[0],
                        role_start,
                        today,
                        local_tz,
                    )
                ]
                if role_key in forced_rebuild_roles:
                    hourly_energy = sorted(fetched_energy)
                else:
                    hourly_energy = merge_hourly_energy(
                        records_by_role[role_key],
                        fetched_energy,
                        role_start,
                        today,
                        local_tz,
                        replacement_dates=(
                            checked_days_by_role[role_key]
                            if role_key in time_zone_rebuild_roles
                            and fetch_completed
                            else nonempty_days_by_role[role_key]
                        ),
                        preserve_hours=(
                            set()
                            if role_key in time_zone_rebuild_roles
                            else preserve_boundary_hours_by_role[role_key]
                        ),
                        records_tz=records_tz_by_role[role_key],
                    )
                if (
                    fractional_window
                    or role_key in time_zone_rebuild_roles
                ) and role_key not in forced_rebuild_roles:
                    baseline_sum = baseline_before_first_hour(
                        records_by_role[role_key],
                        hourly_energy,
                        baseline_sum,
                    )
                statistics = build_hourly_energy_statistics(
                    hourly_energy,
                    baseline_sum,
                )
                if not statistics:
                    continue

                role = ENERGY_ROLES_BY_KEY[role_key]
                statistics_by_role[role_key] = statistics
                metadata_by_role[role_key] = StatisticMetaData(
                    mean_type=StatisticMeanType.NONE,
                    has_sum=True,
                    name=f"{role.name} ({point.name})",
                    source=DOMAIN,
                    statistic_id=statistic_ids[role_key],
                    unit_class=EnergyConverter.UNIT_CLASS,
                    unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                )
                roles_with_statistics.add(role_key)

            def _enqueue_statistics(role_keys: set[str]) -> None:
                for role_key in sorted(role_keys):
                    if role_key not in statistics_by_role:
                        continue
                    statistics = statistics_by_role[role_key]
                    async_add_external_statistics(
                        hass=self.hass,
                        metadata=metadata_by_role[role_key],
                        statistics=statistics,
                    )
                    _LOGGER.info(
                        "Queued %d hourly records for smart1 derived "
                        "energy role %s",
                        len(statistics),
                        role_key,
                    )

            recorder = get_instance(self.hass)
            expected_marker_versions = {
                role_key: self.history_state.current_schema_version(
                    statistic_ids[role_key]
                )
                for role_key in refreshable_roles
            } if self.history_state else {}
            if forced_clear_roles:
                rebuild_statistic_ids = [
                    statistic_ids[role_key]
                    for role_key in sorted(forced_clear_roles)
                ]

                def _mark_rebuild_complete() -> None:
                    if not self.history_state:
                        return
                    if (
                        persistence_generation != self._persistence_generation
                    ):
                        return
                    # Empty rebuilds finish with the clear itself.  A role
                    # with replacement rows is completed only after the
                    # queued import has been read back from Recorder.
                    for role_key in (
                        forced_clear_roles - roles_with_statistics
                    ):
                        self.history_state.commit_scan_if_unchanged(
                            statistic_ids[role_key],
                            self._schema_version,
                            has_data=False,
                            expected_version=(
                                expected_marker_versions[role_key]
                            ),
                            checked_through=(
                                today if fetch_completed and repair else None
                            ),
                            empty_days=empty_days_by_role[role_key],
                            nonempty_days=set(),
                            oldest_supported=(
                                today - timedelta(days=HISTORY_DAYS - 1)
                            ),
                            checked_on=None,
                            source_time_zone=source_time_zone,
                        )

                late_rebuild_roles = (
                    forced_clear_roles & roles_with_statistics
                )

                def _finalize_late_rebuild() -> None:
                    if not late_rebuild_roles or not self.history_state:
                        return
                    self._schedule_persistence_task(
                        self._async_finalize_late_clear(
                            recorder,
                            set(late_rebuild_roles),
                            set(forced_clear_roles),
                            dict(statistic_ids),
                            {
                                role_key: list(statistics_by_role[role_key])
                                for role_key in late_rebuild_roles
                            },
                            {
                                role_key: expected_marker_versions[role_key]
                                for role_key in late_rebuild_roles
                            },
                            persistence_generation,
                            {
                                role_key: (
                                    today if fetch_completed and repair else None
                                )
                                for role_key in late_rebuild_roles
                            },
                            empty_days_by_role,
                            nonempty_days_by_role,
                            retry_dates,
                            today,
                            source_time_zone,
                        )
                    )

                if not await async_clear_statistics(
                    recorder,
                    rebuild_statistic_ids,
                    validate_followup=lambda: (
                        validate_energy_statistics_imports(
                            [
                                (
                                    metadata_by_role[role_key],
                                    statistics_by_role[role_key],
                                )
                                for role_key in sorted(forced_clear_roles)
                                if role_key in statistics_by_role
                            ]
                        )
                    ),
                    enqueue_followup=lambda: _enqueue_statistics(
                        forced_clear_roles
                    ),
                    on_done=_mark_rebuild_complete,
                    on_late_done=_finalize_late_rebuild,
                ):
                    _LOGGER.warning(
                        "Timed out while clearing smart1 derived energy "
                        "statistics before a required rebuild; replacement "
                        "data is already queued"
                    )
                    self._last_result = "clear_timeout"
                    return
                self._cleared_rebuild_roles = tuple(
                    sorted(forced_clear_roles)
                )
                _LOGGER.info(
                    "Cleared %d smart1 derived energy statistics before rebuild",
                    len(rebuild_statistic_ids),
                )

            _enqueue_statistics(refreshable_roles - forced_rebuild_roles)

            completed_roles = (
                initial_backfill_roles | rebuildable_roles
                if fetch_completed
                else set()
            )
            marker_update_roles = set()
            if self.history_state:
                marker_update_roles = {
                    role_key
                    for role_key in roles_with_statistics
                    if (
                        (
                            role_key in completed_roles
                            and (
                                not self._is_complete(
                                    statistic_ids[role_key]
                                )
                                or self._data_presence(
                                    statistic_ids[role_key]
                                ) is not True
                            )
                        )
                        or (
                            role_key not in completed_roles
                            and self._data_presence(
                                statistic_ids[role_key]
                            ) is not True
                        )
                        or (
                            role_key in coverage_targets
                            and repair
                            and required_starts[role_key] is not None
                        )
                        or role_key in completed_retry_roles
                        or role_key in time_zone_audit_roles
                    )
                }

            persisted_roles = (
                await self._async_confirm_statistics_persistence(
                    recorder,
                    marker_update_roles,
                    statistic_ids,
                    statistics_by_role,
                    expected_marker_versions,
                    persistence_generation,
                    {
                        role_key: (
                            coverage_targets.get(role_key)
                            if repair
                            else None
                        )
                        for role_key in marker_update_roles
                    },
                    empty_days_by_role,
                    nonempty_days_by_role,
                    retry_dates,
                    today,
                    source_time_zone,
                )
            )

            pending_marker_roles = marker_update_roles - persisted_roles
            for role_key in completed_roles:
                has_data = (
                    role_key in roles_with_statistics
                    or (
                        role_key not in forced_rebuild_roles
                        and bool(records_by_role[role_key])
                    )
                )
                if role_key in marker_update_roles:
                    # Confirmation already wrote the generation-guarded
                    # marker. Failed roles remain pending below.
                    continue
                if not self.history_state:
                    continue
                self.history_state.commit_scan_if_unchanged(
                    statistic_ids[role_key],
                    self._schema_version,
                    has_data=has_data,
                    expected_version=expected_marker_versions[role_key],
                    checked_through=(
                        coverage_targets.get(role_key) if repair else None
                    ),
                    empty_days=empty_days_by_role[role_key],
                    nonempty_days=nonempty_days_by_role[role_key],
                    oldest_supported=(
                        today - timedelta(days=HISTORY_DAYS - 1)
                    ),
                    retried_day=(
                        retry_dates.get(role_key)
                        if role_key in completed_retry_roles
                        else None
                    ),
                    checked_on=(
                        today
                        if role_key in completed_retry_roles
                        else None
                    ),
                    source_time_zone=source_time_zone,
                )
            for role_key in roles_with_statistics - completed_roles:
                if self._data_presence(statistic_ids[role_key]) is not True:
                    # Confirmation above owns this marker update as well.
                    continue

            if repair:
                for role_key in refreshable_roles - roles_with_statistics:
                    checked_through = coverage_targets.get(role_key)
                    if checked_through is None:
                        continue
                    statistic_id = statistic_ids[role_key]
                    if role_key in completed_roles:
                        # The completion loop above atomically persisted this.
                        continue
                    if not self.history_state:
                        continue
                    self.history_state.commit_scan_if_unchanged(
                        statistic_id,
                        self._schema_version,
                        has_data=(
                            self._data_presence(statistic_id) is True
                        ),
                        expected_version=expected_marker_versions[role_key],
                        checked_through=checked_through,
                        empty_days=empty_days_by_role[role_key],
                        nonempty_days=nonempty_days_by_role[role_key],
                        oldest_supported=(
                            today - timedelta(days=HISTORY_DAYS - 1)
                        ),
                        retried_day=(
                            retry_dates.get(role_key)
                            if role_key in completed_retry_roles
                            else None
                        ),
                        checked_on=(
                            today
                            if role_key in completed_retry_roles
                            else None
                        ),
                        source_time_zone=source_time_zone,
                    )

            if pending_marker_roles:
                _LOGGER.warning(
                    "Deferring smart1 derived energy completion markers for "
                    "roles %s until Recorder persistence can be verified",
                    ", ".join(sorted(pending_marker_roles)),
                )
                self._last_result = "persistence_pending"
                if persistence_generation == self._persistence_generation:
                    self._schedule_persistence_task(
                        self._async_finalize_late_clear(
                            recorder,
                            set(pending_marker_roles),
                            set(forced_clear_roles),
                            dict(statistic_ids),
                            {
                                role_key: list(
                                    statistics_by_role[role_key]
                                )
                                for role_key in pending_marker_roles
                            },
                            {
                                role_key: expected_marker_versions[role_key]
                                for role_key in pending_marker_roles
                            },
                            persistence_generation,
                            {
                                role_key: (
                                    coverage_targets.get(role_key)
                                    if repair
                                    else None
                                )
                                for role_key in pending_marker_roles
                            },
                            empty_days_by_role,
                            nonempty_days_by_role,
                            retry_dates,
                            today,
                            source_time_zone,
                        )
                    )
                return

            self._last_result = (
                "completed"
                if fetch_completed
                else "completed_with_partial_fetch"
            )
