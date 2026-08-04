"""Import energy derived from selected smart1 five-minute power points."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date, datetime, timedelta
import logging
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import ClientError

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
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
from .history import (
    HISTORY_DAYS,
    REFRESH_DAYS,
    build_daily_energy_statistics,
    determine_import_window,
    merge_daily_energy,
)
from .point import Smart1Point
from .power_integration import integrate_power_rows

_LOGGER = logging.getLogger(__name__)


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
            HISTORY_DAYS + REFRESH_DAYS + 7,
            statistic_id,
            True,
            {"state", "sum"},
        )
        return result.get(statistic_id, [])

    async def _fetch_daily_energy(
        self,
        start_date: date,
        end_date: date,
        local_tz: ZoneInfo,
    ) -> dict[str, list[tuple[date, float]]]:
        """Fetch each date once and integrate all selected points."""
        daily_energy = {role_key: [] for role_key in self.role_points}
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
                break

            for role_key, point in self.role_points.items():
                integration = integrate_power_rows(rows, point.id, local_tz)
                if integration.integrated_intervals:
                    daily_energy[role_key].append(
                        (target_date, integration.energy_kwh)
                    )

            target_date += timedelta(days=1)

        return daily_energy

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
            windows = {
                role_key: determine_import_window(records, today, local_tz)
                for role_key, records in records_by_role.items()
            }
            start_date = min(window[0] for window in windows.values())
            fetched_by_role = await self._fetch_daily_energy(
                start_date,
                today,
                local_tz,
            )

            for role_key, point in self.role_points.items():
                role_start, baseline_sum = windows[role_key]
                fetched_energy = [
                    item
                    for item in fetched_by_role[role_key]
                    if item[0] >= role_start
                ]
                daily_energy = merge_daily_energy(
                    records_by_role[role_key],
                    fetched_energy,
                    role_start,
                    today,
                    local_tz,
                )
                statistics = build_daily_energy_statistics(
                    daily_energy,
                    baseline_sum,
                    local_tz,
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
                    "Imported %d days for smart1 derived energy role %s",
                    len(statistics),
                    role_key,
                )
