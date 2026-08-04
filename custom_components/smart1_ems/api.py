from __future__ import annotations

import csv
import io
import logging
from datetime import date
from urllib.parse import quote

import aiohttp

from .const import BASE_URL
from .interface import parse_interface
from .point import Smart1Point
from .pv import parse_pv_cumulative_energy

_LOGGER = logging.getLogger(__name__)


class Smart1ApiError(Exception):
    """Error returned inside a smart1 CSV response."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"smart1 API error {code}")


class Smart1Api:
    """Client for the smart1 EMS portal API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        device_id: str,
    ) -> None:
        self.session = session
        self.api_key = api_key
        self.device_id = device_id

    async def _get_csv(
        self,
        path: str,
        *,
        missing_ok: bool = False,
    ) -> list[dict[str, str]]:
        """Execute a CSV request."""

        url = f"{BASE_URL}{path}?apikey={self.api_key}"
        safe_url = url.replace(self.api_key, "***")

        _LOGGER.debug("GET %s", safe_url)

        async with self.session.get(url, timeout=30) as response:
            text = await response.text()

            if missing_ok and response.status == 404:
                _LOGGER.debug("No smart1 data for %s", safe_url)
                return []

            if response.status >= 400:
                _LOGGER.error(
                    "HTTP %s for %s\n%s",
                    response.status,
                    safe_url,
                    text[:500],
                )
                response.raise_for_status()

        if not text.strip():
            return []

        rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))

        if rows and "Errorcode" in rows[0]:
            error_code = str(rows[0].get("Errorcode") or "unknown").strip()
            if missing_ok and error_code.startswith("404"):
                _LOGGER.debug("No smart1 data for %s", safe_url)
                return []
            raise Smart1ApiError(error_code)

        return rows

    async def get_plants(self) -> list[dict[str, str]]:
        """Return all plants."""
        return await self._get_csv("/plants")

    async def get_sensors(self) -> list[dict[str, str]]:
        """Return all sensors."""
        return await self._get_csv(f"/sensors/{self.device_id}")

    async def get_counters(self) -> list[dict[str, str]]:
        """Return all counters."""
        return await self._get_csv(f"/counters/{self.device_id}")

    def _counter_to_point(self, row: dict[str, str]) -> Smart1Point:
        """Convert one counter row into a Smart1Point."""

        return Smart1Point(
            id=(
                row.get("Counter Id")
                or row.get("CounterId")
                or row.get('"Counter Id"')
                or ""
            ),
            name=row.get("Name", ""),
            type=row.get("Type", ""),
            source="counter",
            hardware=row.get("Hardware", ""),
            interface=row.get("Interface", ""),
            max=row.get("Max", ""),
            color=row.get("Color", ""),
            index=row.get("Index", ""),
            raw=row,
            parsed_interface=parse_interface(row.get("Interface", "")),
        )

    def _sensor_to_point(self, row: dict[str, str]) -> Smart1Point:
        """Convert one sensor row into a Smart1Point."""

        return Smart1Point(
            id=row.get("SensorId", ""),
            name=row.get("Name", ""),
            type=row.get("Type", ""),
            source="sensor",
            hardware=row.get("Hardware", ""),
            interface=row.get("Interface", ""),
            max=row.get("Max", ""),
            color=row.get("Color", ""),
            index=row.get("Index", ""),
            raw=row,
            parsed_interface=parse_interface(row.get("Interface", "")),
        )

    async def get_linear_devices(self) -> list[Smart1Point]:
        """Return all enabled sensors and counters."""

        devices: list[Smart1Point] = []

        counters = await self.get_counters()

        for row in counters:
            if row.get("On", "on") != "on":
                continue

            device = self._counter_to_point(row)

            if device.id:
                devices.append(device)

        sensors = await self.get_sensors()

        for row in sensors:
            if row.get("On", "on") != "on":
                continue

            device = self._sensor_to_point(row)

            if device.id:
                devices.append(device)

        _LOGGER.debug("Discovered %d smart1 linear points", len(devices))

        return devices

    async def get_latest_linear_values(
        self,
        linear_ids: list[str],
    ) -> dict[str, float | None]:
        """Return latest values for all requested linear ids."""

        if not linear_ids:
            return {}

        today = date.today().strftime("%Y%m%d")

        ids = quote(",".join(linear_ids), safe=",")

        rows = await self._get_csv(
            f"/data/csv/{self.device_id}/linear/day/detailed/{today}/{ids}"
        )

        values = {linear_id: None for linear_id in linear_ids}

        for row in rows:
            linear_id = row.get("LinearId")

            raw = (
                row.get("Value1")
                or row.get("Value 1")
                or row.get('"Value 1"')
                or row.get("Value")
            )

            if linear_id not in values:
                continue

            if raw in (None, "", "No value", '"No value"'):
                continue

            try:
                values[linear_id] = float(str(raw).replace(",", "."))

            except ValueError:
                _LOGGER.warning(
                    "Cannot parse value '%s' for %s",
                    raw,
                    linear_id,
                )

        return values

    async def get_pv_cumulative_energy(
        self,
        period: str = "day",
        target_date: date | None = None,
        *,
        missing_ok: bool = False,
    ) -> float | None:
        """Return cumulative PV production in kWh."""

        if period not in {"day", "month", "year"}:
            raise ValueError(f"Unsupported cumulative period: {period}")

        date_string = (target_date or date.today()).strftime("%Y%m%d")
        rows = await self._get_csv(
            f"/data/csv/{self.device_id}/photovoltaics/"
            f"{period}/cumulative/{date_string}",
            missing_ok=missing_ok,
        )

        return parse_pv_cumulative_energy(rows)

    async def get_linear_cumulative_rows(
        self,
        linear_ids: list[str],
        period: str = "day",
        target_date: date | None = None,
        *,
        missing_ok: bool = False,
    ) -> list[dict[str, str]]:
        """Return unmodified rows from the undocumented cumulative response."""
        if period not in {"day", "month", "year"}:
            raise ValueError(f"Unsupported cumulative period: {period}")

        if not linear_ids:
            return []

        date_string = (target_date or date.today()).strftime("%Y%m%d")
        ids = quote(",".join(linear_ids), safe=",")
        return await self._get_csv(
            f"/data/csv/{self.device_id}/linear/"
            f"{period}/cumulative/{date_string}/{ids}",
            missing_ok=missing_ok,
        )
