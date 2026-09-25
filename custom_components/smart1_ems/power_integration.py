"""Integrate smart1 power samples without bridging large data gaps."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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


def _normalize_ambiguous_samples(
    samples: list[tuple[datetime, float]],
    local_tz: ZoneInfo,
) -> list[tuple[datetime, float]]:
    """Resolve ambiguous wall times from their row-order-independent multiset.

    Exact duplicate rows alone are not evidence that both sides of a
    daylight-saving-time fold are present. Distinct values at one wall time do
    prove that the hour repeated. A dense full-hour sequence of duplicates is
    also accepted so two genuinely identical fold profiles remain usable.
    """
    ambiguous_values: defaultdict[datetime, list[float]] = defaultdict(list)
    non_ambiguous_samples: list[tuple[datetime, float]] = []
    support_by_date: defaultdict[date, list[datetime]] = defaultdict(list)
    non_ambiguous_counts: defaultdict[
        tuple[date, datetime, float], int
    ] = defaultdict(int)

    for timestamp, value in samples:
        candidates = _utc_candidates(timestamp, local_tz)
        if timestamp.tzinfo is None and len(candidates) > 1:
            ambiguous_values[timestamp].append(value)
            continue

        non_ambiguous_samples.append((timestamp, value))
        utc_timestamp = candidates[0]
        local_date = utc_timestamp.astimezone(local_tz).date()
        support_by_date[local_date].append(utc_timestamp)
        non_ambiguous_counts[(local_date, utc_timestamp, value)] += 1

    groups_by_date: defaultdict[
        date, dict[datetime, list[float]]
    ] = defaultdict(dict)
    for timestamp, values in ambiguous_values.items():
        groups_by_date[timestamp.date()][timestamp] = values

    dates_with_non_ambiguous_duplicates = {
        local_date
        for (local_date, _timestamp, _value), count in (
            non_ambiguous_counts.items()
        )
        if count > 1
    }

    normalized = list(non_ambiguous_samples)
    for local_date, date_groups in groups_by_date.items():
        fold_confirmed = any(
            len(set(values)) > 1 for values in date_groups.values()
        ) or _has_dense_fold_coverage(
            date_groups,
            support_by_date[local_date],
            local_date in dates_with_non_ambiguous_duplicates,
            local_tz,
        )

        single_fold_index = _canonical_single_fold_index(
            list(date_groups),
            support_by_date[local_date],
            local_tz,
        )
        for timestamp, values in sorted(date_groups.items()):
            candidates = _utc_candidates(timestamp, local_tz)
            unique_values = sorted(set(values))
            normalized_value = sum(unique_values) / len(unique_values)

            if fold_confirmed and (
                len(unique_values) > 1 or len(values) > 1
            ):
                normalized.extend(
                    (candidate, normalized_value)
                    for candidate in candidates
                )
                continue

            normalized.append(
                (candidates[single_fold_index], normalized_value)
            )

    return normalized


def _has_dense_fold_coverage(
    groups: dict[datetime, list[float]],
    support: list[datetime],
    has_non_ambiguous_duplicates: bool,
    local_tz: ZoneInfo,
) -> bool:
    """Return whether duplicates densely cover a complete repeated hour."""
    repeated = sorted(
        timestamp for timestamp, values in groups.items() if len(values) > 1
    )
    if (
        len(repeated) < 4
        or not support
        or has_non_ambiguous_duplicates
    ):
        return False

    candidates = _utc_candidates(repeated[0], local_tz)
    if len(candidates) < 2:
        return False
    fold_duration = candidates[1] - candidates[0]
    if repeated[-1] - repeated[0] < fold_duration - MAX_SAMPLE_GAP:
        return False

    return all(
        current - previous <= MAX_SAMPLE_GAP
        for previous, current in zip(repeated, repeated[1:], strict=False)
    )


def _canonical_single_fold_index(
    wall_times: list[datetime],
    support: list[datetime],
    local_tz: ZoneInfo,
) -> int:
    """Choose one fold deterministically when only one sample is evidenced."""
    if not wall_times:
        return 0

    candidate_count = len(_utc_candidates(wall_times[0], local_tz))

    def score(fold_index: int) -> tuple[int, int, float, int]:
        timeline = sorted(
            set(support)
            | {
                _utc_candidates(timestamp, local_tz)[fold_index]
                for timestamp in wall_times
            }
        )
        deltas = [
            (current - previous).total_seconds()
            for previous, current in zip(
                timeline,
                timeline[1:],
                strict=False,
            )
            if current > previous
        ]
        valid = [
            delta
            for delta in deltas
            if delta <= MAX_SAMPLE_GAP.total_seconds()
        ]
        return (
            -len(valid),
            len(deltas) - len(valid),
            sum(deltas),
            fold_index,
        )

    return min(range(candidate_count), key=score)


def _resolve_utc_timestamps(
    timestamps: list[datetime],
    local_tz: ZoneInfo,
) -> tuple[datetime, ...]:
    """Return UTC timestamps after canonical fold normalization."""
    candidate_rows = [
        _utc_candidates(timestamp, local_tz) for timestamp in timestamps
    ]
    return tuple(candidates[0] for candidates in candidate_rows)


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

    parsed_samples = _normalize_ambiguous_samples(parsed_samples, local_tz)
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
