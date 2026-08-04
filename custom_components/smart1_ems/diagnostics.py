"""Diagnostics support for the smart1 EMS integration."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from aiohttp import ClientError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import Smart1Api
from .classifier import classify_point
from .const import DOMAIN
from .point import Smart1Point


def _point_diagnostics(number: int, point: Smart1Point) -> dict[str, Any]:
    """Return non-value metadata used to classify one point."""
    parsed = point.parsed_interface

    return {
        "point_number": number,
        "name": point.name,
        "type": point.type,
        "source": point.source,
        "hardware": point.hardware,
        "interface": point.interface,
        "max": point.max,
        "index": point.index,
        "parsed_interface": {
            "protocol": parsed.protocol if parsed else None,
            "service": parsed.service if parsed else None,
            "service_id": parsed.service_id if parsed else None,
            "object_type": parsed.object_type if parsed else None,
            "object_id": parsed.object_id if parsed else None,
            "signal": parsed.signal if parsed else None,
        },
        "current_category": classify_point(point).value,
    }


async def _linear_cumulative_probe(
    api: Smart1Api,
    numbered_points: list[tuple[int, Smart1Point]],
) -> dict[str, Any]:
    """Probe yesterday's cumulative endpoint without exposing IDs or values."""
    energy_points = [
        (number, point)
        for number, point in numbered_points
        if point.source == "counter" and point.type.lower() == "energy"
    ]
    point_numbers_by_id = {
        point.id: number for number, point in energy_points
    }
    target_date = date.today() - timedelta(days=1)

    try:
        rows = await api.get_linear_cumulative_rows(
            [point.id for _, point in energy_points],
            target_date=target_date,
            missing_ok=True,
        )
    except (ClientError, TimeoutError) as err:
        return {
            "period": "previous_complete_day",
            "requested_points": len(energy_points),
            "result": "request_failed",
            "error_type": type(err).__name__,
        }

    points_with_rows = sorted(
        {
            point_numbers_by_id[linear_id]
            for row in rows
            if (linear_id := row.get("LinearId")) in point_numbers_by_id
        }
    )
    columns = sorted({column for row in rows for column in row})

    return {
        "period": "previous_complete_day",
        "requested_points": len(energy_points),
        "result": "data_returned" if rows else "no_data",
        "response_rows": len(rows),
        "response_columns": columns,
        "point_numbers_with_rows": points_with_rows,
        "unmatched_response_rows": len(rows) - sum(
            1 for row in rows if row.get("LinearId") in point_numbers_by_id
        ),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics without credentials, device IDs, or live values."""
    runtime_data = hass.data[DOMAIN][entry.entry_id]
    points = runtime_data["devices"]
    numbered_points = list(enumerate(points, start=1))

    return {
        "integration": DOMAIN,
        "discovery": runtime_data["discovery"].to_dict(),
        "linear_cumulative_probe": await _linear_cumulative_probe(
            runtime_data["api"],
            numbered_points,
        ),
        "points": [
            _point_diagnostics(number, point)
            for number, point in numbered_points
        ],
    }
