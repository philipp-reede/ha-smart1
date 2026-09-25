"""Import energy derived from selected smart1 five-minute power points."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
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
from .energy_roles import ENERGY_ROLES_BY_KEY, statistic_id_for_role
from .history import (
    HISTORY_DAYS,
    MAX_HOURLY_RECORDS_PER_DAY,
    REFRESH_DAYS,
)
from .history_state import (
    DERIVED_HISTORY_SCHEMA_VERSION,
    Smart1HistoryState,
)
from .point import Smart1Point
from .power_integration import integrate_power_rows
from .recorder_helpers import (
    async_clear_statistics,
    validate_energy_statistics_imports,
)

_LOGGER = logging.getLogger(__name__)

# ``get_last_statistics`` counts records rather than days.  Derived history is
# hourly, so retain enough rows to reconstruct the complete supported history
# when a legacy statistic must be cleared and written again.
STATISTICS_LOOKBACK = HISTORY_DAYS * MAX_HOURLY_RECORDS_PER_DAY
FETCH_ATTEMPTS = 3


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
) -> tuple[date, float]:
    """Return the hourly refresh start and preceding cumulative sum.

    Pre-marker daily-only statistics trigger a full import under the same
    statistic ID so Energy Dashboard configuration remains intact. Once the
    current backfill is complete, sparse midnight hours are valid.
    """
    initial_start = today - timedelta(days=HISTORY_DAYS - 1)
    if force_initial_rebuild:
        return initial_start, 0.0
    if not records:
        if initial_backfill_complete:
            return today - timedelta(days=refresh_days - 1), 0.0
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
) -> list[tuple[datetime, float]]:
    """Replace fetched local days while preserving temporarily missing days.

    Home Assistant's external-statistics API upserts records but does not
    delete obsolete buckets. Explicit zero-value tombstones therefore replace
    stale hours when a day's derived distribution changes.
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


def build_hourly_energy_statistics(
    hourly_energy: list[tuple[datetime, float]],
    baseline_sum: float,
) -> list[StatisticData]:
    """Build cumulative Home Assistant statistics from hourly kWh values."""
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

    def _is_complete(self, statistic_id: str) -> bool:
        """Return whether a statistic completed its current backfill."""
        return bool(
            self.history_state
            and self.history_state.is_complete(
                statistic_id,
                DERIVED_HISTORY_SCHEMA_VERSION,
            )
        )

    def _data_presence(self, statistic_id: str) -> bool | None:
        """Return whether a completed role backfill produced recorder rows."""
        if not self.history_state:
            return None
        return self.history_state.data_presence(
            statistic_id,
            DERIVED_HISTORY_SCHEMA_VERSION,
        )

    def _mark_complete(
        self,
        statistic_id: str,
        *,
        has_data: bool,
    ) -> None:
        """Persist completion of one derived statistic backfill."""
        if self.history_state:
            self.history_state.mark_complete(
                statistic_id,
                DERIVED_HISTORY_SCHEMA_VERSION,
                has_data=has_data,
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
    ) -> tuple[dict[str, list[tuple[datetime, float]]], bool]:
        """Fetch each date once and integrate selected points by UTC hour."""
        hourly_energy = {role_key: [] for role_key in self.role_points}
        linear_ids = list(
            dict.fromkeys(point.id for point in self.role_points.values())
        )
        target_date = start_date

        while target_date <= end_date:
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
                        return hourly_energy, False
                    await asyncio.sleep(2**attempt)

            for role_key, point in self.role_points.items():
                integration = integrate_power_rows(rows, point.id, local_tz)
                if integration.integrated_intervals:
                    hourly_energy[role_key].extend(
                        integration.hourly_energy_kwh
                    )

            target_date += timedelta(days=1)

        return hourly_energy, True

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
            self._last_result = "running"
            self._last_mode = "history_repair" if repair else "current_day"
            self._last_fetch_completed = None
            local_tz = ZoneInfo(self.hass.config.time_zone)
            today = datetime.now(local_tz).date()
            statistic_ids = {
                role_key: statistic_id_for_role(
                    role_key,
                    point.id,
                    self.statistics_namespace,
                )
                for role_key, point in self.role_points.items()
            }
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
            forced_rebuild_roles = (
                set(self.role_points) - complete_roles
                if self.force_initial_rebuild
                else set()
            )
            detected_rebuild_roles |= forced_rebuild_roles
            self._detected_rebuild_roles = tuple(
                sorted(detected_rebuild_roles)
            )

            # Adopt valid pre-marker hourly statistics without a redundant
            # year-long import. Legacy/rebuild data still takes the full path.
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
                )
                for role_key, records in records_by_role.items()
                if role_key in refreshable_roles
            }
            start_date = min(window[0] for window in windows.values())
            fetched_by_role, fetch_completed = await self._fetch_hourly_energy(
                start_date,
                today,
                local_tz,
            )
            self._last_fetch_completed = fetch_completed
            initial_backfill_roles = refreshable_roles - complete_roles
            if initial_backfill_roles and not fetch_completed:
                _LOGGER.warning(
                    "Deferring initial smart1 derived energy import because "
                    "the history fetch did not complete",
                )
                self._last_result = "incomplete_fetch"
                return
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
                    if item[0].astimezone(local_tz).date() >= role_start
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
            if forced_clear_roles:
                rebuild_statistic_ids = [
                    statistic_ids[role_key]
                    for role_key in sorted(forced_clear_roles)
                ]
                expected_versions = {
                    role_key: (
                        self.history_state.current_schema_version(
                            statistic_ids[role_key]
                        )
                        if self.history_state
                        else 0
                    )
                    for role_key in forced_clear_roles
                }

                def _mark_rebuild_complete() -> None:
                    if not self.history_state:
                        return
                    for role_key in forced_clear_roles:
                        self.history_state.mark_complete_if_unchanged(
                            statistic_ids[role_key],
                            DERIVED_HISTORY_SCHEMA_VERSION,
                            has_data=role_key in roles_with_statistics,
                            expected_version=expected_versions[role_key],
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
            for role_key in completed_roles:
                self._mark_complete(
                    statistic_ids[role_key],
                    has_data=(
                        role_key in roles_with_statistics
                        or (
                            role_key not in forced_rebuild_roles
                            and bool(records_by_role[role_key])
                        )
                    ),
                )
            for role_key in roles_with_statistics - completed_roles:
                if self._data_presence(statistic_ids[role_key]) is not True:
                    self._mark_complete(
                        statistic_ids[role_key],
                        has_data=True,
                    )

            self._last_result = (
                "completed"
                if fetch_completed
                else "completed_with_partial_fetch"
            )
