"""Import historical smart1 photovoltaic production into Home Assistant."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
import logging
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
    statistics_during_period,
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
    UNJOURNALED_PV_HISTORY_SCHEMA_VERSIONS,
    recorded_source_time_zone,
    scoped_statistic_id,
    source_time_zone_requires_audit,
    source_time_zone_requires_rebuild,
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
PV_STATISTICS_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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


def _has_positive_statistics_on_dates_in_any_zone(
    records: list[Mapping[str, Any]],
    local_dates: set[date],
    local_zones: tuple[ZoneInfo, ...],
) -> bool:
    """Return whether one positive row may map to an empty source day.

    During recovery from a journaled time-zone migration Recorder may contain
    either the source or target layout. A row is unsafe to replace when its
    local date lacks fresh source data under either possible interpretation:
    the replacement merge clears the union of those layouts as well. A
    genuinely ambiguous successful-empty day therefore remains pending rather
    than risking deletion of valid energy.
    """
    if not local_dates or not local_zones:
        return False
    for record in records:
        state = record.get("state")
        if (
            not isinstance(state, (int, float))
            or isinstance(state, bool)
            or float(state) <= 0.0
        ):
            continue
        if any(
            _statistics_date(record, local_tz) in local_dates
            for local_tz in local_zones
        ):
            return True
    return False


def _positive_daily_energy_from_records(
    records: list[Mapping[str, Any]],
    local_tz: ZoneInfo,
    target_dates: set[date],
    *,
    daily_only: bool,
) -> dict[date, float]:
    """Return exact non-negative stored totals for selected local days."""
    records_by_date: dict[date, list[Mapping[str, Any]]] = {}
    for record in records:
        target_date = _statistics_date(record, local_tz)
        if target_date in target_dates:
            records_by_date.setdefault(target_date, []).append(record)

    totals: dict[date, float] = {}
    for target_date, day_records in records_by_date.items():
        aligned = _utc_hour_aligned_records(day_records)
        if aligned:
            # A daily-mode statistic may still contain a preserved historical
            # hourly profile from an earlier hourly -> daily switch. Sum that
            # profile instead of treating its final hour as the daily total.
            # For a legacy daily row plus its repaired off-hour duplicate,
            # the one aligned row remains the authoritative replacement.
            candidates = aligned
        elif daily_only:
            candidates = [max(day_records, key=_statistics_start)]
        else:
            candidates = day_records if len(day_records) == 1 else []
        total = 0.0
        has_numeric_state = False
        for candidate in candidates:
            state = candidate.get("state")
            if state is None:
                continue
            try:
                energy_kwh = float(state)
            except (TypeError, ValueError):
                continue
            if energy_kwh >= 0.0:
                has_numeric_state = True
                total += energy_kwh
        if has_numeric_state:
            totals[target_date] = total
    return totals


def _positive_daily_energy_from_hourly(
    hourly_energy: list[tuple[datetime, float]],
    local_tz: ZoneInfo,
) -> dict[date, float]:
    """Return exact non-negative target-local daily totals from a profile."""
    totals: dict[date, float] = {}
    for start, energy_kwh in hourly_energy:
        if energy_kwh < 0.0:
            continue
        target_date = start.astimezone(local_tz).date()
        totals[target_date] = totals.get(target_date, 0.0) + energy_kwh
    return totals


def _remap_unchecked_source_energy(
    records: list[Mapping[str, Any]],
    checked_dates: set[date],
    records_tz: ZoneInfo,
    target_tz: ZoneInfo,
    *,
    daily_only: bool,
    preserve_hourly_profile: bool = False,
) -> list[tuple[datetime, float]]:
    """Aggregate retained source-local days into the target layout.

    A proven time-zone change replaces the complete statistic. Portal data is
    authoritative for every successfully checked source date; all other dates
    must be retained from Recorder. Rebuilding their cumulative sums later is
    essential because a remapped future edge may sort after the refreshed
    supported window even though its old cumulative value was smaller.
    """
    records_by_date: dict[date, list[Mapping[str, Any]]] = {}
    for record in records:
        source_date = _statistics_date(record, records_tz)
        if source_date in checked_dates:
            continue
        records_by_date.setdefault(source_date, []).append(record)

    totals: dict[date, float] = {}
    retained_profile: dict[datetime, float] = {}
    for source_date, day_records in records_by_date.items():
        aligned = _utc_hour_aligned_records(day_records)
        if (
            preserve_hourly_profile
            and records_tz == target_tz
            and aligned
        ):
            for candidate in aligned:
                state = candidate.get("state")
                if state is None:
                    continue
                try:
                    energy_kwh = float(state)
                except (TypeError, ValueError):
                    continue
                if energy_kwh >= 0.0:
                    retained_profile[_statistics_start(candidate)] = energy_kwh
            continue
        if aligned:
            # A completed daily schema can retain pre-window hourly profiles
            # from a previous mode switch. Preserve their complete total. A
            # lone aligned row also wins over an obsolete off-hour daily row.
            candidates = aligned
        elif daily_only:
            # Multiple off-hour rows are legacy/repaired daily duplicates;
            # the later timestamp is the authoritative exact total.
            candidates = [max(day_records, key=_statistics_start)]
        else:
            # Hourly profiles can coexist with an obsolete off-hour daily
            # spike. Preserve the aligned profile only. A lone non-aligned row
            # still represents an exact daily fallback and remains recoverable.
            candidates = day_records if len(day_records) == 1 else []
        total = 0.0
        has_numeric_state = False
        for candidate in candidates:
            state = candidate.get("state")
            if state is None:
                continue
            try:
                energy_kwh = float(state)
            except (TypeError, ValueError):
                continue
            if energy_kwh >= 0.0:
                has_numeric_state = True
                total += energy_kwh
        if has_numeric_state:
            totals[source_date] = total
    remapped_daily = [
        (
            _first_utc_hour_in_local_day(source_date, target_tz),
            energy_kwh,
        )
        for source_date, energy_kwh in sorted(totals.items())
    ]
    return sorted([*retained_profile.items(), *remapped_daily])


def _baseline_before_all_records(
    records: list[Mapping[str, Any]],
) -> float:
    """Return the cumulative baseline before a whole-statistic remap."""
    numeric: list[tuple[datetime, float, float]] = []
    for record in records:
        state = record.get("state")
        cumulative_sum = record.get("sum")
        if state is None or cumulative_sum is None:
            continue
        try:
            numeric.append(
                (
                    _statistics_start(record),
                    float(state),
                    float(cumulative_sum),
                )
            )
        except (TypeError, ValueError):
            continue
    if not numeric:
        return 0.0
    _start, state, cumulative_sum = min(numeric, key=lambda item: item[0])
    return cumulative_sum - state


def _apply_daily_energy_fallbacks(
    hourly_energy: list[tuple[datetime, float]],
    fallback_daily_energy: Mapping[date, float],
    empty_days: set[date],
    local_tz: ZoneInfo,
    *,
    additional_dates: set[date] | None = None,
) -> list[tuple[datetime, float]]:
    """Replace ambiguous empty target days with journaled exact totals."""
    eligible_dates = empty_days | (additional_dates or set())
    active_fallbacks = {
        target_date: float(energy_kwh)
        for target_date, energy_kwh in fallback_daily_energy.items()
        if target_date in eligible_dates and float(energy_kwh) >= 0.0
    }
    if not active_fallbacks:
        return hourly_energy
    kept = [
        (start, energy_kwh)
        for start, energy_kwh in hourly_energy
        if start.astimezone(local_tz).date() not in active_fallbacks
    ]
    kept.extend(
        (
            _first_utc_hour_in_local_day(target_date, local_tz),
            energy_kwh,
        )
        for target_date, energy_kwh in active_fallbacks.items()
    )
    return sorted(kept)


def _pending_fallback_daily_energy(pending: Any) -> dict[date, float]:
    """Return fallback totals from a production or lightweight journal."""
    getter = getattr(pending, "fallback_energy_by_date", None)
    if callable(getter):
        return getter()
    raw_fallbacks = getattr(pending, "fallback_daily_energy", ())
    if isinstance(raw_fallbacks, Mapping):
        return dict(raw_fallbacks)
    try:
        return dict(raw_fallbacks)
    except (TypeError, ValueError):
        return {}


def _pending_has_complete_daily_fallback(pending: Any) -> bool:
    """Return whether a pending journal also proves zero target days."""
    complete = getattr(pending, "has_complete_fallback", None)
    if complete is not None:
        return bool(complete)
    return getattr(pending, "fallback_daily_energy", None) is not None


def _pending_replacement_hourly_energy(
    pending: Any,
) -> dict[datetime, float]:
    """Return an exact replacement profile from a production/test journal."""
    getter = getattr(pending, "replacement_energy_by_start", None)
    if callable(getter):
        return getter()
    raw_replacement = getattr(pending, "replacement_hourly_energy", ())
    if isinstance(raw_replacement, Mapping):
        return dict(raw_replacement)
    try:
        return dict(raw_replacement)
    except (TypeError, ValueError):
        return {}


def _pending_has_complete_replacement(pending: Any) -> bool:
    """Return whether an exact (possibly empty) replacement was journaled."""
    complete = getattr(pending, "has_complete_replacement", None)
    if complete is not None:
        return bool(complete)
    return getattr(pending, "replacement_hourly_energy", None) is not None


def _pending_has_invalid_replacement(pending: Any) -> bool:
    """Return whether an exact journal field was present but malformed."""
    invalid = getattr(pending, "has_invalid_replacement", None)
    if invalid is not None:
        return bool(invalid)
    return bool(
        getattr(pending, "replacement_hourly_energy_invalid", False)
    )


def determine_import_window(
    records: list[Mapping[str, Any]],
    today: date,
    local_tz: ZoneInfo,
    refresh_days: int = REFRESH_DAYS,
    *,
    initial_backfill_complete: bool = False,
    required_start: date | None = None,
) -> tuple[date, float]:
    """Return the first date to refresh and its preceding cumulative sum."""
    if not records:
        if initial_backfill_complete:
            refresh_start = today - timedelta(days=refresh_days - 1)
            return min(refresh_start, required_start or refresh_start), 0.0
        return today - timedelta(days=HISTORY_DAYS - 1), 0.0

    refresh_start = today - timedelta(days=refresh_days - 1)
    if required_start is not None:
        refresh_start = min(refresh_start, required_start)
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

    if required_start is not None and refresh_start == required_start:
        following = [
            record
            for record in records
            if record.get("sum") is not None
            and record.get("state") is not None
            and _statistics_date(record, local_tz) >= refresh_start
        ]
        if following:
            first = min(following, key=_statistics_start)
            return (
                refresh_start,
                float(first["sum"]) - float(first["state"]),
            )

    oldest_date = min(_statistics_date(record, local_tz) for record in records)
    return min(oldest_date, refresh_start), 0.0


def history_repair_start(
    today: date,
    checked_through: date | None,
) -> date | None:
    """Return the first unchecked supported history day, if any."""
    if checked_through is not None and checked_through >= today:
        return None
    full_start = today - timedelta(days=HISTORY_DAYS - 1)
    if checked_through is None:
        return full_start
    return max(full_start, checked_through + timedelta(days=1))


def history_date_range(start_date: date, end_date: date) -> set[date]:
    """Return every calendar date in one inclusive history interval."""
    if start_date > end_date:
        return set()
    return {
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    }


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
    *,
    records_tz: ZoneInfo | None = None,
) -> list[tuple[datetime, float]]:
    """Merge a stored hourly profile into a daily-only replacement batch."""
    records_tz = records_tz or local_tz
    fetched_by_date = dict(fetched_energy)
    records_by_date: dict[date, list[Mapping[str, Any]]] = {}
    for record in records:
        target_date = _statistics_date(record, records_tz)
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
            if records_tz != local_tz:
                # A profile-to-daily rebuild clears the complete statistic.
                # When the source zone changed at the same time, carrying
                # these old UTC profile buckets into the replacement would
                # make their source day change as soon as the new fingerprint
                # is committed. Preserve the measured total instead and map
                # it to the daily bucket of the same old source date in the
                # new zone. A later empty-day retry then replaces this exact
                # bucket without leaving an old-zone UTC tail behind.
                energy_kwh = sum(
                    float(state)
                    for record in aligned_records
                    if (state := record.get("state")) is not None
                )
                if energy_kwh > 1e-9:
                    energy_by_hour[
                        _first_utc_hour_in_local_day(target_date, local_tz)
                    ] = energy_kwh
                continue
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
    records_tz: ZoneInfo,
    *,
    target_tz: ZoneInfo | None = None,
    end_date: date | None = None,
) -> list[StatisticData]:
    """Keep profile rows outside the supported rebuild window.

    Whole-UTC-hour rows are genuine hourly profile buckets and remain
    unchanged.  A lone positive local-midnight row is a legacy exact-daily
    fallback; remap it only when its local day has no hourly profile at all.
    When the source time zone changed, aggregate each old source day and map
    its exact energy to the same calendar day in the new zone. This avoids
    dropping the old-zone UTC tail when the complete statistic is cleared.
    """
    target_tz = target_tz or records_tz
    preserved: dict[datetime, StatisticData] = {}
    records_by_date: dict[date, list[Mapping[str, Any]]] = {}
    for record in records:
        target_date = _statistics_date(record, records_tz)
        if (
            target_date >= start_date
            and (end_date is None or target_date <= end_date)
        ):
            continue
        records_by_date.setdefault(target_date, []).append(record)

    for target_date, day_records in records_by_date.items():
        aligned_records = _utc_hour_aligned_records(day_records)
        candidates = aligned_records
        remap_singleton = not aligned_records and len(day_records) == 1
        if remap_singleton:
            candidates = day_records

        if target_tz != records_tz and candidates:
            numeric_records: list[tuple[Mapping[str, Any], float, float]] = []
            for record in candidates:
                state = record.get("state")
                cumulative_sum = record.get("sum")
                if state is None or cumulative_sum is None:
                    continue
                try:
                    numeric_records.append(
                        (record, float(state), float(cumulative_sum))
                    )
                except (TypeError, ValueError):
                    continue
            if not numeric_records:
                continue
            energy_kwh = sum(item[1] for item in numeric_records)
            if remap_singleton and energy_kwh <= 1e-9:
                continue
            last_record = max(
                numeric_records,
                key=lambda item: _statistics_start(item[0]),
            )
            start = _first_utc_hour_in_local_day(target_date, target_tz)
            preserved[start] = StatisticData(
                start=start,
                state=energy_kwh,
                sum=last_record[2],
            )
            continue

        for record in candidates:
            original_start = _statistics_start(record)
            start = (
                _first_utc_hour_in_local_day(target_date, records_tz)
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
    records_tz: ZoneInfo,
    *,
    target_tz: ZoneInfo | None = None,
    end_date: date | None = None,
) -> list[StatisticData]:
    """Remap exact daily PV rows outside the supported rebuild window."""
    target_tz = target_tz or records_tz
    preserved: dict[date, tuple[datetime, StatisticData]] = {}
    for record in records:
        original_start = _statistics_start(record)
        target_date = original_start.astimezone(records_tz).date()
        if (
            target_date >= start_date
            and (end_date is None or target_date <= end_date)
        ):
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
                start=_first_utc_hour_in_local_day(target_date, target_tz),
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
    required_start: date | None = None,
    records_tz: ZoneInfo | None = None,
) -> tuple[date, float]:
    """Return the hourly PV refresh window and preceding cumulative sum."""
    full_start = today - timedelta(days=HISTORY_DAYS - 1)
    records_tz = records_tz or local_tz
    if force_initial_rebuild:
        return full_start, 0.0
    if not records:
        if initial_backfill_complete:
            refresh_start = today - timedelta(days=refresh_days - 1)
            return min(refresh_start, required_start or refresh_start), 0.0
        return full_start, 0.0
    if force_full_refresh or (
        not initial_backfill_complete
        and needs_hourly_pv_migration(records, records_tz)
    ):
        preceding = [
            record
            for record in records
            if record.get("sum") is not None
            and _statistics_date(record, records_tz) < full_start
        ]
        if preceding:
            baseline = max(preceding, key=_statistics_start)
            return full_start, float(baseline["sum"])

        boundary = [
            record
            for record in records
            if record.get("sum") is not None
            and record.get("state") is not None
            and _statistics_date(record, records_tz) == full_start
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
        required_start=required_start,
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
    *,
    replacement_dates: set[date] | None = None,
    records_tz: ZoneInfo | None = None,
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
    dates_to_replace = (
        replacement_dates
        if replacement_dates is not None
        else {
            start.astimezone(local_tz).date() for start in fetched_by_hour
        }
    )
    records_tz = records_tz or local_tz

    for record in records:
        energy_kwh = record.get("state")
        if energy_kwh is None:
            continue
        start = _statistics_start(record)
        old_target_date = start.astimezone(records_tz).date()
        new_target_date = start.astimezone(local_tz).date()
        if (
            start_date <= old_target_date <= end_date
            or start_date <= new_target_date <= end_date
        ):
            energy_by_hour[start] = (
                0.0
                if (
                    old_target_date in dates_to_replace
                    or new_target_date in dates_to_replace
                )
                and start not in fetched_by_hour
                else float(energy_kwh)
            )

    energy_by_hour.update(fetched_by_hour)
    return sorted(energy_by_hour.items())


def _baseline_before_first_pv_hour(
    records: list[Mapping[str, Any]],
    hourly_energy: list[tuple[datetime, float]],
    fallback: float,
) -> float:
    """Return the cumulative sum immediately before a remapped UTC seam."""
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

    def _checked_through(self) -> date | None:
        """Return the last day fully queried for the active PV schema."""
        if not self.history_state:
            return None
        return self.history_state.checked_through(
            self.statistic_id,
            self.schema_version,
        )

    def _mark_complete(
        self,
        *,
        has_data: bool,
        checked_through: date | None = None,
    ) -> None:
        """Persist completion of the current PV history schema."""
        if self.history_state:
            self.history_state.mark_complete(
                self.statistic_id,
                self.schema_version,
                has_data=has_data,
                checked_through=checked_through,
            )

    def _next_empty_retry_date(
        self,
        *,
        today: date,
        before: date,
    ) -> date | None:
        """Return at most one old empty day for this repair run."""
        if not self.history_state:
            return None
        return self.history_state.next_empty_retry_date(
            self.statistic_id,
            self.schema_version,
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
        checked_through: date | None = None,
        empty_days: set[date] | None = None,
        nonempty_days: set[date] | None = None,
        retried_day: date | None = None,
        today: date | None = None,
        source_time_zone: str | None = None,
        time_zone_migration_generation: str | None = None,
        reset_history_progress: bool = False,
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

        commit_kwargs = {
            "has_data": True,
            "expected_version": expected_marker_version,
            "checked_through": checked_through,
            "empty_days": empty_days or set(),
            "nonempty_days": nonempty_days or set(),
            "oldest_supported": (
                today - timedelta(days=HISTORY_DAYS - 1)
                if today is not None
                else date.min
            ),
            "retried_day": (
                retried_day
                if retried_day
                in ((empty_days or set()) | (nonempty_days or set()))
                else None
            ),
            "checked_on": (today if retried_day is not None else None),
            "source_time_zone": source_time_zone,
            "time_zone_migration_generation": (
                time_zone_migration_generation
            ),
            "reset_history_progress": reset_history_progress,
        }
        async_commit = getattr(
            self.history_state,
            "async_commit_scan_if_unchanged",
            None,
        )
        if callable(async_commit):
            marked = await async_commit(
                self.statistic_id,
                self.schema_version,
                **commit_kwargs,
            )
        else:
            marked = self.history_state.commit_scan_if_unchanged(
                self.statistic_id,
                self.schema_version,
                **commit_kwargs,
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
                and (
                    checked_through is None
                    or (
                        self.history_state.checked_through(
                            self.statistic_id,
                            self.schema_version,
                        )
                        or date.min
                    )
                    >= checked_through
                )
                and (
                    source_time_zone is None
                    or not source_time_zone_requires_audit(
                        self.history_state,
                        self.statistic_id,
                        source_time_zone,
                    )
                )
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
        checked_through: date | None = None,
        empty_days: set[date] | None = None,
        nonempty_days: set[date] | None = None,
        retried_day: date | None = None,
        today: date | None = None,
        source_time_zone: str | None = None,
        time_zone_migration_generation: str | None = None,
        reset_history_progress: bool = False,
    ) -> None:
        """Finalize a replacement whose clear callback arrived late."""
        confirmed = await self._async_confirm_statistics_persistence(
            recorder,
            statistics,
            expected_marker_version,
            persistence_generation,
            checked_through,
            empty_days,
            nonempty_days,
            retried_day,
            today,
            source_time_zone,
            time_zone_migration_generation,
            reset_history_progress,
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

    async def _all_existing_statistics(self) -> list[Mapping[str, Any]]:
        """Return the complete long-term statistic before a whole-ID clear."""
        result = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            PV_STATISTICS_EPOCH,
            None,
            {self.statistic_id},
            "hour",
            None,
            {"state", "sum"},
        )
        return result.get(self.statistic_id, [])

    async def _fetch_daily_energy(
        self,
        start_date: date,
        end_date: date,
    ) -> tuple[list[tuple[date, float]], bool, date | None]:
        """Fetch daily production without failing on dates with no data."""
        daily_energy: list[tuple[date, float]] = []
        checked_through: date | None = None
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
                        return daily_energy, False, checked_through
                    await asyncio.sleep(2**attempt)

            if value is not None:
                daily_energy.append((target_date, value))

            checked_through = target_date

            target_date += timedelta(days=1)

        return daily_energy, True, checked_through

    async def _fetch_hourly_energy(
        self,
        start_date: date,
        end_date: date,
        local_tz: ZoneInfo,
        exact_fallback: Mapping[date, float],
    ) -> tuple[list[tuple[datetime, float]], bool, int, int, date | None]:
        """Fetch and normalize exact PV totals into measured hourly buckets."""
        assert self.pv_power_point is not None
        hourly_energy: list[tuple[datetime, float]] = []
        completed = True
        distributed_days = 0
        daily_fallback_days = 0
        checked_through: date | None = None
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
                checked_through = target_date
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
            checked_through = target_date
            target_date += timedelta(days=1)

        return (
            hourly_energy,
            completed,
            distributed_days,
            daily_fallback_days,
            checked_through,
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
            # Every repair run supersedes pending readback finalizers from an
            # older repair.  Schema-version checks alone are insufficient:
            # two normal catch-up runs use the same schema, and the older
            # finalizer could otherwise re-add stale empty-day evidence.
            if repair:
                self._persistence_generation += 1
            persistence_generation = self._persistence_generation
            self._last_result = "running"
            self._last_refresh_days = refresh_days
            configured_time_zone = self.hass.config.time_zone
            pending_time_zone_migration = (
                self.history_state.pending_time_zone_migration(
                    self.statistic_id
                )
                if self.history_state
                and callable(
                    getattr(
                        self.history_state,
                        "pending_time_zone_migration",
                        None,
                    )
                )
                else None
            )
            pending_fallback_complete = bool(
                pending_time_zone_migration is not None
                and _pending_has_complete_daily_fallback(
                    pending_time_zone_migration
                )
            )
            # A crash can happen either before or after Recorder applies the
            # queued replacement. Finish the journaled source -> target step
            # first, even when Home Assistant has meanwhile changed to a third
            # zone. A later repair then performs target -> configured zone.
            source_time_zone = (
                pending_time_zone_migration.target_time_zone
                if pending_time_zone_migration is not None
                else configured_time_zone
            )
            local_tz = ZoneInfo(source_time_zone)
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
            # The legacy multi-installation isolation is a one-time operation,
            # independent of later storage-schema changes. Completion markers
            # and that migration flag were introduced together, so any
            # persisted schema proves this statistic has already passed the
            # isolation boundary. A later hourly/daily switch must preserve
            # the now installation-specific pre-window history.
            stored_schema_version = (
                self.history_state.current_schema_version(self.statistic_id)
                if self.history_state
                else 0
            )
            recorded_time_zone = (
                pending_time_zone_migration.source_time_zone
                if pending_time_zone_migration is not None
                else recorded_source_time_zone(
                    self.history_state,
                    self.statistic_id,
                    source_time_zone,
                )
            )
            try:
                records_tz = ZoneInfo(recorded_time_zone)
            except ZoneInfoNotFoundError:
                records_tz = local_tz
            legacy_layout_canonicalization_required = bool(
                records
                and stored_schema_version
                in UNJOURNALED_PV_HISTORY_SCHEMA_VERSIONS
            )
            time_zone_rebuild_required = bool(
                pending_time_zone_migration is not None
                or legacy_layout_canonicalization_required
                or (
                    self.history_state
                    and source_time_zone_requires_rebuild(
                        self.history_state,
                        self.statistic_id,
                        source_time_zone,
                    )
                )
            )
            time_zone_audit_required = bool(
                self.history_state
                and source_time_zone_requires_audit(
                    self.history_state,
                    self.statistic_id,
                    source_time_zone,
                )
            )
            forced_initial_rebuild = (
                self.force_initial_rebuild
                and stored_schema_version == 0
            )
            alignment_rebuild_required = needs_utc_hour_alignment_rebuild(
                records
            )
            stored_daily_schema = stored_schema_version in {
                2,
                6,
                PV_DAILY_HISTORY_SCHEMA_VERSION,
            }
            switching_from_daily_schema = bool(
                self.pv_power_point is not None
                and self.history_state
                and stored_daily_schema
            )
            switching_from_hourly_schema = bool(
                self.pv_power_point is None
                and stored_schema_version
                in {
                    4,
                    5,
                    PV_HISTORY_SCHEMA_VERSION,
                }
            )
            current_daily_schema = (
                stored_schema_version == PV_DAILY_HISTORY_SCHEMA_VERSION
            )
            records_have_hourly_profile = _has_hourly_profile_semantics(
                records,
                records_tz,
            )
            stored_profile_semantics = bool(
                self.pv_power_point is None
                and not current_daily_schema
                and (
                    switching_from_hourly_schema
                    or records_have_hourly_profile
                )
            )
            stored_daily_semantics = bool(
                stored_daily_schema
                or switching_from_daily_schema
                or (
                    not records_have_hourly_profile
                    and needs_hourly_pv_migration(records, records_tz)
                )
            )
            profile_schema_rebuild_required = stored_profile_semantics
            destructive_rebuild_required = (
                forced_initial_rebuild
                or alignment_rebuild_required
                or profile_schema_rebuild_required
                or time_zone_rebuild_required
            )
            journaled_rebuild_required = (
                alignment_rebuild_required
                or profile_schema_rebuild_required
                or time_zone_rebuild_required
            )
            if (
                repair
                and not forced_initial_rebuild
                and destructive_rebuild_required
                and len(records) >= record_count
            ):
                # A whole-ID clear removes every row, not only the bounded
                # lookback used for normal repair detection. Read the complete
                # statistic before the clear so older valid history can be
                # reimported as part of the replacement batch.
                records = await self._all_existing_statistics()
            self._migration_required = (
                alignment_rebuild_required
                or profile_schema_rebuild_required
                or time_zone_rebuild_required
                or time_zone_audit_required
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
                                    records_tz,
                                )
                            )
                        )
                    )
                )
            )

            # Existing hourly data predates the persistent completion marker.
            # Adopt its storage schema here. The independent coverage cursor
            # still requests one complete supported-window audit when needed.
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

            required_start = (
                history_repair_start(today, self._checked_through())
                if repair and self.history_state
                else None
            )

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
                    or time_zone_rebuild_required
                    or time_zone_audit_required
                ),
                required_start=required_start,
                records_tz=records_tz,
            )
            main_start_date = start_date
            retry_date = (
                self._next_empty_retry_date(
                    today=today,
                    before=main_start_date,
                )
                if repair
                and initial_backfill_complete
                and not (
                    destructive_rebuild_required
                    or time_zone_audit_required
                )
                else None
            )

            if self.pv_power_point is None:
                main_fetch_result = await self._fetch_daily_energy(
                    main_start_date,
                    today,
                )
                if len(main_fetch_result) == 2:
                    fetched_daily_energy, fetch_completed = main_fetch_result
                    main_checked_through = today if fetch_completed else None
                else:
                    (
                        fetched_daily_energy,
                        fetch_completed,
                        main_checked_through,
                    ) = main_fetch_result
                main_fetched_daily_energy = list(fetched_daily_energy)
                retry_completed = False
                retry_fetched_daily_energy: list[tuple[date, float]] = []
                if fetch_completed and retry_date is not None:
                    retry_fetch_result = await self._fetch_daily_energy(
                        retry_date,
                        retry_date,
                    )
                    (
                        retry_fetched_daily_energy,
                        retry_completed,
                        *_retry_checked,
                    ) = retry_fetch_result
                    if retry_completed:
                        fetched_daily_energy.extend(
                            retry_fetched_daily_energy
                        )
                portal_daily_fallback_days = sum(
                    energy_kwh > 0
                    for _target_date, energy_kwh in fetched_daily_energy
                )
                if stored_profile_semantics and not forced_initial_rebuild:
                    # A recovered old empty day sits outside the normal main
                    # refresh window.  Include it in the profile-to-daily
                    # replacement range before building the batch; widening
                    # only the later merge window would drop the fetched value
                    # while still allowing its retry state to be committed.
                    profile_merge_start = min(
                        start_date,
                        min(
                            (
                                target_date
                                for target_date, _energy in (
                                    retry_fetched_daily_energy
                                )
                            ),
                            default=start_date,
                        ),
                    )
                    fetched_hourly_energy = (
                        _merge_profile_history_for_daily_rebuild(
                            records,
                            fetched_daily_energy,
                            profile_merge_start,
                            today,
                            local_tz,
                            records_tz=records_tz,
                        )
                    )
                else:
                    if (
                        alignment_rebuild_required
                        and not forced_initial_rebuild
                    ):
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
                            records_tz,
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
                # During journal recovery Recorder may contain either the
                # source or target layout. Never reinterpret those ambiguous
                # rows as source-local exact totals. Query the portal first;
                # a successful-empty day is restored later from the durable
                # target-date fallback instead.
                exact_fallback = (
                    _daily_exact_fallback(records, records_tz)
                    if pending_time_zone_migration is None
                    and self._migration_required
                    and not forced_initial_rebuild
                    and not time_zone_rebuild_required
                    else {}
                )
                main_fetch_result = await self._fetch_hourly_energy(
                    main_start_date,
                    today,
                    local_tz,
                    exact_fallback,
                )
                (
                    fetched_hourly_energy,
                    fetch_completed,
                    distributed_days,
                    daily_fallback_days,
                    *main_checked,
                ) = main_fetch_result
                main_checked_through = (
                    main_checked[0]
                    if main_checked
                    else (today if fetch_completed else None)
                )
                main_fetched_hourly_energy = list(fetched_hourly_energy)
                retry_completed = False
                retry_fetched_hourly_energy: list[
                    tuple[datetime, float]
                ] = []
                if fetch_completed and retry_date is not None:
                    (
                        retry_fetched_hourly_energy,
                        retry_completed,
                        retry_distributed_days,
                        retry_daily_fallback_days,
                        *_retry_checked,
                    ) = await self._fetch_hourly_energy(
                        retry_date,
                        retry_date,
                        local_tz,
                        exact_fallback,
                    )
                    if retry_completed:
                        fetched_hourly_energy.extend(
                            retry_fetched_hourly_energy
                        )
                        distributed_days += retry_distributed_days
                        daily_fallback_days += retry_daily_fallback_days

            if self.pv_power_point is None:
                main_nonempty_days = {
                    target_date
                    for target_date, _energy in main_fetched_daily_energy
                }
                retry_nonempty_days = {
                    target_date
                    for target_date, _energy in retry_fetched_daily_energy
                }
            else:
                main_nonempty_days = {
                    start.astimezone(local_tz).date()
                    for start, _energy in main_fetched_hourly_energy
                }
                retry_nonempty_days = {
                    start.astimezone(local_tz).date()
                    for start, _energy in retry_fetched_hourly_energy
                }
            main_checked_days = (
                history_date_range(
                    main_start_date,
                    main_checked_through,
                )
                if main_checked_through is not None
                else set()
            )
            empty_days = main_checked_days - main_nonempty_days
            if retry_completed and retry_date is not None:
                if retry_date in retry_nonempty_days:
                    nonempty_retry_date = retry_date
                else:
                    empty_days.add(retry_date)
                    nonempty_retry_date = None
            else:
                nonempty_retry_date = None
            persisted_nonempty_days = (
                main_nonempty_days | retry_nonempty_days
            )
            migration_fallback_daily_energy: dict[date, float] = {}
            pending_clear_rebuild_baseline = (
                getattr(
                    pending_time_zone_migration,
                    "clear_rebuild_baseline_sum",
                    None,
                )
                if pending_time_zone_migration is not None
                else None
            )
            pending_fallback_daily_energy = (
                _pending_fallback_daily_energy(
                    pending_time_zone_migration
                )
                if pending_time_zone_migration is not None
                else {}
            )
            pending_replacement_hourly_energy = (
                _pending_replacement_hourly_energy(
                    pending_time_zone_migration
                )
                if pending_time_zone_migration is not None
                else {}
            )
            pending_identity_rebuild = bool(
                pending_time_zone_migration is not None
                and pending_time_zone_migration.source_time_zone
                == pending_time_zone_migration.target_time_zone
            )
            pending_replacement_complete = bool(
                pending_time_zone_migration is not None
                and _pending_has_complete_replacement(
                    pending_time_zone_migration
                )
            )
            pending_replacement_invalid = bool(
                pending_time_zone_migration is not None
                and _pending_has_invalid_replacement(
                    pending_time_zone_migration
                )
            )
            replaying_exact_replacement = bool(
                time_zone_rebuild_required
                and pending_time_zone_migration is not None
                and pending_replacement_complete
            )
            if (
                pending_time_zone_migration is not None
                and time_zone_rebuild_required
                and (
                    pending_clear_rebuild_baseline is None
                    or pending_replacement_invalid
                    or (
                        pending_identity_rebuild
                        and not pending_replacement_complete
                    )
                    or (
                        not pending_identity_rebuild
                        and not pending_replacement_complete
                        and not pending_fallback_complete
                    )
                    or (
                        not pending_replacement_complete
                        and not pending_fallback_daily_energy
                        and float(pending_clear_rebuild_baseline) != 0.0
                    )
                )
            ):
                # Every journal created by this release contains the complete
                # replacement totals and the cumulative baseline. Refuse a
                # malformed/incomplete entry before touching Recorder; unlike
                # guessing from the current rows, this is safe whether a queued
                # pre-crash clear already committed or not.
                _LOGGER.warning(
                    "Deferring smart1 PV time-zone migration because the "
                    "durable clear journal is incomplete"
                )
                self._last_result = "migration_journal_incomplete"
                return
            if time_zone_rebuild_required:
                if pending_time_zone_migration is not None:
                    migration_fallback_daily_energy = dict(
                        pending_fallback_daily_energy
                    )
                else:
                    migration_fallback_daily_energy = (
                        _positive_daily_energy_from_hourly(
                            fetched_hourly_energy,
                            local_tz,
                        )
                    )
                    for target_date, energy_kwh in (
                        _positive_daily_energy_from_records(
                            records,
                            records_tz,
                            empty_days,
                            daily_only=stored_daily_semantics,
                        ).items()
                    ):
                        migration_fallback_daily_energy[target_date] = (
                            energy_kwh
                        )
                if pending_replacement_complete:
                    # Finish the exact batch that was durably recorded before
                    # Recorder was cleared. Mixing a newer fetch into this
                    # ambiguous retry could lose cross-day boundary buckets;
                    # the next normal refresh safely applies newer portal data.
                    fetched_hourly_energy = sorted(
                        pending_replacement_hourly_energy.items()
                    )
                else:
                    fetched_hourly_energy = _apply_daily_energy_fallbacks(
                        fetched_hourly_energy,
                        migration_fallback_daily_energy,
                        empty_days,
                        local_tz,
                        additional_dates=(
                            set(migration_fallback_daily_energy)
                            - main_checked_days
                            if pending_time_zone_migration is not None
                            else set()
                        ),
                    )
                if pending_time_zone_migration is None:
                    # A whole-ID clear must recreate every row, not only the
                    # portal-supported window. Aggregate all unchecked source
                    # days by their original calendar date and map those exact
                    # totals into the target zone. On a pending retry the same
                    # rows come from the durable complete fallback instead.
                    fetched_hourly_energy.extend(
                        _remap_unchecked_source_energy(
                            records,
                            main_checked_days,
                            records_tz,
                            local_tz,
                            daily_only=stored_daily_semantics,
                            preserve_hourly_profile=(
                                self.pv_power_point is not None
                            ),
                        )
                    )
                    fetched_hourly_energy = sorted(
                        dict(fetched_hourly_energy).items()
                    )
            if nonempty_retry_date is not None:
                start_date, baseline_sum = (
                    determine_hourly_pv_import_window(
                        records,
                        today,
                        local_tz,
                        refresh_days,
                        initial_backfill_complete=(
                            initial_backfill_complete
                        ),
                        required_start=nonempty_retry_date,
                        records_tz=records_tz,
                    )
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
                or time_zone_rebuild_required
                or time_zone_audit_required
            ) and not fetch_completed:
                _LOGGER.warning(
                    "Deferring initial smart1 PV history import because "
                    "the history fetch did not complete",
                )
                self._last_result = "incomplete_fetch"
                return

            if destructive_rebuild_required:
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
                    replacement_dates=(
                        main_checked_days
                        if time_zone_rebuild_required and fetch_completed
                        else None
                    ),
                    records_tz=records_tz,
                )
            if (
                time_zone_audit_required
                and not forced_initial_rebuild
                and not destructive_rebuild_required
            ):
                # A large offset change can place the beginning of the new
                # source day before the end of the old source day in UTC. An
                # older installation without a fingerprint can also have a
                # sparse first in-window row. Start from the cumulative value
                # immediately before the earliest preserved/replaced bucket so
                # neither case double-counts or introduces a falling sum.
                baseline_sum = _baseline_before_first_pv_hour(
                    records,
                    hourly_energy,
                    baseline_sum,
                )
            if (
                alignment_rebuild_required
                or profile_schema_rebuild_required
                or time_zone_rebuild_required
            ) and not forced_initial_rebuild and (
                pending_clear_rebuild_baseline is None
                and not time_zone_rebuild_required
            ):
                preserved_statistics = (
                    _preserved_daily_pv_statistics_before(
                        records,
                        start_date,
                        records_tz,
                        target_tz=local_tz,
                        end_date=(today if time_zone_rebuild_required else None),
                    )
                    if self.pv_power_point is None
                    and not stored_profile_semantics
                    else _preserved_pv_statistics_before(
                        records,
                        start_date,
                        records_tz,
                        target_tz=local_tz,
                        end_date=(today if time_zone_rebuild_required else None),
                    )
                )
            else:
                preserved_statistics = []
            if time_zone_rebuild_required:
                baseline_sum = (
                    float(pending_clear_rebuild_baseline)
                    if pending_clear_rebuild_baseline is not None
                    else _baseline_before_all_records(records)
                )
            elif pending_clear_rebuild_baseline is not None:
                baseline_sum = float(pending_clear_rebuild_baseline)
            if preserved_statistics:
                # Schema and alignment repairs deliberately reimport valid
                # pre-window rows. Continue from their last cumulative value
                # so the replacement cannot introduce a falling sum at the
                # rebuild boundary. Forced isolation rebuilds never enter this
                # branch because their shared history is not attributable.
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
                or time_zone_rebuild_required
            ):
                statistics = sorted(
                    [*preserved_statistics, *statistics],
                    key=lambda item: item["start"],
                )
            clear_rebuild_baseline_sum: float | None = None
            if journaled_rebuild_required:
                # Journal the complete replacement batch, including any
                # pre-window rows that a destructive clear must recreate.
                # A later retry can then recover both their energy and the
                # cumulative baseline even if Recorder crashed after clear.
                migration_fallback_daily_energy = (
                    _positive_daily_energy_from_hourly(
                        [
                            (item["start"], float(item["state"]))
                            for item in statistics
                        ],
                        local_tz,
                    )
                )
                # The explicit value also makes an intentionally empty
                # replacement distinguishable from an incomplete journal.
                # Derive it from the first row of the *complete* replacement:
                # profile/alignment rebuilds may prepend preserved rows whose
                # cumulative sum predates the supported portal window.
                clear_rebuild_baseline_sum = (
                    _baseline_before_all_records(statistics)
                    if statistics
                    else float(baseline_sum)
                )
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
            # The recovery fetch is useful only to prove that the portal is
            # reachable.  Exact replay deliberately ignores its content to
            # finish the durable batch atomically, so it must not advance
            # coverage or empty-day evidence. Resetting target-schema progress
            # makes the next ordinary repair perform a full supported-window
            # catch-up without another destructive clear.
            commit_main_checked_through = (
                None
                if replaying_exact_replacement
                else main_checked_through
            )
            commit_empty_days = (
                set() if replaying_exact_replacement else empty_days
            )
            commit_nonempty_days = (
                set()
                if replaying_exact_replacement
                else persisted_nonempty_days
            )
            commit_retry_date = (
                None
                if replaying_exact_replacement
                else (retry_date if retry_completed else None)
            )
            time_zone_migration_generation: str | None = None
            if self.history_state:
                # A pending target represents the most conservative view of
                # Recorder after a crash: its queued write may already have
                # committed even though the fingerprint did not. Reuse that
                # generation when retrying the same target. For a new target,
                # persist a replacement journal entry before *any* Recorder
                # clear/import is queued.
                pending_time_zone_migration = (
                    self.history_state.pending_time_zone_migration(
                        self.statistic_id
                    )
                    if callable(
                        getattr(
                            self.history_state,
                            "pending_time_zone_migration",
                            None,
                        )
                    )
                    else pending_time_zone_migration
                )
                matching_pending_migration = bool(
                    pending_time_zone_migration is not None
                    and pending_time_zone_migration.target_time_zone
                    == source_time_zone
                )
                if (
                    journaled_rebuild_required
                    and (statistics or destructive_rebuild_required)
                ):
                    begin_migration = getattr(
                        self.history_state,
                        "async_begin_time_zone_migration",
                        None,
                    )
                    migration_kwargs: dict[str, Any] = {
                        "fallback_daily_energy": (
                            migration_fallback_daily_energy
                        )
                    }
                    if destructive_rebuild_required:
                        migration_kwargs["clear_rebuild_baseline_sum"] = (
                            clear_rebuild_baseline_sum
                        )
                    migration_source_time_zone = (
                        pending_time_zone_migration.source_time_zone
                        if matching_pending_migration
                        else recorded_time_zone
                    )
                    migration_kwargs["replacement_hourly_energy"] = [
                        {
                            "start": item["start"],
                            "state": item["state"],
                        }
                        for item in statistics
                    ]
                    try:
                        if callable(begin_migration):
                            time_zone_migration_generation = (
                                await begin_migration(
                                    self.statistic_id,
                                    migration_source_time_zone,
                                    source_time_zone,
                                    **migration_kwargs,
                                )
                            )
                        else:
                            time_zone_migration_generation = (
                                self.history_state.begin_time_zone_migration(
                                    self.statistic_id,
                                    migration_source_time_zone,
                                    source_time_zone,
                                    **migration_kwargs,
                                )
                            )
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning(
                            "Unable to persist smart1 PV time-zone "
                            "migration journal (%s)",
                            type(err).__name__,
                        )
                        self._last_result = "migration_journal_error"
                        return
                elif matching_pending_migration:
                    time_zone_migration_generation = (
                        pending_time_zone_migration.generation
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

                async def _async_commit_empty_rebuild() -> bool:
                    if (
                        persistence_generation
                        != self._persistence_generation
                        or not self.history_state
                    ):
                        return False
                    commit_kwargs = {
                        "has_data": False,
                        "expected_version": expected_marker_version,
                        "checked_through": (
                            commit_main_checked_through
                            if fetch_completed
                            else None
                        ),
                        "empty_days": commit_empty_days,
                        "nonempty_days": set(),
                        "oldest_supported": (
                            today - timedelta(days=HISTORY_DAYS - 1)
                        ),
                        "checked_on": None,
                        "source_time_zone": source_time_zone,
                        "time_zone_migration_generation": (
                            time_zone_migration_generation
                        ),
                        "reset_history_progress": (
                            replaying_exact_replacement
                        ),
                    }
                    async_commit = getattr(
                        self.history_state,
                        "async_commit_scan_if_unchanged",
                        None,
                    )
                    if callable(async_commit):
                        committed = await async_commit(
                            self.statistic_id,
                            self.schema_version,
                            **commit_kwargs,
                        )
                    else:
                        committed = (
                            self.history_state.commit_scan_if_unchanged(
                                self.statistic_id,
                                self.schema_version,
                                **commit_kwargs,
                            )
                        )
                    if committed:
                        self._migration_required = False
                    return committed

                def _mark_rebuild_complete() -> None:
                    # A successful clear is the complete Recorder operation
                    # only when there is no replacement batch.  Non-empty
                    # replacements are committed by the async caller below;
                    # non-empty replacements additionally require readback.
                    return

                def _finalize_late_rebuild() -> None:
                    if not statistics:
                        self._schedule_persistence_task(
                            _async_commit_empty_rebuild()
                        )
                        return
                    self._schedule_persistence_task(
                        self._async_finalize_late_clear(
                            recorder,
                            statistics,
                            expected_marker_version,
                            persistence_generation,
                            (
                                commit_main_checked_through
                                if repair
                                else None
                            ),
                            commit_empty_days,
                            commit_nonempty_days,
                            commit_retry_date,
                            today,
                            source_time_zone,
                            time_zone_migration_generation,
                            replaying_exact_replacement,
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
                if self.history_state and main_checked_through is not None:
                    if destructive_rebuild_required:
                        committed = await _async_commit_empty_rebuild()
                    else:
                        commit_kwargs = {
                            "has_data": bool(records),
                            "expected_version": expected_marker_version,
                            "checked_through": (
                                commit_main_checked_through
                                if repair
                                else None
                            ),
                            "empty_days": commit_empty_days,
                            "nonempty_days": commit_nonempty_days,
                            "oldest_supported": (
                                today - timedelta(days=HISTORY_DAYS - 1)
                            ),
                            "retried_day": (
                                commit_retry_date
                            ),
                            "checked_on": (
                                today
                                if commit_retry_date is not None
                                else None
                            ),
                            "source_time_zone": source_time_zone,
                            "time_zone_migration_generation": (
                                time_zone_migration_generation
                            ),
                            "reset_history_progress": (
                                replaying_exact_replacement
                            ),
                        }
                        async_commit = getattr(
                            self.history_state,
                            "async_commit_scan_if_unchanged",
                            None,
                        )
                        if callable(async_commit):
                            committed = await async_commit(
                                self.statistic_id,
                                self.schema_version,
                                **commit_kwargs,
                            )
                        else:
                            committed = (
                                self.history_state.commit_scan_if_unchanged(
                                    self.statistic_id,
                                    self.schema_version,
                                    **commit_kwargs,
                                )
                            )
                    if committed:
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
                    (
                        main_checked_through is not None
                        and not initial_backfill_complete
                    )
                    or self._data_presence() is not True
                    or alignment_rebuild_required
                    or profile_schema_rebuild_required
                    or time_zone_rebuild_required
                    or time_zone_audit_required
                    or (
                        main_checked_through is not None
                        and repair
                        and required_start is not None
                    )
                    or (retry_date is not None and retry_completed)
                )
            )
            if completion_needs_update:
                persistence_confirmed = (
                    await self._async_confirm_statistics_persistence(
                        recorder,
                        statistics,
                        expected_marker_version,
                        persistence_generation,
                        (
                            commit_main_checked_through
                            if repair
                            else None
                        ),
                        commit_empty_days,
                        commit_nonempty_days,
                        commit_retry_date,
                        today,
                        source_time_zone,
                        time_zone_migration_generation,
                        replaying_exact_replacement,
                    )
                )
                if not persistence_confirmed:
                    _LOGGER.info(
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
                                (
                                    commit_main_checked_through
                                    if repair
                                    else None
                                ),
                                commit_empty_days,
                                commit_nonempty_days,
                                commit_retry_date,
                                today,
                                source_time_zone,
                                time_zone_migration_generation,
                                replaying_exact_replacement,
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
