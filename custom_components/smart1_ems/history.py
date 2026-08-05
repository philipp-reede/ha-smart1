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

from .api import Smart1Api, Smart1ApiError
from .const import DOMAIN
from .point import Smart1Point
from .power_integration import PowerIntegrationResult, integrate_power_rows

_LOGGER = logging.getLogger(__name__)

HISTORY_DAYS = 365
REFRESH_DAYS = 3
PV_STATISTIC_ID = f"{DOMAIN}:pv_production"
PV_FETCH_ATTEMPTS = 3
PV_STATISTICS_LOOKBACK = HISTORY_DAYS * 25


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
) -> tuple[date, float]:
    """Return the first date to refresh and its preceding cumulative sum."""
    if not records:
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
                start=datetime.combine(target_date, time.min, tzinfo=local_tz),
                state=energy_kwh,
                sum=cumulative_sum,
            )
        )

    return statistics


def merge_daily_energy(
    records: list[Mapping[str, Any]],
    fetched_energy: list[tuple[date, float]],
    start_date: date,
    end_date: date,
    local_tz: ZoneInfo,
) -> list[tuple[date, float]]:
    """Merge refreshed values with existing data for temporarily missing dates."""
    daily_energy: dict[date, float] = {}

    for record in records:
        energy_kwh = record.get("state")
        if energy_kwh is None:
            continue

        target_date = _statistics_date(record, local_tz)
        if start_date <= target_date <= end_date:
            daily_energy[target_date] = float(energy_kwh)

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


def determine_hourly_pv_import_window(
    records: list[Mapping[str, Any]],
    today: date,
    local_tz: ZoneInfo,
    refresh_days: int = REFRESH_DAYS,
) -> tuple[date, float]:
    """Return the hourly PV refresh window and preceding cumulative sum."""
    if not records or needs_hourly_pv_migration(records, local_tz):
        return today - timedelta(days=HISTORY_DAYS - 1), 0.0
    return determine_import_window(records, today, local_tz, refresh_days)


def distribute_exact_pv_energy(
    target_date: date,
    exact_energy_kwh: float | None,
    integration: PowerIntegrationResult,
    local_tz: ZoneInfo,
) -> tuple[list[tuple[datetime, float]], bool]:
    """Scale measured hourly PV distribution to its exact daily total."""
    if exact_energy_kwh is None or exact_energy_kwh < 0:
        return [], False

    midnight = datetime.combine(
        target_date,
        time.min,
        tzinfo=local_tz,
    ).astimezone(timezone.utc)
    hourly_energy = dict(integration.hourly_energy_kwh)
    hourly_energy.setdefault(midnight, 0.0)
    integrated_total = sum(hourly_energy.values())

    if exact_energy_kwh == 0:
        return [(midnight, 0.0)], True
    if integrated_total <= 0:
        return [(midnight, exact_energy_kwh)], False

    scale = exact_energy_kwh / integrated_total
    scaled = {
        start: energy_kwh * scale
        for start, energy_kwh in hourly_energy.items()
    }
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
    """Merge refreshed PV hours over existing daily or hourly records."""
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
    ) -> None:
        self.hass = hass
        self.api = api
        self.pv_power_point = pv_power_point
        self._lock = asyncio.Lock()
        self._last_result = "not_started"
        self._last_refresh_days: int | None = None
        self._last_fetched_days = 0
        self._last_distributed_days = 0
        self._last_daily_fallback_days = 0
        self._migration_required = False

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
        }

    async def _existing_statistics(self) -> list[Mapping[str, Any]]:
        """Return enough recent records to establish a refresh baseline."""
        result = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            PV_STATISTICS_LOOKBACK,
            PV_STATISTIC_ID,
            True,
            {"state", "sum"},
        )
        return result.get(PV_STATISTIC_ID, [])

    async def _fetch_daily_energy(
        self,
        start_date: date,
        end_date: date,
    ) -> list[tuple[date, float]]:
        """Fetch daily production without failing on dates with no data."""
        daily_energy: list[tuple[date, float]] = []
        target_date = start_date

        while target_date <= end_date:
            try:
                value = await self.api.get_pv_cumulative_energy(
                    target_date=target_date,
                    missing_ok=True,
                )
            except (ClientError, Smart1ApiError, TimeoutError) as err:
                _LOGGER.warning(
                    "Unable to import smart1 PV history from %s: %s",
                    target_date,
                    err,
                )
                break

            if value is not None:
                daily_energy.append((target_date, value))

            target_date += timedelta(days=1)

        return daily_energy

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
                                err,
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
                                err,
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
            records = await self._existing_statistics()
            self._migration_required = needs_hourly_pv_migration(
                records,
                local_tz,
            )
            if self.pv_power_point is None:
                start_date, baseline_sum = determine_import_window(
                    records,
                    today,
                    local_tz,
                    refresh_days,
                )
                fetched_energy = await self._fetch_daily_energy(
                    start_date,
                    today,
                )
                self._last_fetched_days = len(fetched_energy)
                daily_energy = merge_daily_energy(
                    records,
                    fetched_energy,
                    start_date,
                    today,
                    local_tz,
                )
                statistics = build_daily_energy_statistics(
                    daily_energy,
                    baseline_sum,
                    local_tz,
                )
                self._last_result = "completed_daily_fallback"
            else:
                if self._migration_required and not repair:
                    self._last_result = "repair_pending"
                    return
                start_date, baseline_sum = determine_hourly_pv_import_window(
                    records,
                    today,
                    local_tz,
                    refresh_days,
                )
                exact_fallback = (
                    _daily_exact_fallback(records, local_tz)
                    if self._migration_required
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
                hourly_energy = merge_hourly_pv_energy(
                    records,
                    fetched_hourly_energy,
                    start_date,
                    today,
                    local_tz,
                )
                statistics = build_hourly_pv_statistics(
                    hourly_energy,
                    baseline_sum,
                )
                if daily_fallback_days:
                    self._last_result = "completed_with_daily_fallback"
                elif fetch_completed:
                    self._last_result = "completed"
                else:
                    self._last_result = "completed_with_partial_fetch"

            if not statistics:
                _LOGGER.debug("No smart1 PV history available for import")
                self._last_result = "no_data"
                return

            async_add_external_statistics(
                hass=self.hass,
                metadata=StatisticMetaData(
                    mean_type=StatisticMeanType.NONE,
                    has_sum=True,
                    name="smart1 EMS PV production",
                    source=DOMAIN,
                    statistic_id=PV_STATISTIC_ID,
                    unit_class=EnergyConverter.UNIT_CLASS,
                    unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                ),
                statistics=statistics,
            )
            _LOGGER.info(
                "Imported %d days of smart1 PV history",
                len(statistics),
            )
            if self._migration_required:
                await get_instance(self.hass).async_block_till_done()
                if (
                    self.pv_power_point is not None
                    and fetch_completed
                    and daily_fallback_days == 0
                ):
                    self._migration_required = False
