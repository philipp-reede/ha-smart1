"""Import integration-owned historical power statistics for Energy Dashboard."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import logging
import math
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
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import PowerConverter

from .api import Smart1Api, Smart1ApiError, describe_api_error
from .const import DOMAIN
from .history_state import (
    POWER_HISTORY_SCHEMA_VERSION,
    Smart1HistoryState,
    scoped_statistic_id,
)
from .point import Smart1Point
from .power_integration import (
    MAX_SAMPLE_GAP,
    integrate_power_samples,
    normalize_power_rows,
)
from .recorder_helpers import (
    async_wait_for_recorder_commit,
    async_wait_for_statistics_readback,
    statistics_are_persisted,
    validate_power_statistics_imports,
)

_LOGGER = logging.getLogger(__name__)

POWER_HISTORY_DAYS = 365
POWER_HISTORY_REFRESH_DAYS = 3
POWER_FETCH_ATTEMPTS = 3
FULL_HOUR_SECONDS = 60 * 60
MAX_HOURLY_RECORDS_PER_DAY = 25
PERSISTENCE_RETRY_INTERVAL = timedelta(hours=6)

PV_POWER_KEY = "pv"
GRID_POWER_KEY = "grid"
BATTERY_POWER_KEY = "battery"

_CHANNEL_NAMES = {
    PV_POWER_KEY: "smart1 EMS PV power",
    GRID_POWER_KEY: "smart1 EMS grid power",
    BATTERY_POWER_KEY: "smart1 EMS battery power",
}

StatisticsWriter = Callable[
    [StatisticMetaData, list[StatisticData]],
    None,
]
PersistenceChecker = Callable[
    [str, list[StatisticData]],
    Awaitable[bool],
]
StatisticsReader = Callable[
    [str, int],
    Awaitable[list[Mapping[str, Any]]],
]
ActiveCheck = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class PowerHistoryChannel:
    """One external signed or unsigned power statistic."""

    key: str
    name: str
    statistic_id: str
    terms: tuple[tuple[Smart1Point, float], ...]


@dataclass(frozen=True, slots=True)
class PowerHistoryFetchResult:
    """Result of one boundary-aware detailed-data scan."""

    hourly_means: Mapping[str, tuple[tuple[datetime, float], ...]]
    completed: bool
    checked_through: date | None
    requested_days: int
    emitted_hours: int
    replaceable_hours: tuple[datetime, ...] = ()
    incomplete_days: Mapping[str, frozenset[date]] | None = None
    complete_days: Mapping[str, frozenset[date]] | None = None


@dataclass(frozen=True, slots=True)
class _PendingPersistence:
    """One fetched batch awaiting Recorder readback confirmation."""

    last_attempt_at: datetime
    batches: Mapping[
        str,
        tuple[
            PowerHistoryChannel,
            StatisticMetaData,
            list[StatisticData],
        ],
    ]
    expected_versions: Mapping[str, int]
    existing_has_data: Mapping[str, bool]
    result: PowerHistoryFetchResult
    retry_result: PowerHistoryFetchResult | None
    retry_date: date | None
    retry_dates_by_key: Mapping[str, date]
    retry_completed: bool
    today: date
    oldest_supported: date
    source_time_zone: str


def power_statistic_id(
    channel_key: str,
    source_terms: Iterable[tuple[str, str]],
    statistics_namespace: str = "",
    source_time_zone: str = "UTC",
) -> str:
    """Return an external ID scoped to sources and timestamp semantics.

    Portal timestamps without an explicit offset are interpreted in Home
    Assistant's configured time zone.  Including that zone in the identity
    makes a later zone change non-destructive: a new statistic is backfilled
    while the old, differently mapped series remains untouched.
    """
    canonical_sources = "\0".join(
        f"{role_key}:{point_id}"
        for role_key, point_id in sorted(source_terms)
    )
    identity = f"{source_time_zone}\0{canonical_sources}"
    source_hash = sha256(identity.encode()).hexdigest()[:8]
    base_id = f"{DOMAIN}:{channel_key}_power_{source_hash}"
    return scoped_statistic_id(base_id, statistics_namespace)


def build_power_history_channels(
    pv_power_point: Smart1Point | None,
    role_points: Mapping[str, Smart1Point],
    *,
    statistics_namespace: str = "",
    source_time_zone: str = "UTC",
) -> tuple[PowerHistoryChannel, ...]:
    """Build only channels whose complete source set is available.

    The sign convention matches the Energy Dashboard flow view: PV is
    positive, grid import is positive and export negative, while battery
    discharge is positive and charge negative.
    """
    channels: list[PowerHistoryChannel] = []
    if pv_power_point is not None:
        channels.append(
            PowerHistoryChannel(
                key=PV_POWER_KEY,
                name=(
                    f"{_CHANNEL_NAMES[PV_POWER_KEY]} "
                    f"({source_time_zone})"
                ),
                statistic_id=power_statistic_id(
                    PV_POWER_KEY,
                    ((PV_POWER_KEY, pv_power_point.id),),
                    statistics_namespace,
                    source_time_zone,
                ),
                terms=((pv_power_point, 1.0),),
            )
        )

    grid_import = role_points.get("grid_import")
    grid_export = role_points.get("grid_export")
    if grid_import is not None and grid_export is not None:
        channels.append(
            PowerHistoryChannel(
                key=GRID_POWER_KEY,
                name=(
                    f"{_CHANNEL_NAMES[GRID_POWER_KEY]} "
                    f"({source_time_zone})"
                ),
                statistic_id=power_statistic_id(
                    GRID_POWER_KEY,
                    (
                        ("grid_import", grid_import.id),
                        ("grid_export", grid_export.id),
                    ),
                    statistics_namespace,
                    source_time_zone,
                ),
                terms=((grid_import, 1.0), (grid_export, -1.0)),
            )
        )

    battery_discharge = role_points.get("battery_discharge")
    battery_charge = role_points.get("battery_charge")
    if battery_discharge is not None and battery_charge is not None:
        channels.append(
            PowerHistoryChannel(
                key=BATTERY_POWER_KEY,
                name=(
                    f"{_CHANNEL_NAMES[BATTERY_POWER_KEY]} "
                    f"({source_time_zone})"
                ),
                statistic_id=power_statistic_id(
                    BATTERY_POWER_KEY,
                    (
                        ("battery_charge", battery_charge.id),
                        ("battery_discharge", battery_discharge.id),
                    ),
                    statistics_namespace,
                    source_time_zone,
                ),
                terms=(
                    (battery_discharge, 1.0),
                    (battery_charge, -1.0),
                ),
            )
        )
    return tuple(channels)


def _hourly_covered_seconds(
    samples: tuple[tuple[datetime, float], ...],
) -> dict[datetime, float]:
    """Return integrated seconds per UTC hour without bridging gaps."""
    coverage: defaultdict[datetime, float] = defaultdict(float)
    for (previous_time, _previous_value), (
        current_time,
        _current_value,
    ) in zip(samples, samples[1:], strict=False):
        previous_time = previous_time.astimezone(timezone.utc)
        current_time = current_time.astimezone(timezone.utc)
        interval = current_time - previous_time
        if interval <= timedelta() or interval > MAX_SAMPLE_GAP:
            continue

        segment_start = previous_time
        while segment_start < current_time:
            hour_start = segment_start.replace(
                minute=0,
                second=0,
                microsecond=0,
            )
            segment_end = min(
                current_time,
                hour_start + timedelta(hours=1),
            )
            coverage[hour_start] += (
                segment_end - segment_start
            ).total_seconds()
            segment_start = segment_end
    return dict(coverage)


def complete_hourly_power_means(
    samples: tuple[tuple[datetime, float], ...],
    *,
    completed_before: datetime,
) -> dict[datetime, float]:
    """Return W means only for fully covered, completed UTC hours."""
    completed_before = completed_before.astimezone(timezone.utc).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    integration = integrate_power_samples(samples)
    coverage = _hourly_covered_seconds(samples)
    return {
        hour_start: energy_kwh * 1000.0
        for hour_start, energy_kwh in integration.hourly_energy_kwh
        if hour_start < completed_before
        and math.isclose(
            coverage.get(hour_start, 0.0),
            FULL_HOUR_SECONDS,
            rel_tol=0.0,
            abs_tol=1e-6,
        )
    }


def build_hourly_power_statistics(
    hourly_means: Iterable[tuple[datetime, float]],
) -> list[StatisticData]:
    """Build Recorder rows for external hourly power means."""
    return [
        StatisticData(start=start, mean=mean_w)
        for start, mean_w in sorted(hourly_means)
    ]


class Smart1PowerHistoryImporter:
    """Import external PV, signed grid and signed battery power means."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: Smart1Api,
        *,
        pv_power_point: Smart1Point | None,
        role_points: Mapping[str, Smart1Point],
        statistics_namespace: str = "",
        history_state: Smart1HistoryState | None = None,
        statistics_writer: StatisticsWriter | None = None,
        persistence_checker: PersistenceChecker | None = None,
        statistics_reader: StatisticsReader | None = None,
        active_check: ActiveCheck | None = None,
    ) -> None:
        self.hass = hass
        self.api = api
        self.history_state = history_state
        self._configured_source_time_zone = getattr(
            getattr(hass, "config", None),
            "time_zone",
            "UTC",
        )
        self.channels = build_power_history_channels(
            pv_power_point,
            role_points,
            statistics_namespace=statistics_namespace,
            source_time_zone=self._configured_source_time_zone,
        )
        self._statistics_writer = statistics_writer
        self._persistence_checker = persistence_checker
        self._statistics_reader = statistics_reader
        self._active_check = active_check or (lambda: True)
        self._active = True
        self._lock = asyncio.Lock()
        self._pending_refresh_days = 0
        self._pending_repair = False
        self._pending_persistence: _PendingPersistence | None = None
        self._last_result = "not_started"
        self._last_mode: str | None = None
        self._last_fetch_completed: bool | None = None
        self._last_requested_days = 0
        self._last_emitted_hours = 0

    def _is_active(self) -> bool:
        """Return whether this importer still owns the config-entry runtime."""
        return self._active and bool(self._active_check())

    def deactivate(self) -> None:
        """Fence an old config-entry generation from future writes."""
        self._active = False
        self._pending_refresh_days = 0
        self._pending_repair = False
        self._pending_persistence = None

    @property
    def statistic_ids(self) -> Mapping[str, str]:
        """Return channel keys mapped to integration-owned statistic IDs."""
        return {
            channel.key: channel.statistic_id for channel in self.channels
        }

    @property
    def diagnostic_status(self) -> dict[str, object]:
        """Return value- and identifier-free power-history diagnostics."""
        complete_channels = sum(
            1
            for channel in self.channels
            if self.history_state
            and self.history_state.is_complete(
                channel.statistic_id,
                POWER_HISTORY_SCHEMA_VERSION,
            )
        )
        return {
            "type": "power_history",
            "last_result": self._last_result,
            "last_mode": self._last_mode,
            "last_fetch_completed": self._last_fetch_completed,
            "configured_channels": len(self.channels),
            "complete_channels": complete_channels,
            "last_requested_days": self._last_requested_days,
            "last_emitted_hours": self._last_emitted_hours,
            "persistence_confirmation_pending": (
                self._pending_persistence is not None
            ),
        }

    def _needs_full_scan(self) -> bool:
        if not self.history_state:
            return True
        return any(
            not self.history_state.is_complete(
                channel.statistic_id,
                POWER_HISTORY_SCHEMA_VERSION,
            )
            for channel in self.channels
        )

    @staticmethod
    def _filter_samples_for_source_date(
        samples: tuple[tuple[datetime, float], ...],
        target_date: date,
        local_tz: ZoneInfo,
    ) -> tuple[tuple[datetime, float], ...]:
        return tuple(
            sample
            for sample in samples
            if sample[0].astimezone(local_tz).date() == target_date
        )

    async def _fetch_hourly_means(
        self,
        start_date: date,
        end_date: date,
        local_tz: ZoneInfo,
        *,
        completed_before: datetime,
        include_following_boundary: bool = False,
    ) -> PowerHistoryFetchResult:
        """Fetch one combined response per day and retain boundary intervals."""
        points_by_id = {
            point.id: point
            for channel in self.channels
            for point, _factor in channel.terms
        }
        linear_ids = list(points_by_id)
        hourly_energy: dict[str, defaultdict[datetime, float]] = {
            point_id: defaultdict(float) for point_id in linear_ids
        }
        hourly_coverage: dict[str, defaultdict[datetime, float]] = {
            point_id: defaultdict(float) for point_id in linear_ids
        }
        carry_samples: dict[str, tuple[datetime, float]] = {}
        checked_through: date | None = None
        requested_days = 0
        completed = True

        # The preceding source day is required in fractional-offset zones and
        # makes resumed scans boundary-safe in every zone. Its completed rows
        # are filtered out below unless they overlap the requested UTC window.
        target_date = start_date - timedelta(days=1)
        request_end_date = end_date + (
            timedelta(days=1) if include_following_boundary else timedelta()
        )
        while target_date <= request_end_date:
            if not self._is_active():
                completed = False
                break
            for attempt in range(POWER_FETCH_ATTEMPTS):
                try:
                    rows = await self.api.get_linear_detailed_rows(
                        linear_ids,
                        target_date=target_date,
                        missing_ok=True,
                    )
                    requested_days += 1
                    break
                except (ClientError, Smart1ApiError, TimeoutError) as err:
                    if attempt == POWER_FETCH_ATTEMPTS - 1:
                        _LOGGER.warning(
                            "Unable to import smart1 power history from %s "
                            "after %d attempts: %s",
                            target_date,
                            POWER_FETCH_ATTEMPTS,
                            describe_api_error(err),
                        )
                        completed = False
                        rows = []
                        break
                    await asyncio.sleep(2**attempt)
            if not completed:
                break

            for point_id in linear_ids:
                samples = self._filter_samples_for_source_date(
                    normalize_power_rows(rows, point_id, local_tz),
                    target_date,
                    local_tz,
                )
                if not samples:
                    continue
                sequence = samples
                if carry := carry_samples.get(point_id):
                    sequence = (carry, *samples)

                integration = integrate_power_samples(sequence)
                for hour_start, energy_kwh in integration.hourly_energy_kwh:
                    hourly_energy[point_id][hour_start] += energy_kwh
                for hour_start, covered_seconds in (
                    _hourly_covered_seconds(sequence).items()
                ):
                    hourly_coverage[point_id][hour_start] += covered_seconds
                carry_samples[point_id] = samples[-1]

            if start_date <= target_date <= end_date:
                checked_through = target_date
            target_date += timedelta(days=1)

        window_start = datetime.combine(
            start_date,
            time.min,
            local_tz,
        ).astimezone(timezone.utc)
        open_source_date = completed_before.astimezone(local_tz).date()
        completed_before = completed_before.astimezone(timezone.utc).replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        if checked_through is None:
            replaceable_hours: tuple[datetime, ...] = ()
        else:
            window_end = datetime.combine(
                checked_through + timedelta(days=1),
                time.min,
                local_tz,
            ).astimezone(timezone.utc)
            # The next source day supplies the sample on local midnight. If
            # that day could not be fetched, leave the final UTC hour outside
            # the authoritative replacement range so a resume can complete it.
            replacement_end = min(
                completed_before,
                (
                    window_end
                    if include_following_boundary
                    else window_end - MAX_SAMPLE_GAP
                ),
            )
            candidate = (window_start - timedelta(hours=1)).replace(
                minute=0,
                second=0,
                microsecond=0,
            )
            hours: list[datetime] = []
            while candidate + timedelta(hours=1) <= replacement_end:
                if candidate + timedelta(hours=1) >= window_start:
                    hours.append(candidate)
                candidate += timedelta(hours=1)
            replaceable_hours = tuple(hours)
        replaceable_hour_set = set(replaceable_hours)

        means_by_point: dict[str, dict[datetime, float]] = {}
        for point_id in linear_ids:
            means_by_point[point_id] = {
                hour_start: energy_kwh * 1000.0
                for hour_start, energy_kwh in hourly_energy[point_id].items()
                if hour_start in replaceable_hour_set
                and math.isclose(
                    hourly_coverage[point_id].get(hour_start, 0.0),
                    FULL_HOUR_SECONDS,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            }

        channel_means: dict[str, tuple[tuple[datetime, float], ...]] = {}
        incomplete_days: dict[str, frozenset[date]] = {}
        complete_days: dict[str, frozenset[date]] = {}
        emitted_hours = 0
        scanned_dates = (
            {
                start_date + timedelta(days=offset)
                for offset in range((checked_through - start_date).days + 1)
            }
            if checked_through is not None
            and checked_through >= start_date
            else set()
        )
        expected_by_date: defaultdict[date, set[datetime]] = defaultdict(set)
        for hour_start in replaceable_hours:
            source_date = (
                hour_start + timedelta(minutes=30)
            ).astimezone(local_tz).date()
            if source_date in scanned_dates:
                expected_by_date[source_date].add(hour_start)
        for channel in self.channels:
            common_hours = set.intersection(
                *(
                    set(means_by_point[point.id])
                    for point, _factor in channel.terms
                )
            )
            means = tuple(
                (
                    hour_start,
                    sum(
                        means_by_point[point.id][hour_start] * factor
                        for point, factor in channel.terms
                    ),
                )
                for hour_start in sorted(common_hours)
            )
            channel_means[channel.key] = means
            emitted_hours += len(means)
            observed_hours = {hour_start for hour_start, _mean in means}
            channel_complete_days = {
                source_date
                for source_date, expected_hours in expected_by_date.items()
                if expected_hours and expected_hours <= observed_hours
            }
            # The local source day containing ``completed_before`` is still
            # open.  Even when every hour expected so far is present (or no
            # completed hour exists yet), completing its durable day marker
            # would prevent a later run from recovering the remaining hours.
            channel_incomplete_days = (
                set(expected_by_date) - channel_complete_days
            )
            if open_source_date in scanned_dates:
                channel_complete_days.discard(open_source_date)
                channel_incomplete_days.add(open_source_date)
            # A failed next source-day request leaves the closing hour of the
            # last successfully scanned day outside ``replaceable_hours``.
            # Keep that prefix-end day retryable even though every earlier
            # expected hour was present.
            if not completed and checked_through is not None:
                channel_complete_days.discard(checked_through)
                channel_incomplete_days.add(checked_through)
            complete_days[channel.key] = frozenset(channel_complete_days)
            incomplete_days[channel.key] = frozenset(
                channel_incomplete_days
            )

        return PowerHistoryFetchResult(
            hourly_means=channel_means,
            completed=completed,
            checked_through=checked_through,
            requested_days=requested_days,
            emitted_hours=emitted_hours,
            replaceable_hours=replaceable_hours,
            incomplete_days=incomplete_days,
            complete_days=complete_days,
        )

    def _metadata(self, channel: PowerHistoryChannel) -> StatisticMetaData:
        return StatisticMetaData(
            mean_type=StatisticMeanType.ARITHMETIC,
            has_sum=False,
            name=channel.name,
            source=DOMAIN,
            statistic_id=channel.statistic_id,
            unit_class=PowerConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfPower.WATT,
        )

    def _write_statistics(
        self,
        metadata: StatisticMetaData,
        statistics: list[StatisticData],
    ) -> None:
        if self._statistics_writer is not None:
            self._statistics_writer(metadata, statistics)
            return
        async_add_external_statistics(
            hass=self.hass,
            metadata=metadata,
            statistics=statistics,
        )

    async def _existing_statistics(
        self,
        statistic_id: str,
        record_count: int,
    ) -> list[Mapping[str, Any]]:
        if self._statistics_reader is not None:
            return await self._statistics_reader(statistic_id, record_count)
        result = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            record_count,
            statistic_id,
            True,
            {"mean"},
        )
        return result.get(statistic_id, [])

    @staticmethod
    def _record_start(record: Mapping[str, Any]) -> datetime | None:
        """Return one Recorder timestamp normalized to UTC."""
        raw_start = record.get("start")
        if isinstance(raw_start, datetime):
            if raw_start.tzinfo is None or raw_start.utcoffset() is None:
                return None
            return raw_start.astimezone(timezone.utc)
        try:
            return datetime.fromtimestamp(float(raw_start), timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    async def _statistics_persisted(
        self,
        statistic_id: str,
        statistics: list[StatisticData],
    ) -> bool:
        if not statistics:
            return True
        if self._persistence_checker is not None:
            return await self._persistence_checker(statistic_id, statistics)
        return await async_wait_for_statistics_readback(
            statistics,
            lambda: self._existing_statistics(
                statistic_id,
                POWER_HISTORY_DAYS * MAX_HOURLY_RECORDS_PER_DAY
                + len(statistics)
                + 1,
            ),
            matcher=statistics_are_persisted,
        )

    async def _commit_marker(
        self,
        channel: PowerHistoryChannel,
        *,
        has_data: bool,
        expected_version: int,
        checked_through: date,
        oldest_supported: date,
        source_time_zone: str,
        incomplete_days: set[date],
        complete_days: set[date],
        retried_day: date | None = None,
        checked_on: date | None = None,
    ) -> bool:
        if not self.history_state:
            return True
        commit_kwargs = {
            "has_data": has_data,
            "expected_version": expected_version,
            "checked_through": checked_through,
            "empty_days": incomplete_days,
            "nonempty_days": complete_days,
            "oldest_supported": oldest_supported,
            "source_time_zone": source_time_zone,
            "retried_day": retried_day,
            "checked_on": checked_on,
        }
        async_commit = getattr(
            self.history_state,
            "async_commit_scan_if_unchanged",
            None,
        )
        if callable(async_commit):
            return await async_commit(
                channel.statistic_id,
                POWER_HISTORY_SCHEMA_VERSION,
                **commit_kwargs,
            )
        commit = getattr(self.history_state, "commit_scan_if_unchanged", None)
        if callable(commit):
            return commit(
                channel.statistic_id,
                POWER_HISTORY_SCHEMA_VERSION,
                **commit_kwargs,
            )
        self.history_state.mark_complete(
            channel.statistic_id,
            POWER_HISTORY_SCHEMA_VERSION,
            has_data=has_data,
            checked_through=checked_through,
        )
        return True

    def _incomplete_progress(
        self,
        channel: PowerHistoryChannel,
    ) -> Any | None:
        if not self.history_state:
            return None
        getter = getattr(
            self.history_state,
            "incomplete_scan_progress",
            None,
        )
        if not callable(getter):
            return None
        return getter(channel.statistic_id, POWER_HISTORY_SCHEMA_VERSION)

    def _checked_through(
        self,
        channel: PowerHistoryChannel,
    ) -> date | None:
        """Return durable coverage for one completed power statistic."""
        if not self.history_state:
            return None
        getter = getattr(self.history_state, "checked_through", None)
        if not callable(getter):
            return None
        return getter(channel.statistic_id, POWER_HISTORY_SCHEMA_VERSION)

    def _resume_start(
        self,
        oldest_supported: date,
        source_time_zone: str,
        today: date,
    ) -> tuple[date, bool]:
        """Resume only when every channel has the same safe scan prefix."""
        progress = [
            self._incomplete_progress(channel) for channel in self.channels
        ]
        if not progress:
            return oldest_supported, False
        if any(
            item is not None
            and (
                getattr(item, "checked_through") >= today
                or getattr(item, "oldest_supported") > oldest_supported
            )
            for item in progress
        ):
            # A clock rollback or newer restored backup can leave the next
            # resume date beyond the request window or move its beginning
            # before the saved scan prefix. Rebuild the supported window and
            # discard every channel's shared prefix so it cannot be merged
            # back into the replacement progress.
            return oldest_supported, True
        if any(item is None for item in progress):
            return oldest_supported, False
        if any(
            getattr(item, "source_time_zone", None) != source_time_zone
            for item in progress
        ):
            return oldest_supported, False
        checked_through = min(
            getattr(item, "checked_through") for item in progress
        )
        return (
            max(oldest_supported, checked_through + timedelta(days=1)),
            False,
        )

    def _restart_scan_state(self) -> bool:
        """Forget invalid scan state without removing Recorder rows."""
        if not self.history_state:
            return True
        forgetter = getattr(
            self.history_state,
            "forget_statistics",
            None,
        )
        if not callable(forgetter):
            return False
        forgetter({channel.statistic_id for channel in self.channels})
        return (
            self._is_active()
            and self._needs_full_scan()
            and all(
                self._incomplete_progress(channel) is None
                for channel in self.channels
            )
        )

    def _next_retry_dates(
        self,
        *,
        oldest_supported: date,
        before: date,
        today: date,
    ) -> dict[str, date]:
        if not self.history_state:
            return {}
        getter = getattr(self.history_state, "next_empty_retry_date", None)
        if not callable(getter):
            return {}
        return {
            channel.key: retry_date
            for channel in self.channels
            if (
                retry_date := getter(
                    channel.statistic_id,
                    POWER_HISTORY_SCHEMA_VERSION,
                    oldest_supported=oldest_supported,
                    before=before,
                    today=today,
                )
            )
            is not None
        }

    async def _record_incomplete_progress(
        self,
        batches: Mapping[
            str,
            tuple[
                PowerHistoryChannel,
                StatisticMetaData,
                list[StatisticData],
            ],
        ],
        *,
        expected_versions: Mapping[str, int],
        checked_through: date,
        oldest_supported: date,
        source_time_zone: str,
        existing_has_data_by_key: Mapping[str, bool] | None = None,
        incomplete_days_by_key: Mapping[str, frozenset[date]] | None = None,
        complete_days_by_key: Mapping[str, frozenset[date]] | None = None,
    ) -> bool:
        if not self.history_state:
            return True
        recorder = getattr(
            self.history_state,
            "record_incomplete_scan_if_unchanged",
            None,
        )
        if not callable(recorder):
            return False
        for channel, _metadata, statistics in batches.values():
            if not self._is_active():
                return False
            prior = self._incomplete_progress(channel)
            previous_has_data = (
                self.history_state.data_presence(
                    channel.statistic_id,
                    POWER_HISTORY_SCHEMA_VERSION,
                )
                is True
            )
            if not recorder(
                channel.statistic_id,
                POWER_HISTORY_SCHEMA_VERSION,
                expected_version=expected_versions[channel.statistic_id],
                checked_through=checked_through,
                has_data=(
                    bool(statistics)
                    or bool(
                        (existing_has_data_by_key or {}).get(
                            channel.key,
                            False,
                        )
                    )
                    or previous_has_data
                    or bool(getattr(prior, "has_data", False))
                ),
                oldest_supported=oldest_supported,
                source_time_zone=source_time_zone,
                incomplete_days=set(
                    (incomplete_days_by_key or {}).get(channel.key, ())
                ),
                complete_days=set(
                    (complete_days_by_key or {}).get(channel.key, ())
                ),
            ):
                return False
        return True

    def _expected_versions_unchanged(
        self,
        expected_versions: Mapping[str, int],
    ) -> bool:
        """Return whether a pending batch still owns every state generation."""
        if not self.history_state:
            return True
        return all(
            self.history_state.current_schema_version(statistic_id)
            == expected_version
            for statistic_id, expected_version in expected_versions.items()
        )

    async def _read_existing_by_key(
        self,
        record_count: int,
    ) -> dict[str, list[Mapping[str, Any]]]:
        rows = await asyncio.gather(
            *(
                self._existing_statistics(
                    channel.statistic_id,
                    record_count,
                )
                for channel in self.channels
            )
        )
        return {
            channel.key: records
            for channel, records in zip(self.channels, rows, strict=True)
        }

    @staticmethod
    def _merged_means(
        main: PowerHistoryFetchResult,
        retry: PowerHistoryFetchResult | None,
        channel_key: str,
    ) -> dict[datetime, float]:
        means = dict(main.hourly_means.get(channel_key, ()))
        if retry is not None:
            means.update(retry.hourly_means.get(channel_key, ()))
        return means

    async def _confirm_batches(
        self,
        batches: Mapping[
            str,
            tuple[
                PowerHistoryChannel,
                StatisticMetaData,
                list[StatisticData],
            ],
        ],
    ) -> dict[str, bool]:
        """Read back every non-empty batch without contacting the portal."""
        return dict(
            await asyncio.gather(
                *(
                    self._confirm_channel(channel, statistics)
                    for channel, _metadata, statistics in batches.values()
                )
            )
        )

    async def _finalize_persisted_batch(
        self,
        pending: _PendingPersistence,
    ) -> None:
        """Commit scan state after Recorder has durably accepted a batch."""
        if not self._is_active():
            self._last_result = "inactive"
            return
        if not self._expected_versions_unchanged(
            pending.expected_versions
        ):
            self._last_result = "state_changed"
            return

        result = pending.result
        checked_through = result.checked_through
        if checked_through is None:
            self._last_result = "fetch_incomplete"
            return
        if not result.completed or checked_through != pending.today:
            progress_recorded = await self._record_incomplete_progress(
                pending.batches,
                expected_versions=pending.expected_versions,
                checked_through=checked_through,
                oldest_supported=pending.oldest_supported,
                source_time_zone=pending.source_time_zone,
                existing_has_data_by_key=pending.existing_has_data,
                incomplete_days_by_key=result.incomplete_days,
                complete_days_by_key=result.complete_days,
            )
            self._last_result = (
                "fetch_incomplete" if progress_recorded else "state_changed"
            )
            return

        for channel, _metadata, statistics in pending.batches.values():
            if not self._is_active():
                self._last_result = "inactive"
                return
            if (
                self.history_state
                and self.history_state.current_schema_version(
                    channel.statistic_id
                )
                != pending.expected_versions[channel.statistic_id]
            ):
                self._last_result = "state_changed"
                return
            previous_has_data = (
                self.history_state.data_presence(
                    channel.statistic_id,
                    POWER_HISTORY_SCHEMA_VERSION,
                )
                if self.history_state
                else None
            )
            progress = self._incomplete_progress(channel)
            has_data = (
                bool(statistics)
                or pending.existing_has_data.get(channel.key, False)
                or previous_has_data is True
                or bool(getattr(progress, "has_data", False))
            )
            incomplete_days = set(
                (result.incomplete_days or {}).get(channel.key, ())
            )
            complete_days = set(
                (result.complete_days or {}).get(channel.key, ())
            )
            if pending.retry_result is not None:
                incomplete_days.update(
                    (pending.retry_result.incomplete_days or {}).get(
                        channel.key,
                        (),
                    )
                )
                complete_days.update(
                    (pending.retry_result.complete_days or {}).get(
                        channel.key,
                        (),
                    )
                )
            incomplete_days.difference_update(complete_days)
            committed = await self._commit_marker(
                channel,
                has_data=has_data,
                expected_version=pending.expected_versions[
                    channel.statistic_id
                ],
                checked_through=pending.today,
                oldest_supported=pending.oldest_supported,
                source_time_zone=pending.source_time_zone,
                incomplete_days=incomplete_days,
                complete_days=complete_days,
                retried_day=(
                    pending.retry_date
                    if pending.retry_completed
                    and pending.retry_dates_by_key.get(channel.key)
                    == pending.retry_date
                    else None
                ),
                checked_on=(
                    pending.today
                    if pending.retry_completed
                    and pending.retry_dates_by_key.get(channel.key)
                    == pending.retry_date
                    else None
                ),
            )
            if not committed:
                self._last_result = "state_changed"
                return

        self._last_result = "completed"

    async def _resume_pending_persistence(
        self,
        *,
        now_utc: datetime,
        repair: bool,
    ) -> bool:
        """Retry a retained Recorder batch without contacting the portal."""
        pending = self._pending_persistence
        if pending is None:
            return False
        if not self._is_active():
            self._last_result = "inactive"
            return True
        if not self._expected_versions_unchanged(
            pending.expected_versions
        ):
            self._pending_persistence = None
            self._last_result = "state_changed"
            return True

        if not repair:
            self._last_result = "persistence_pending"
            return True

        pending_age = now_utc - pending.last_attempt_at
        if pending_age < timedelta():
            # A wall-clock rollback makes the retained retry timestamp
            # meaningless. Retry the already fetched batch immediately so a
            # future timestamp cannot block Recorder recovery for days.
            pending_age = PERSISTENCE_RETRY_INTERVAL
        if pending_age < PERSISTENCE_RETRY_INTERVAL:
            self._last_result = "persistence_pending"
            return True

        # A queued external-statistics job may have failed or been lost while
        # Recorder was unavailable. Re-enqueue the already fetched and
        # validated upserts instead of repeating up to 366 portal requests.
        pending = replace(pending, last_attempt_at=now_utc)
        self._pending_persistence = pending
        for _channel, metadata, statistics in pending.batches.values():
            if statistics:
                self._write_statistics(metadata, statistics)
        if any(
            statistics
            for _channel, _metadata, statistics in pending.batches.values()
        ) and self._persistence_checker is None:
            await async_wait_for_recorder_commit(get_instance(self.hass))

        persisted_by_key = await self._confirm_batches(pending.batches)
        if not self._is_active():
            self._last_result = "inactive"
            return True
        if not self._expected_versions_unchanged(
            pending.expected_versions
        ):
            self._pending_persistence = None
            self._last_result = "state_changed"
            return True
        if all(persisted_by_key.values()):
            await self._finalize_persisted_batch(pending)
            if self._pending_persistence is pending:
                self._pending_persistence = None
            return True

        self._last_result = "persistence_pending"
        return True

    async def async_import(
        self,
        refresh_days: int = POWER_HISTORY_REFRESH_DAYS,
        *,
        repair: bool = True,
        now: datetime | None = None,
    ) -> None:
        """Import 365 days initially and refresh recent completed hours.

        A concurrent wider repair is coalesced instead of being dropped behind
        the hourly current-day refresh.
        """
        if refresh_days < 1:
            raise ValueError("refresh_days must be positive")
        if not self.channels or not self._is_active():
            return
        if self._lock.locked():
            self._pending_refresh_days = max(
                self._pending_refresh_days,
                refresh_days,
            )
            self._pending_repair = self._pending_repair or repair
            return

        refresh_days = max(refresh_days, self._pending_refresh_days)
        repair = repair or self._pending_repair

        async with self._lock:
            requested_days = refresh_days
            requested_repair = repair
            for _coalesced_run in range(2):
                self._pending_refresh_days = 0
                self._pending_repair = False
                await self._async_import_locked(
                    requested_days,
                    repair=requested_repair,
                    now=now,
                )
                if not self._is_active():
                    return
                if self._last_result == "persistence_pending":
                    # A concurrent timer must not immediately repeat either
                    # readback or a full portal scan after the same timeout.
                    self._pending_refresh_days = 0
                    self._pending_repair = False
                    return
                pending_days = self._pending_refresh_days
                pending_repair = self._pending_repair
                if pending_days <= requested_days and (
                    not pending_repair or requested_repair
                ):
                    return
                requested_days = max(requested_days, pending_days)
                requested_repair = requested_repair or pending_repair

    async def _async_import_locked(
        self,
        refresh_days: int,
        *,
        repair: bool,
        now: datetime | None,
    ) -> None:
        """Run one import while the per-importer lock is held."""
        self._last_result = "running"
        self._last_fetch_completed = None
        now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        source_time_zone = getattr(
            getattr(self.hass, "config", None),
            "time_zone",
            "UTC",
        )
        if source_time_zone != self._configured_source_time_zone:
            # Statistic identity contains the source zone. A reload creates a
            # new, non-destructive series instead of remapping existing rows.
            self._last_result = "time_zone_reload_required"
            return
        try:
            local_tz = ZoneInfo(source_time_zone)
        except ZoneInfoNotFoundError:
            _LOGGER.warning(
                "Unable to import smart1 power history: invalid time zone"
            )
            self._last_result = "invalid_time_zone"
            return

        if await self._resume_pending_persistence(
            now_utc=now_utc,
            repair=repair,
        ):
            return

        today = now_utc.astimezone(local_tz).date()
        oldest_supported = today - timedelta(days=POWER_HISTORY_DAYS - 1)
        full_scan = self._needs_full_scan() or any(
            self._incomplete_progress(channel) is not None
            for channel in self.channels
        )
        recent_start = max(
            oldest_supported,
            today - timedelta(days=refresh_days - 1),
        )
        coverage = [
            self._checked_through(channel) for channel in self.channels
        ]
        if any(
            checked is not None and checked > today
            for checked in coverage
        ):
            # Completed state from a future clock must not make the newly
            # supported beginning of the rolling window look covered. Reset
            # every channel together before rebuilding that window.
            full_scan = True
            start_date = oldest_supported
            self._last_mode = "repair"
            if not self._restart_scan_state():
                self._last_result = (
                    "inactive" if not self._is_active() else "state_changed"
                )
                return
        elif full_scan:
            start_date, reset_incomplete_progress = self._resume_start(
                oldest_supported,
                source_time_zone,
                today,
            )
            self._last_mode = "initial"
            if (
                reset_incomplete_progress
                and not self._restart_scan_state()
            ):
                self._last_result = (
                    "inactive" if not self._is_active() else "state_changed"
                )
                return
        else:
            if repair and any(checked is None for checked in coverage):
                # A schema marker without coverage cannot prove which outage
                # days were already queried. Rebuild the supported window.
                full_scan = True
                start_date = oldest_supported
                self._last_mode = "repair"
            elif repair:
                catch_up_start = min(
                    checked + timedelta(days=1)
                    for checked in coverage
                    if checked is not None
                )
                start_date = max(
                    oldest_supported,
                    min(recent_start, catch_up_start),
                )
                self._last_mode = (
                    "catch_up" if start_date < recent_start else "refresh"
                )
            else:
                start_date = recent_start
                self._last_mode = "refresh"

        retry_dates_by_key = (
            self._next_retry_dates(
                oldest_supported=oldest_supported,
                before=start_date,
                today=today,
            )
            if repair and not full_scan
            else {}
        )
        retry_date = min(retry_dates_by_key.values(), default=None)
        record_count = (
            POWER_HISTORY_DAYS * MAX_HOURLY_RECORDS_PER_DAY
            if full_scan or retry_date is not None
            else (
                (today - start_date).days + 3
            ) * MAX_HOURLY_RECORDS_PER_DAY
        )
        try:
            existing_by_key = await self._read_existing_by_key(record_count)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Unable to inspect smart1 power statistics (%s)",
                type(err).__name__,
            )
            self._last_result = "recorder_unavailable"
            return
        supported_start = datetime.combine(
            oldest_supported,
            time.min,
            local_tz,
        ).astimezone(timezone.utc) - timedelta(hours=1)
        existing_by_key = {
            channel_key: [
                record
                for record in records
                if (
                    (record_start := self._record_start(record)) is not None
                    and supported_start <= record_start < now_utc
                )
            ]
            for channel_key, records in existing_by_key.items()
        }
        if not full_scan and self.history_state and any(
            self.history_state.data_presence(
                channel.statistic_id,
                POWER_HISTORY_SCHEMA_VERSION,
            )
            is True
            and not existing_by_key[channel.key]
            for channel in self.channels
        ):
            # A DB restore or manual statistics removal invalidates a
            # successful non-empty marker. Recreate the supported window.
            full_scan = True
            start_date = oldest_supported
            retry_dates_by_key = {}
            retry_date = None
            self._last_mode = "repair"

        expected_versions = {
            channel.statistic_id: (
                self.history_state.current_schema_version(
                    channel.statistic_id
                )
                if self.history_state
                else 0
            )
            for channel in self.channels
        }
        result = await self._fetch_hourly_means(
            start_date,
            today,
            local_tz,
            completed_before=now_utc,
        )
        self._last_fetch_completed = result.completed
        self._last_requested_days = result.requested_days
        self._last_emitted_hours = result.emitted_hours
        if not self._is_active():
            self._last_result = "inactive"
            return
        if result.checked_through is None:
            self._last_result = "fetch_incomplete"
            return

        retry_result: PowerHistoryFetchResult | None = None
        retry_completed = False
        if (
            result.completed
            and result.checked_through == today
            and retry_date is not None
        ):
            attempted_retry = await self._fetch_hourly_means(
                retry_date,
                retry_date,
                local_tz,
                completed_before=now_utc,
                include_following_boundary=True,
            )
            self._last_requested_days += attempted_retry.requested_days
            self._last_emitted_hours += attempted_retry.emitted_hours
            if (
                attempted_retry.completed
                and attempted_retry.checked_through == retry_date
            ):
                retry_result = attempted_retry
                retry_completed = True

        batches: dict[
            str,
            tuple[
                PowerHistoryChannel,
                StatisticMetaData,
                list[StatisticData],
            ],
        ] = {}
        for channel in self.channels:
            means = self._merged_means(result, retry_result, channel.key)
            statistics = build_hourly_power_statistics(means.items())
            metadata = self._metadata(channel)
            batches[channel.key] = (channel, metadata, statistics)

        validate_power_statistics_imports(
            (metadata, statistics)
            for _channel, metadata, statistics in batches.values()
        )
        if not self._is_active():
            self._last_result = "inactive"
            return
        for _channel, metadata, statistics in batches.values():
            if statistics:
                self._write_statistics(metadata, statistics)

        recorder = get_instance(self.hass)
        if any(
            statistics
            for _channel, _metadata, statistics in batches.values()
        ) and self._persistence_checker is None:
            await async_wait_for_recorder_commit(recorder)

        pending = _PendingPersistence(
            last_attempt_at=now_utc,
            batches=batches,
            expected_versions=expected_versions,
            existing_has_data={
                channel.key: bool(existing_by_key[channel.key])
                for channel in self.channels
            },
            result=result,
            retry_result=retry_result,
            retry_date=retry_date,
            retry_dates_by_key=retry_dates_by_key,
            retry_completed=retry_completed,
            today=today,
            oldest_supported=oldest_supported,
            source_time_zone=source_time_zone,
        )
        persisted_by_key = await self._confirm_batches(batches)
        if not self._is_active():
            self._last_result = "inactive"
            return
        if not self._expected_versions_unchanged(expected_versions):
            self._last_result = "state_changed"
            return
        if not all(persisted_by_key.values()):
            self._pending_persistence = pending
            self._last_result = "persistence_pending"
            return
        await self._finalize_persisted_batch(pending)

    async def _confirm_channel(
        self,
        channel: PowerHistoryChannel,
        statistics: list[StatisticData],
    ) -> tuple[str, bool]:
        """Confirm one queued batch."""
        if statistics:
            return (
                channel.key,
                await self._statistics_persisted(
                    channel.statistic_id,
                    statistics,
                ),
            )
        return channel.key, True
