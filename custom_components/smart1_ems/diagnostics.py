"""Diagnostics support for the smart1 EMS integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

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


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics without credentials, device IDs, or live values."""
    runtime_data = hass.data[DOMAIN][entry.entry_id]
    points = runtime_data["devices"]

    return {
        "integration": DOMAIN,
        "discovery": runtime_data["discovery"].to_dict(),
        "points": [
            _point_diagnostics(number, point)
            for number, point in enumerate(points, start=1)
        ],
    }
