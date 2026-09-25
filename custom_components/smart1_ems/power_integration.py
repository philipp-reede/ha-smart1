"""Integrate smart1 power samples without bridging large data gaps."""

from __future__ import annotations

from collections import defaultdict
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
    hourly_energy_kwh: tuple[tuple[datetime, float], ...]
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


def _row_timestamp(row: Mapping[str, Any]) -> datetime | None:
    """Parse one smart1 timestamp without discarding offset information."""
    raw_timestamp = row.get("Timestamp")
    if not raw_timestamp:
        return None

    try:
        return datetime.fromisoformat(str(raw_timestamp).strip('"'))
    except ValueError:
        return None


def _utc_candidates(
    timestamp: datetime,
    local_tz: ZoneInfo,
) -> tuple[datetime, ...]:
    """Return possible UTC instants for one parsed timestamp."""
    if timestamp.tzinfo is not None:
        return (timestamp.astimezone(timezone.utc),)

    fold_zero = timestamp.replace(tzinfo=local_tz, fold=0).astimezone(
        timezone.utc
    )
    fold_one = timestamp.replace(tzinfo=local_tz, fold=1).astimezone(
        timezone.utc
    )
    if fold_zero < fold_one:
        # A backward clock change makes the local timestamp ambiguous.
        return (fold_zero, fold_one)
    return (fold_zero,)


def _sequence_is_descending(
    timestamps: list[datetime],
    local_tz: ZoneInfo,
) -> bool:
    """Return whether the CSV rows are predominantly newest-first."""
    wall_times = [
        (
            timestamp
            if timestamp.tzinfo is None
            else timestamp.astimezone(local_tz).replace(tzinfo=None)
        )
        for timestamp in timestamps
    ]
    ascending_steps = sum(
        current > previous
        for previous, current in zip(
            wall_times,
            wall_times[1:],
            strict=False,
        )
    )
    descending_steps = sum(
        current < previous
        for previous, current in zip(
            wall_times,
            wall_times[1:],
            strict=False,
        )
    )
    if ascending_steps != descending_steps:
        return descending_steps > ascending_steps
    return len(wall_times) > 1 and wall_times[-1] < wall_times[0]


def _resolve_utc_timestamps(
    timestamps: list[datetime],
    local_tz: ZoneInfo,
) -> tuple[datetime, ...]:
    """Resolve naive ambiguous timestamps while preserving CSV row order."""
    if not timestamps:
        return ()

    candidate_rows = [
        _utc_candidates(timestamp, local_tz) for timestamp in timestamps
    ]
    if all(len(candidates) == 1 for candidates in candidate_rows):
        return tuple(candidates[0] for candidates in candidate_rows)

    descending = _sequence_is_descending(timestamps, local_tz)
    states: list[tuple[int, float, tuple[datetime, ...]]] = [
        (0, 0.0, (candidate,)) for candidate in candidate_rows[0]
    ]

    for candidates in candidate_rows[1:]:
        next_states: list[tuple[int, float, tuple[datetime, ...]]] = []
        for candidate in candidates:
            transitions = []
            for violations, elapsed, path in states:
                directional_delta = (
                    path[-1] - candidate
                    if descending
                    else candidate - path[-1]
                ).total_seconds()
                transitions.append(
                    (
                        violations + (directional_delta < 0),
                        elapsed + abs(directional_delta),
                        (*path, candidate),
                    )
                )
            next_states.append(
                min(
                    transitions,
                    key=lambda state: (state[0], state[1]),
                )
            )
        states = next_states

    return min(
        states,
        key=lambda state: (
            state[0],
            state[1],
            -state[2][-1].timestamp()
            if descending
            else state[2][-1].timestamp(),
        ),
    )[2]


def integrate_power_rows(
    rows: list[Mapping[str, Any]],
    linear_id: str,
    local_tz: ZoneInfo,
) -> PowerIntegrationResult:
    """Integrate one point's W samples into kWh using the trapezoidal rule."""
    samples: dict[datetime, float] = {}
    parsed_samples: list[tuple[datetime, float]] = []

    for row in rows:
        if row.get("LinearId") != linear_id:
            continue

        timestamp = _row_timestamp(row)
        value = _row_value(row)
        if timestamp is None or value is None:
            continue

        parsed_samples.append((timestamp, value))

    resolved_timestamps = _resolve_utc_timestamps(
        [timestamp for timestamp, _value in parsed_samples],
        local_tz,
    )
    for timestamp, (_parsed_timestamp, value) in zip(
        resolved_timestamps,
        parsed_samples,
        strict=True,
    ):
        samples[timestamp] = value

    ordered_samples = sorted(samples.items())
    energy_kwh = 0.0
    hourly_energy_kwh: defaultdict[datetime, float] = defaultdict(float)
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
        interval_energy_kwh = average_power_w * interval_seconds / 3_600_000
        energy_kwh += interval_energy_kwh
        integrated_intervals += 1
        covered_seconds += interval_seconds

        segment_start = previous_time
        while segment_start < current_time:
            hour_start = segment_start.replace(
                minute=0,
                second=0,
                microsecond=0,
            )
            segment_end = min(current_time, hour_start + timedelta(hours=1))
            start_fraction = (
                (segment_start - previous_time).total_seconds()
                / interval_seconds
            )
            end_fraction = (
                (segment_end - previous_time).total_seconds()
                / interval_seconds
            )
            segment_start_power = previous_value + (
                current_value - previous_value
            ) * start_fraction
            segment_end_power = previous_value + (
                current_value - previous_value
            ) * end_fraction
            segment_seconds = (segment_end - segment_start).total_seconds()
            hourly_energy_kwh[hour_start] += (
                (segment_start_power + segment_end_power)
                / 2
                * segment_seconds
                / 3_600_000
            )
            segment_start = segment_end

    return PowerIntegrationResult(
        energy_kwh=energy_kwh,
        hourly_energy_kwh=tuple(sorted(hourly_energy_kwh.items())),
        sample_count=len(ordered_samples),
        integrated_intervals=integrated_intervals,
        skipped_gaps=skipped_gaps,
        covered_seconds=covered_seconds,
    )
