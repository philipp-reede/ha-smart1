"""Integrate smart1 power samples without bridging large data gaps."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

MAX_SAMPLE_GAP = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class PowerIntegrationResult:
    """Summary of trapezoidal power integration."""

    energy_kwh: float
    sample_count: int
    integrated_intervals: int
    skipped_gaps: int
    covered_seconds: float


def _row_value(row: Mapping[str, Any]) -> float | None:
    """Return a numeric power value from one smart1 row."""
    raw_value = (
        row.get("Value1")
        or row.get("Value 1")
        or row.get('"Value 1"')
        or row.get("Value")
    )
    if raw_value in (None, "", "No value", '"No value"'):
        return None

    try:
        return max(0.0, float(str(raw_value).replace(",", ".")))
    except ValueError:
        return None


def _row_timestamp(
    row: Mapping[str, Any],
    local_tz: ZoneInfo,
) -> datetime | None:
    """Return one smart1 timestamp normalized to UTC."""
    raw_timestamp = row.get("Timestamp")
    if not raw_timestamp:
        return None

    try:
        timestamp = datetime.fromisoformat(str(raw_timestamp).strip('"'))
    except ValueError:
        return None

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=local_tz)

    return timestamp.astimezone(timezone.utc)


def integrate_power_rows(
    rows: list[Mapping[str, Any]],
    linear_id: str,
    local_tz: ZoneInfo,
) -> PowerIntegrationResult:
    """Integrate one point's W samples into kWh using the trapezoidal rule."""
    samples: dict[datetime, float] = {}

    for row in rows:
        if row.get("LinearId") != linear_id:
            continue

        timestamp = _row_timestamp(row, local_tz)
        value = _row_value(row)
        if timestamp is None or value is None:
            continue

        samples[timestamp] = value

    ordered_samples = sorted(samples.items())
    energy_kwh = 0.0
    integrated_intervals = 0
    skipped_gaps = 0
    covered_seconds = 0.0

    for (previous_time, previous_value), (current_time, current_value) in zip(
        ordered_samples,
        ordered_samples[1:],
        strict=False,
    ):
        interval_seconds = (current_time - previous_time).total_seconds()
        if interval_seconds <= 0:
            continue
        if interval_seconds > MAX_SAMPLE_GAP.total_seconds():
            skipped_gaps += 1
            continue

        average_power_w = (previous_value + current_value) / 2
        energy_kwh += average_power_w * interval_seconds / 3_600_000
        integrated_intervals += 1
        covered_seconds += interval_seconds

    return PowerIntegrationResult(
        energy_kwh=energy_kwh,
        sample_count=len(ordered_samples),
        integrated_intervals=integrated_intervals,
        skipped_gaps=skipped_gaps,
        covered_seconds=covered_seconds,
    )
