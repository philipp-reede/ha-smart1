"""Non-blocking time-zone loading helpers for smart1 history."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util


async def async_resolve_time_zone(
    time_zone_name: str,
    configured_time_zone_name: str,
) -> ZoneInfo | None:
    """Return a time zone without loading tzdata in the event loop.

    Home Assistant has already loaded its configured default time zone. A
    different persisted history zone can require synchronous file I/O on its
    first ``ZoneInfo`` construction, so let Home Assistant resolve it in an
    executor instead.
    """
    if time_zone_name == configured_time_zone_name:
        configured_time_zone = dt_util.get_default_time_zone()
        if (
            isinstance(configured_time_zone, ZoneInfo)
            and configured_time_zone.key == time_zone_name
        ):
            return configured_time_zone

    return await dt_util.async_get_time_zone(time_zone_name)
