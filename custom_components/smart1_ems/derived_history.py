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

from .api import Smart1Api, Smart1ApiError
from .const import DOMAIN
from .energy_roles import ENERGY_ROLES_BY_KEY, statistic_id_for_role
from .history import HISTORY_DAYS, REFRESH_DAYS
from .point import Smart1Point
from .power_integration import integrate_power_rows

_LOGGER = logging.getLogger(__name__)

STATISTICS_LOOKBACK = HISTORY_DAYS + REFRESH_DAYS + 7


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


def needs_hourly_rebuild(
    records: list[Mapping[str, Any]],
    local_tz: ZoneInfo,
) -> bool:
    """Return whether derived statistics must be cleared and rebuilt."""
    return bool(records) and (
        not _has_hourly_resolution(records, local_tz)
        or _has_decreasing_sum(records)
    )


def determine_hourly_import_window(
    records: list[Mapping[str, Any]],
    today: date,
    local_tz: ZoneInfo,
) -> tuple[date, float]:
    """Return the hourly refresh start and preceding cumulative sum.

    Existing daily-only statistics trigger a full import under the same
    statistic ID so Energy Dashboard configuration remains intact.
    """
    initial_start = today - timedelta(days=HISTORY_DAYS - 1)
    if not records or needs_hourly_rebuild(records, local_tz):
        return initial_start, 0.0

    refresh_start = today - timedelta(days=REFRESH_DAYS - 1)
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
    """Merge refreshed hourly values with existing statistics as fallback."""
    energy_by_hour: dict[datetime, float] = {}

    for record in records:
        energy_kwh = record.get("state")
        if energy_kwh is None:
            continue

        start = _statistics_start(record)
        target_date = start.astimezone(local_tz).date()
        if start_date <= target_date <= end_date:
            energy_by_hour[start] = float(energy_kwh)

    energy_by_hour.update(
        (start.astimezone(timezone.utc), energy_kwh)
        for start, energy_kwh in fetched_energy
    )
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
    ) -> None:
        self.hass = hass
        self.api = api
        self.role_points = role_points
        self._lock = asyncio.Lock()

    async def _existing_statistics(
        self,
        statistic_id: str,
    ) -> list[Mapping[str, Any]]:
        """Return recent records for one derived statistic."""
        result = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            STATISTICS_LOOKBACK,
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
            try:
                rows = await self.api.get_linear_detailed_rows(
                    linear_ids,
                    target_date=target_date,
                    missing_ok=True,
                )
            except (ClientError, Smart1ApiError, TimeoutError) as err:
                _LOGGER.warning(
                    "Unable to import derived smart1 energy from %s: %s",
                    target_date,
                    err,
                )
                return hourly_energy, False

            for role_key, point in self.role_points.items():
                integration = integrate_power_rows(rows, point.id, local_tz)
                if integration.integrated_intervals:
                    hourly_energy[role_key].extend(
                        integration.hourly_energy_kwh
                    )

            target_date += timedelta(days=1)

        return hourly_energy, True

    async def async_import(self) -> None:
        """Import initial derived history or refresh recent days."""
        if self._lock.locked() or not self.role_points:
            return

        async with self._lock:
            local_tz = ZoneInfo(self.hass.config.time_zone)
            today = datetime.now(local_tz).date()
            statistic_ids = {
                role_key: statistic_id_for_role(role_key, point.id)
                for role_key, point in self.role_points.items()
            }
            records_by_role = {
                role_key: await self._existing_statistics(statistic_id)
                for role_key, statistic_id in statistic_ids.items()
            }
            rebuild_roles = {
                role_key
                for role_key, records in records_by_role.items()
                if needs_hourly_rebuild(records, local_tz)
            }
            windows = {
                role_key: determine_hourly_import_window(
                    records,
                    today,
                    local_tz,
                )
                for role_key, records in records_by_role.items()
            }
            start_date = min(window[0] for window in windows.values())
            fetched_by_role, fetch_completed = await self._fetch_hourly_energy(
                start_date,
                today,
                local_tz,
            )
            missing_replacements = {
                role_key
                for role_key in rebuild_roles
                if not fetched_by_role[role_key]
            }
            if rebuild_roles and (
                not fetch_completed or missing_replacements
            ):
                _LOGGER.warning(
                    "Keeping existing smart1 energy statistics because the "
                    "replacement history is incomplete "
                    "(fetch completed: %s, roles without data: %d)",
                    fetch_completed,
                    len(missing_replacements),
                )
                return

            recorder = get_instance(self.hass)
            if rebuild_roles:
                rebuild_statistic_ids = [
                    statistic_ids[role_key]
                    for role_key in sorted(rebuild_roles)
                ]
                recorder.async_clear_statistics(rebuild_statistic_ids)
                await recorder.async_block_till_done()
                _LOGGER.info(
                    "Cleared %d smart1 derived energy statistics before rebuild",
                    len(rebuild_statistic_ids),
                )

            for role_key, point in self.role_points.items():
                role_start, baseline_sum = windows[role_key]
                fetched_energy = [
                    item
                    for item in fetched_by_role[role_key]
                    if item[0].astimezone(local_tz).date() >= role_start
                ]
                if role_key in rebuild_roles:
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
                async_add_external_statistics(
                    hass=self.hass,
                    metadata=StatisticMetaData(
                        mean_type=StatisticMeanType.NONE,
                        has_sum=True,
                        name=f"{role.name} ({point.name})",
                        source=DOMAIN,
                        statistic_id=statistic_ids[role_key],
                        unit_class=EnergyConverter.UNIT_CLASS,
                        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                    ),
                    statistics=statistics,
                )
                _LOGGER.info(
                    "Imported %d hourly records for smart1 derived energy role %s",
                    len(statistics),
                    role_key,
                )

            if rebuild_roles:
                await recorder.async_block_till_done()
