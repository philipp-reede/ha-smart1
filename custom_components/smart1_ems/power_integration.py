"""Integrate smart1 power samples without bridging large data gaps."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import reduce
from math import gcd
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
    support_by_date: defaultdict[
        date, list[tuple[datetime, float]]
    ] = defaultdict(list)

    for timestamp, value in samples:
        candidates = _utc_candidates(timestamp, local_tz)
        if timestamp.tzinfo is None and len(candidates) > 1:
            ambiguous_values[timestamp].append(value)
            continue

        non_ambiguous_samples.append((timestamp, value))
        utc_timestamp = candidates[0]
        local_date = utc_timestamp.astimezone(local_tz).date()
        support_by_date[local_date].append((utc_timestamp, value))

    groups_by_date: defaultdict[
        date, dict[datetime, list[float]]
    ] = defaultdict(dict)
    for timestamp, values in ambiguous_values.items():
        groups_by_date[timestamp.date()][timestamp] = values

    normalized = list(non_ambiguous_samples)
    for local_date, date_groups in groups_by_date.items():
        support_samples = support_by_date[local_date]
        duplicate_factor = _systematic_duplicate_factor(support_samples)
        if duplicate_factor > 1:
            date_groups = {
                timestamp: _remove_systematic_duplicates(
                    values,
                    duplicate_factor,
                )
                for timestamp, values in date_groups.items()
            }
        support_timestamps = [
            timestamp for timestamp, _value in support_samples
        ]
        fold_confirmed = any(
            len(set(values)) > 1 for values in date_groups.values()
        ) or _has_dense_fold_coverage(
            date_groups,
            support_timestamps,
            local_tz,
        )

        single_fold_index = _canonical_single_fold_index(
            list(date_groups),
            support_timestamps,
            local_tz,
        )
        if fold_confirmed:
            normalized.extend(
                _resolve_confirmed_fold_samples(
                    date_groups,
                    support_samples,
                    local_tz,
                )
            )
            continue

        for timestamp, values in sorted(date_groups.items()):
            candidates = _utc_candidates(timestamp, local_tz)
            unique_values = sorted(set(values))
            normalized_value = sum(unique_values) / len(unique_values)
            normalized.append(
                (candidates[single_fold_index], normalized_value)
            )

    return normalized


def _systematic_duplicate_factor(
    support: list[tuple[datetime, float]],
) -> int:
    """Return a response-wide duplicate factor evidenced outside the fold."""
    counts = Counter(support)
    if len({timestamp for timestamp, _value in counts}) < 2:
        return 1

    factor = reduce(gcd, counts.values())
    return factor if factor > 1 else 1


def _remove_systematic_duplicates(
    values: list[float],
    duplicate_factor: int,
) -> list[float]:
    """Remove only multiplicity explained by a response-wide duplication."""
    counts = Counter(values)
    if any(count % duplicate_factor for count in counts.values()):
        return values
    return [
        value
        for value, count in sorted(counts.items())
        for _duplicate in range(count // duplicate_factor)
    ]


def _resolve_confirmed_fold_samples(
    groups: dict[datetime, list[float]],
    support: list[tuple[datetime, float]],
    local_tz: ZoneInfo,
) -> list[tuple[datetime, float]]:
    """Assign repeated-hour values to folds without averaging profiles.

    Naive timestamps do not identify the fold, so assignments are chosen by a
    deterministic minimum-variation path. This preserves every evidenced
    profile and uses samples immediately outside the repeated hour to resolve
    the otherwise unavoidable global fold ambiguity.
    """
    wall_times = sorted(groups)
    if not wall_times:
        return []

    state_options = [
        _fold_value_options(groups[timestamp]) for timestamp in wall_times
    ]
    support_values = _canonical_support_values(support)
    first_candidates = _utc_candidates(wall_times[0], local_tz)
    last_candidates = _utc_candidates(wall_times[-1], local_tz)
    before = _nearest_support_before(first_candidates[0], support_values)
    after = _nearest_support_after(last_candidates[1], support_values)

    best_result: tuple[
        tuple[int, float, tuple[tuple[float, float], ...]],
        tuple[tuple[float | None, float | None], ...],
    ] | None = None

    for initial_state in state_options[0]:
        paths: dict[
            tuple[float | None, float | None],
            tuple[int, float, tuple[tuple[float | None, float | None], ...]],
        ] = {
            initial_state: (
                *_support_score(before, initial_state[0]),
                (initial_state,),
            )
        }

        for index, options in enumerate(state_options[1:], start=1):
            gap = wall_times[index] - wall_times[index - 1]
            next_paths: dict[
                tuple[float | None, float | None],
                tuple[
                    int,
                    float,
                    tuple[tuple[float | None, float | None], ...],
                ],
            ] = {}
            for state in options:
                candidates = []
                for previous_state, (
                    connections,
                    variation,
                    path,
                ) in paths.items():
                    added_connections, added_variation = (
                        _adjacent_state_score(previous_state, state, gap)
                    )
                    candidates.append(
                        (
                            connections + added_connections,
                            variation + added_variation,
                            (*path, state),
                        )
                    )
                next_paths[state] = min(
                    candidates,
                    key=_fold_path_score,
                )
            paths = next_paths

        for connections, variation, path in paths.values():
            boundary_gap = first_candidates[1] - last_candidates[0]
            boundary_connections, boundary_variation = (
                _value_transition_score(
                    path[-1][0],
                    path[0][1],
                    boundary_gap,
                )
            )
            after_connections, after_variation = _support_score(
                after,
                path[-1][1],
            )
            final_connections = (
                connections
                + boundary_connections
                + after_connections
            )
            final_variation = (
                variation + boundary_variation + after_variation
            )
            score = (
                -final_connections,
                final_variation,
                _fold_path_tiebreak(path),
            )
            candidate = (score, path)
            if best_result is None or candidate[0] < best_result[0]:
                best_result = candidate

    assert best_result is not None
    resolved: list[tuple[datetime, float]] = []
    for timestamp, state in zip(
        wall_times,
        best_result[1],
        strict=True,
    ):
        for candidate, value in zip(
            _utc_candidates(timestamp, local_tz),
            state,
            strict=True,
        ):
            if value is not None:
                resolved.append((candidate, value))
    return resolved


def _fold_value_options(
    values: list[float],
) -> tuple[tuple[float | None, float | None], ...]:
    """Return deterministic fold assignments for one ambiguous wall time."""
    unique_values = sorted(set(values))
    if len(unique_values) == 1:
        value = unique_values[0]
        if len(values) > 1:
            return ((value, value),)
        return ((value, None), (None, value))

    # The portal should expose at most one value per fold. If it returns more,
    # retain the outer observed profiles instead of inventing an average.
    low = unique_values[0]
    high = unique_values[-1]
    return ((low, high), (high, low))


def _canonical_support_values(
    support: list[tuple[datetime, float]],
) -> dict[datetime, float]:
    """Return deterministic values for unambiguous boundary timestamps."""
    values_by_timestamp: defaultdict[datetime, set[float]] = defaultdict(set)
    for timestamp, value in support:
        values_by_timestamp[timestamp].add(value)
    return {
        timestamp: sum(values) / len(values)
        for timestamp, values in values_by_timestamp.items()
    }


def _nearest_support_before(
    timestamp: datetime,
    support: dict[datetime, float],
) -> tuple[timedelta, float] | None:
    """Return the nearest usable support sample before a timestamp."""
    candidates = [
        (timestamp - support_time, value)
        for support_time, value in support.items()
        if support_time < timestamp
        and timestamp - support_time <= MAX_SAMPLE_GAP
    ]
    return min(candidates, default=None, key=lambda item: item[0])


def _nearest_support_after(
    timestamp: datetime,
    support: dict[datetime, float],
) -> tuple[timedelta, float] | None:
    """Return the nearest usable support sample after a timestamp."""
    candidates = [
        (support_time - timestamp, value)
        for support_time, value in support.items()
        if support_time > timestamp
        and support_time - timestamp <= MAX_SAMPLE_GAP
    ]
    return min(candidates, default=None, key=lambda item: item[0])


def _support_score(
    support: tuple[timedelta, float] | None,
    value: float | None,
) -> tuple[int, float]:
    """Return continuity score for one optional boundary sample."""
    if support is None or value is None:
        return (0, 0.0)
    return (1, abs(support[1] - value))


def _adjacent_state_score(
    previous: tuple[float | None, float | None],
    current: tuple[float | None, float | None],
    gap: timedelta,
) -> tuple[int, float]:
    """Return continuity score for adjacent wall times in both folds."""
    connections = 0
    variation = 0.0
    for previous_value, current_value in zip(previous, current, strict=True):
        added_connections, added_variation = _value_transition_score(
            previous_value,
            current_value,
            gap,
        )
        connections += added_connections
        variation += added_variation
    return connections, variation


def _value_transition_score(
    previous: float | None,
    current: float | None,
    gap: timedelta,
) -> tuple[int, float]:
    """Return continuity score for one possible power transition."""
    if (
        previous is None
        or current is None
        or gap <= timedelta(0)
        or gap > MAX_SAMPLE_GAP
    ):
        return (0, 0.0)
    return (1, abs(current - previous))


def _fold_path_score(
    item: tuple[
        int,
        float,
        tuple[tuple[float | None, float | None], ...],
    ],
) -> tuple[int, float, tuple[tuple[float, float], ...]]:
    """Return minimization key for an incomplete fold path."""
    connections, variation, path = item
    return (-connections, variation, _fold_path_tiebreak(path))


def _fold_path_tiebreak(
    path: tuple[tuple[float | None, float | None], ...],
) -> tuple[tuple[float, float], ...]:
    """Return a stable ordering for otherwise indistinguishable paths."""
    return tuple(
        tuple(float("inf") if value is None else value for value in state)
        for state in path
    )


def _has_dense_fold_coverage(
    groups: dict[datetime, list[float]],
    support: list[datetime],
    local_tz: ZoneInfo,
) -> bool:
    """Return whether duplicates densely cover a complete repeated hour."""
    repeated = sorted(
        timestamp for timestamp, values in groups.items() if len(values) > 1
    )
    if len(repeated) < 4 or not support:
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
