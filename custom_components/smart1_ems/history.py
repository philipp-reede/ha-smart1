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

_LOGGER = logging.getLogger(__name__)

HISTORY_DAYS = 365
REFRESH_DAYS = 3
PV_STATISTIC_ID = f"{DOMAIN}:pv_production"


def _statistics_date(record: Mapping[str, Any], local_tz: ZoneInfo) -> date:
    """Return the local date represented by a statistics record."""
    start = record["start"]
    if isinstance(start, datetime):
        start_time = start
    else:
        start_time = datetime.fromtimestamp(float(start), timezone.utc)

    return start_time.astimezone(local_tz).date()


def determine_import_window(
    records: list[Mapping[str, Any]],
    today: date,
    local_tz: ZoneInfo,
) -> tuple[date, float]:
    """Return the first date to refresh and its preceding cumulative sum."""
    if not records:
        return today - timedelta(days=HISTORY_DAYS - 1), 0.0

    refresh_start = today - timedelta(days=REFRESH_DAYS - 1)
    preceding = [
        record
        for record in records
        if record.get("sum") is not None
        and _statistics_date(record, local_tz) < refresh_start
    ]

    if preceding:
        baseline = max(
            preceding,
            key=lambda record: _statistics_date(record, local_tz),
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


class Smart1PvHistoryImporter:
    """Fetch and import daily photovoltaic production statistics."""

    def __init__(self, hass: HomeAssistant, api: Smart1Api) -> None:
        self.hass = hass
        self.api = api
        self._lock = asyncio.Lock()

    async def _existing_statistics(self) -> list[Mapping[str, Any]]:
        """Return enough recent records to establish a refresh baseline."""
        result = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            HISTORY_DAYS + REFRESH_DAYS + 7,
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

    async def async_import(self) -> None:
        """Import initial history or refresh the most recent days."""
        if self._lock.locked():
            return

        async with self._lock:
            local_tz = ZoneInfo(self.hass.config.time_zone)
            today = datetime.now(local_tz).date()
            records = await self._existing_statistics()
            start_date, baseline_sum = determine_import_window(
                records,
                today,
                local_tz,
            )
            fetched_energy = await self._fetch_daily_energy(start_date, today)
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

            if not statistics:
                _LOGGER.debug("No smart1 PV history available for import")
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
