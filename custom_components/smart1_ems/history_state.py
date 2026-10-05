"""Persistent schema, data-presence and coverage state for history."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from copy import deepcopy
from datetime import date, datetime, timezone
from hashlib import sha256
import logging
import math
from typing import Any, NamedTuple
from uuid import uuid4
from weakref import WeakValueDictionary


STATISTICS_NAMESPACE_KEY = "statistics_namespace"
HISTORY_SCHEMA_VERSIONS_KEY = "history_schema_versions"
HISTORY_DATA_PRESENCE_KEY = "history_data_presence"
HISTORY_COVERAGE_KEY = "history_coverage"
HISTORY_EMPTY_DAYS_KEY = "history_empty_days"
HISTORY_EMPTY_RETRY_CURSORS_KEY = "history_empty_retry_cursors"
HISTORY_EMPTY_RETRY_RUNS_KEY = "history_empty_retry_runs"
HISTORY_SOURCE_TIME_ZONES_KEY = "history_source_time_zones"
HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY = (
    "history_pending_time_zone_migrations"
)
HISTORY_DEFERRED_TIME_ZONE_PARTITIONS_KEY = (
    "history_deferred_time_zone_partitions"
)
LEGACY_HISTORY_REBUILD_KEY = "legacy_history_rebuild"
HISTORY_STORE_LOCKS_KEY = "smart1_ems_history_store_locks"

_LOGGER = logging.getLogger(__name__)


def history_store_lock(hass: Any, store_key: str | None) -> asyncio.Lock:
    """Return the process-wide lock for one durable history-store key.

    Config-entry reloads create a new ``Smart1HistoryState`` and a new
    ``Store`` object while callbacks from the old runtime may still be
    finishing.  The lock therefore belongs to the Home Assistant process and
    store key, not to either state instance. The registry retains it weakly;
    active states, holders and waiters keep the common fence alive while an
    unused entry disappears automatically. Lightweight callers without a
    normal ``hass.data`` mapping retain an instance-local fallback lock.
    """
    hass_data = getattr(hass, "data", None)
    if not store_key or not isinstance(hass_data, dict):
        return asyncio.Lock()
    locks = hass_data.get(HISTORY_STORE_LOCKS_KEY)
    if isinstance(locks, dict):
        # Upgrade the original strong-reference registry in place. Active
        # states, holders and waiters retain their own strong references, so
        # an entry can disappear only after it can no longer split the fence.
        weak_locks: WeakValueDictionary[str, asyncio.Lock] = (
            WeakValueDictionary()
        )
        for key, lock in locks.items():
            if isinstance(key, str) and isinstance(lock, asyncio.Lock):
                weak_locks[key] = lock
        hass_data[HISTORY_STORE_LOCKS_KEY] = weak_locks
        locks = weak_locks
    elif locks is None:
        locks = WeakValueDictionary()
        hass_data[HISTORY_STORE_LOCKS_KEY] = locks
    if not isinstance(locks, WeakValueDictionary):
        # Do not replace unexpected third-party data at the private key.
        return asyncio.Lock()
    return locks.setdefault(store_key, asyncio.Lock())


async def async_drain_store_operation(operation: Awaitable[Any]) -> Any:
    """Finish storage I/O before propagating caller cancellation.

    Home Assistant performs storage writes and removals in an executor.
    Cancelling the awaiting task does not stop that worker thread. Callers use
    this helper while holding the process-wide store-key lock, so an old
    runtime cannot release the lock and later overwrite or remove state that a
    new runtime has already persisted.

    A storage exception takes precedence over delayed cancellation. The caller
    can then run its normal rollback instead of retaining an unpersisted
    in-memory journal generation.
    """
    inner = asyncio.ensure_future(operation)
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(inner)
            break
        except asyncio.CancelledError as err:
            if inner.cancelled():
                raise
            cancellation = cancellation or err
            if not inner.done():
                continue
            # Retrieve a completed operation's exception before re-raising the
            # caller cancellation. This deliberately lets Store failures reach
            # the surrounding state rollback.
            result = inner.result()
            break
    if cancellation is not None:
        raise cancellation
    return result

# Versions 7/8 perform one journal-protected whole-statistic canonicalization
# after the pre-journal v0.7.3 time-zone migration. Daily remains one version
# above hourly so the active storage mode stays unambiguous. Historical
# versions overlap by mode: v2 was normally daily, v3 could be either daily or
# hourly, v4/v5 were hourly, and v6 was daily. Some v0.7.2 mode switches could
# additionally leave hourly-shaped rows behind a v2 marker. Importers therefore
# inspect legacy row structure before adopting old rows.
PV_HISTORY_SCHEMA_VERSION = 7
PV_DAILY_HISTORY_SCHEMA_VERSION = 8
UNJOURNALED_PV_HISTORY_SCHEMA_VERSIONS = frozenset({5, 6})
# Version 7 performs one non-destructive supported-window refresh for v6
# histories that could have committed an incomplete cross-midnight resume or
# an ambiguous fractional-offset time-zone remap. Versions 4/5 predate the
# durable canonicalization journal and still require the separate whole-ID
# replacement path; v6 must not be cleared merely for this schema upgrade.
DERIVED_HISTORY_SCHEMA_VERSION = 7
UNJOURNALED_DERIVED_HISTORY_SCHEMA_VERSIONS = frozenset({4, 5})
# Version 3 was the fractional-offset-only boundary-fragment repair marker.
# Keep it named for upgrade-state compatibility; version 7 supersedes it in
# every time zone.
DERIVED_FRACTIONAL_OFFSET_SCHEMA_VERSION = 3


class PendingTimeZoneMigration(NamedTuple):
    """Persisted identity of one in-flight source-time-zone migration."""

    source_time_zone: str
    target_time_zone: str
    generation: str
    fallback_daily_energy: tuple[tuple[date, float], ...] | None = None
    clear_rebuild_baseline_sum: float | None = None
    replacement_hourly_energy: (
        tuple[tuple[datetime, float], ...] | None
    ) = None
    replacement_hourly_energy_invalid: bool = False

    def fallback_energy_by_date(self) -> dict[date, float]:
        """Return target-date totals retained across an ambiguous restart."""
        return dict(self.fallback_daily_energy or ())

    @property
    def has_complete_fallback(self) -> bool:
        """Return whether zero as well as positive target days are known."""
        return self.fallback_daily_energy is not None

    def replacement_energy_by_start(self) -> dict[datetime, float]:
        """Return the exact UTC replacement profile, when journaled."""
        return dict(self.replacement_hourly_energy or ())

    @property
    def has_complete_replacement(self) -> bool:
        """Return whether an exact (possibly empty) replacement is known."""
        return self.replacement_hourly_energy is not None

    @property
    def has_invalid_replacement(self) -> bool:
        """Return whether an exact field was present but malformed."""
        return self.replacement_hourly_energy_invalid


class DeferredTimeZonePartition(NamedTuple):
    """Persist one indivisible source-day partition awaiting new evidence."""

    source_time_zone: str
    target_time_zone: str
    schema_version: int
    generation: str
    partition_dates: tuple[date, ...]
    next_probe_on: date
    probe_cursor: int = 0


def statistics_namespace_for_device(device_id: str) -> str:
    """Return a stable, non-identifying namespace for one installation."""
    normalized = device_id.strip()
    if not normalized:
        raise ValueError("A device ID is required for the statistics namespace")
    return sha256(normalized.encode()).hexdigest()[:10]


def scoped_statistic_id(statistic_id: str, namespace: str) -> str:
    """Scope a statistic ID while preserving legacy IDs for their owner."""
    return statistic_id if not namespace else f"{statistic_id}_{namespace}"


def source_time_zone_requires_rebuild(
    history_state: Any,
    statistic_id: str,
    source_time_zone: str,
) -> bool:
    """Return whether stored history was mapped in a known different zone.

    The defensive attribute lookup keeps lightweight third-party and test
    state implementations compatible. An absent fingerprint is deliberately
    not treated as a proven mapping change: it needs a conservative audit that
    preserves successful-empty days rather than destructive tombstones.
    """
    effective_getter = getattr(
        history_state,
        "effective_source_time_zone",
        None,
    )
    if callable(effective_getter):
        stored = effective_getter(statistic_id, source_time_zone)
    else:
        getter = getattr(history_state, "source_time_zone", None)
        stored = getter(statistic_id) if callable(getter) else None
    return bool(stored is not None and stored != source_time_zone)


def source_time_zone_requires_audit(
    history_state: Any,
    statistic_id: str,
    source_time_zone: str,
) -> bool:
    """Return whether a fingerprint is missing or differs from the source."""
    pending_getter = getattr(
        history_state,
        "pending_time_zone_migration",
        None,
    )
    if callable(pending_getter) and pending_getter(statistic_id) is not None:
        return True
    getter = getattr(history_state, "source_time_zone", None)
    if callable(getter):
        stored = getter(statistic_id)
    else:
        # Lightweight/test history-state implementations may expose only the
        # effective accessor. It is still sufficient when no pending journal
        # entry exists (handled above).
        effective_getter = getattr(
            history_state,
            "effective_source_time_zone",
            None,
        )
        stored = (
            effective_getter(statistic_id, source_time_zone)
            if callable(effective_getter)
            else None
        )
    return stored != source_time_zone


def recorded_source_time_zone(
    history_state: Any,
    statistic_id: str,
    default: str,
) -> str:
    """Return the stored row-mapping zone or a safe current-zone fallback."""
    effective_getter = getattr(
        history_state,
        "effective_source_time_zone",
        None,
    )
    if callable(effective_getter):
        return effective_getter(statistic_id, default)
    getter = getattr(history_state, "source_time_zone", None)
    stored = getter(statistic_id) if callable(getter) else None
    return stored if isinstance(stored, str) and stored else default


class Smart1HistoryState:
    """Persist completed history schemas in the owning config entry."""

    def __init__(
        self,
        hass: Any,
        entry: Any,
        *,
        durable_store: Any | None = None,
        durable_store_key: str | None = None,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._active = True
        self._durable_store = durable_store
        self._durable_store_lock = history_store_lock(
            hass,
            durable_store_key,
        )
        raw_versions = entry.data.get(HISTORY_SCHEMA_VERSIONS_KEY, {})
        self._versions = {
            str(statistic_id): int(version)
            for statistic_id, version in raw_versions.items()
            if isinstance(statistic_id, str)
            and isinstance(version, int)
            and not isinstance(version, bool)
        } if isinstance(raw_versions, dict) else {}
        raw_data_presence = entry.data.get(HISTORY_DATA_PRESENCE_KEY, {})
        self._data_presence = {
            str(statistic_id): {
                int(schema_version): has_data
                for schema_version, has_data in schema_versions.items()
                if str(schema_version).isdigit()
                and isinstance(has_data, bool)
            }
            for statistic_id, schema_versions in raw_data_presence.items()
            if isinstance(statistic_id, str)
            and isinstance(schema_versions, dict)
        } if isinstance(raw_data_presence, dict) else {}
        raw_coverage = entry.data.get(HISTORY_COVERAGE_KEY, {})
        self._coverage = {
            str(statistic_id): {
                int(schema_version): checked_through
                for schema_version, raw_checked_through in schema_versions.items()
                if str(schema_version).isdigit()
                and isinstance(raw_checked_through, str)
                and (
                    checked_through := self._parse_date(raw_checked_through)
                )
                is not None
            }
            for statistic_id, schema_versions in raw_coverage.items()
            if isinstance(statistic_id, str)
            and isinstance(schema_versions, dict)
        } if isinstance(raw_coverage, dict) else {}
        raw_empty_days = entry.data.get(HISTORY_EMPTY_DAYS_KEY, {})
        self._empty_days: dict[str, dict[int, set[date]]] = {}
        if isinstance(raw_empty_days, dict):
            for statistic_id, schema_versions in raw_empty_days.items():
                if not isinstance(statistic_id, str) or not isinstance(
                    schema_versions,
                    dict,
                ):
                    continue
                parsed_versions: dict[int, set[date]] = {}
                for schema_version, raw_dates in schema_versions.items():
                    if not str(schema_version).isdigit() or not isinstance(
                        raw_dates,
                        list,
                    ):
                        continue
                    parsed_dates = {
                        parsed_date
                        for raw_date in raw_dates
                        if isinstance(raw_date, str)
                        and (parsed_date := self._parse_date(raw_date))
                        is not None
                    }
                    if parsed_dates:
                        parsed_versions[int(schema_version)] = parsed_dates
                if parsed_versions:
                    self._empty_days[statistic_id] = parsed_versions
        raw_retry_cursors = entry.data.get(
            HISTORY_EMPTY_RETRY_CURSORS_KEY,
            {},
        )
        self._empty_retry_cursors: dict[str, dict[int, date]] = {}
        if isinstance(raw_retry_cursors, dict):
            for statistic_id, schema_versions in raw_retry_cursors.items():
                if not isinstance(statistic_id, str) or not isinstance(
                    schema_versions,
                    dict,
                ):
                    continue
                parsed_versions = {
                    int(schema_version): parsed_date
                    for schema_version, raw_date in schema_versions.items()
                    if str(schema_version).isdigit()
                    and isinstance(raw_date, str)
                    and (parsed_date := self._parse_date(raw_date)) is not None
                }
                if parsed_versions:
                    self._empty_retry_cursors[statistic_id] = parsed_versions
        raw_retry_runs = entry.data.get(HISTORY_EMPTY_RETRY_RUNS_KEY, {})
        self._empty_retry_runs: dict[str, dict[int, date]] = {}
        if isinstance(raw_retry_runs, dict):
            for statistic_id, schema_versions in raw_retry_runs.items():
                if not isinstance(statistic_id, str) or not isinstance(
                    schema_versions,
                    dict,
                ):
                    continue
                parsed_versions = {
                    int(schema_version): parsed_date
                    for schema_version, raw_date in schema_versions.items()
                    if str(schema_version).isdigit()
                    and isinstance(raw_date, str)
                    and (parsed_date := self._parse_date(raw_date)) is not None
                }
                if parsed_versions:
                    self._empty_retry_runs[statistic_id] = parsed_versions
        raw_source_time_zones = entry.data.get(
            HISTORY_SOURCE_TIME_ZONES_KEY,
            {},
        )
        self._source_time_zones = {
            str(statistic_id): time_zone
            for statistic_id, time_zone in raw_source_time_zones.items()
            if isinstance(statistic_id, str)
            and isinstance(time_zone, str)
            and time_zone
        } if isinstance(raw_source_time_zones, dict) else {}
        self._pending_time_zone_migrations = (
            self._parse_pending_time_zone_migrations(
                entry.data.get(
                    HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY,
                    {},
                )
            )
        )
        self._deferred_time_zone_partitions = (
            self._parse_deferred_time_zone_partitions(
                entry.data.get(
                    HISTORY_DEFERRED_TIME_ZONE_PARTITIONS_KEY,
                    {},
                )
            )
        )

    @staticmethod
    def _normalize_fallback_daily_energy(
        raw_energy: Any,
    ) -> tuple[tuple[date, float], ...] | None:
        """Return all-or-nothing non-negative target-date fallback totals."""
        if not isinstance(raw_energy, Mapping):
            return None
        normalized: dict[date, float] = {}
        for raw_date, raw_value in raw_energy.items():
            if isinstance(raw_date, date) and not isinstance(
                raw_date,
                datetime,
            ):
                target_date = raw_date
            elif isinstance(raw_date, str):
                target_date = Smart1HistoryState._parse_date(raw_date)
            else:
                target_date = None
            if isinstance(raw_value, bool):
                return None
            try:
                energy_kwh = float(raw_value)
            except (TypeError, ValueError):
                return None
            if (
                target_date is None
                or not math.isfinite(energy_kwh)
                or energy_kwh < 0.0
                or target_date in normalized
            ):
                return None
            normalized[target_date] = energy_kwh
        return tuple(sorted(normalized.items()))

    @staticmethod
    def _normalize_replacement_hourly_energy(
        raw_energy: Any,
    ) -> tuple[tuple[datetime, float], ...] | None:
        """Return an all-or-nothing exact UTC replacement profile.

        The journal is the only recovery source after a whole-statistic clear.
        Silently dropping one malformed row would therefore turn a complete
        replacement into destructive partial data.  Reject the complete field
        instead so identity-layout recovery can fail closed.
        """
        if not isinstance(raw_energy, (list, tuple)):
            return None
        normalized: dict[datetime, float] = {}
        for raw_row in raw_energy:
            if isinstance(raw_row, Mapping):
                raw_start = raw_row.get("start")
                raw_state = raw_row.get("state")
            elif isinstance(raw_row, (list, tuple)) and len(raw_row) == 2:
                raw_start, raw_state = raw_row
            else:
                return None

            if isinstance(raw_start, datetime):
                start = raw_start
            elif isinstance(raw_start, str):
                try:
                    start = datetime.fromisoformat(
                        raw_start.replace("Z", "+00:00")
                    )
                except ValueError:
                    return None
            else:
                return None
            if start.tzinfo is None or start.utcoffset() is None:
                return None
            start = start.astimezone(timezone.utc)
            if start.minute or start.second or start.microsecond:
                return None
            if isinstance(raw_state, bool):
                return None
            try:
                state = float(raw_state)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(state) or state < 0.0 or start in normalized:
                return None
            normalized[start] = state
        return tuple(sorted(normalized.items()))

    @staticmethod
    def _parse_pending_time_zone_migrations(
        raw_pending_migrations: Any,
    ) -> dict[str, PendingTimeZoneMigration]:
        """Return valid persisted migration journal entries."""
        parsed: dict[str, PendingTimeZoneMigration] = {}
        if not isinstance(raw_pending_migrations, dict):
            return parsed
        for statistic_id, raw_migration in raw_pending_migrations.items():
            if not isinstance(statistic_id, str) or not isinstance(
                raw_migration,
                dict,
            ):
                continue
            source_time_zone = raw_migration.get("source_time_zone")
            target_time_zone = raw_migration.get("target_time_zone")
            generation = raw_migration.get("generation")
            if not (
                isinstance(source_time_zone, str)
                and source_time_zone
                and isinstance(target_time_zone, str)
                and target_time_zone
                and isinstance(generation, str)
                and generation
            ):
                continue
            fallback_daily_energy = (
                Smart1HistoryState._normalize_fallback_daily_energy(
                    raw_migration.get("fallback_daily_energy", {})
                )
                if "fallback_daily_energy" in raw_migration
                else None
            )
            replacement_field_present = (
                "replacement_hourly_energy" in raw_migration
            )
            replacement_hourly_energy = (
                Smart1HistoryState._normalize_replacement_hourly_energy(
                    raw_migration.get("replacement_hourly_energy")
                )
                if replacement_field_present
                else None
            )
            raw_baseline = raw_migration.get("clear_rebuild_baseline_sum")
            try:
                clear_rebuild_baseline_sum = (
                    float(raw_baseline)
                    if raw_baseline is not None
                    and math.isfinite(float(raw_baseline))
                    else None
                )
            except (TypeError, ValueError):
                clear_rebuild_baseline_sum = None
            parsed[statistic_id] = PendingTimeZoneMigration(
                source_time_zone=source_time_zone,
                target_time_zone=target_time_zone,
                generation=generation,
                fallback_daily_energy=fallback_daily_energy,
                clear_rebuild_baseline_sum=clear_rebuild_baseline_sum,
                replacement_hourly_energy=replacement_hourly_energy,
                replacement_hourly_energy_invalid=(
                    replacement_field_present
                    and replacement_hourly_energy is None
                ),
            )
        return parsed

    @staticmethod
    def _parse_deferred_time_zone_partitions(
        raw_partitions: Any,
    ) -> dict[str, DeferredTimeZonePartition]:
        """Return valid persisted fail-closed partition evidence."""
        parsed: dict[str, DeferredTimeZonePartition] = {}
        if not isinstance(raw_partitions, dict):
            return parsed
        for statistic_id, raw_partition in raw_partitions.items():
            if not isinstance(statistic_id, str) or not isinstance(
                raw_partition,
                dict,
            ):
                continue
            source_time_zone = raw_partition.get("source_time_zone")
            target_time_zone = raw_partition.get("target_time_zone")
            schema_version = raw_partition.get("schema_version")
            generation = raw_partition.get("generation")
            raw_dates = raw_partition.get("partition_dates")
            next_probe_on = raw_partition.get("next_probe_on")
            probe_cursor = raw_partition.get("probe_cursor", 0)
            if not (
                isinstance(source_time_zone, str)
                and source_time_zone
                and isinstance(target_time_zone, str)
                and target_time_zone
                and isinstance(schema_version, int)
                and not isinstance(schema_version, bool)
                and schema_version > 0
                and isinstance(generation, str)
                and generation
                and isinstance(raw_dates, list)
                and raw_dates
                and isinstance(next_probe_on, str)
                and isinstance(probe_cursor, int)
                and not isinstance(probe_cursor, bool)
                and probe_cursor >= 0
            ):
                continue
            partition_dates = {
                parsed_date
                for raw_date in raw_dates
                if isinstance(raw_date, str)
                and (parsed_date := Smart1HistoryState._parse_date(raw_date))
                is not None
            }
            parsed_next_probe = Smart1HistoryState._parse_date(next_probe_on)
            if not partition_dates or parsed_next_probe is None:
                continue
            parsed[statistic_id] = DeferredTimeZonePartition(
                source_time_zone=source_time_zone,
                target_time_zone=target_time_zone,
                schema_version=schema_version,
                generation=generation,
                partition_dates=tuple(sorted(partition_dates)),
                next_probe_on=parsed_next_probe,
                probe_cursor=probe_cursor % len(partition_dates),
            )
        return parsed

    def _serialized_deferred_time_zone_partitions(
        self,
    ) -> dict[str, dict[str, Any]]:
        """Return durable fail-closed partition evidence."""
        return {
            statistic_id: {
                "source_time_zone": partition.source_time_zone,
                "target_time_zone": partition.target_time_zone,
                "schema_version": partition.schema_version,
                "generation": partition.generation,
                "partition_dates": [
                    partition_date.isoformat()
                    for partition_date in partition.partition_dates
                ],
                "next_probe_on": partition.next_probe_on.isoformat(),
                "probe_cursor": partition.probe_cursor,
            }
            for statistic_id, partition in (
                self._deferred_time_zone_partitions.items()
            )
        }

    def _durable_time_zone_state(self) -> dict[str, Any]:
        """Return the crash-critical subset written without a delay."""
        return {
            HISTORY_SOURCE_TIME_ZONES_KEY: dict(self._source_time_zones),
            HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY: {
                statistic_id: {
                    "source_time_zone": migration.source_time_zone,
                    "target_time_zone": migration.target_time_zone,
                    "generation": migration.generation,
                    **(
                        {
                            "fallback_daily_energy": {
                                target_date.isoformat(): energy_kwh
                                for target_date, energy_kwh in (
                                    migration.fallback_daily_energy
                                )
                            }
                        }
                        if migration.fallback_daily_energy is not None
                        else {}
                    ),
                    **(
                        {
                            "clear_rebuild_baseline_sum": (
                                migration.clear_rebuild_baseline_sum
                            )
                        }
                        if migration.clear_rebuild_baseline_sum is not None
                        else {}
                    ),
                    **(
                        {
                            "replacement_hourly_energy": [
                                {
                                    "start": start.isoformat(),
                                    "state": energy_kwh,
                                }
                                for start, energy_kwh in (
                                    migration.replacement_hourly_energy
                                )
                            ]
                        }
                        if migration.replacement_hourly_energy is not None
                        else {}
                    ),
                    **(
                        {"replacement_hourly_energy": None}
                        if migration.replacement_hourly_energy_invalid
                        else {}
                    ),
                }
                for statistic_id, migration in (
                    self._pending_time_zone_migrations.items()
                )
            },
            HISTORY_DEFERRED_TIME_ZONE_PARTITIONS_KEY: (
                self._serialized_deferred_time_zone_partitions()
            ),
        }

    async def _async_save_durable_time_zone_state(self) -> None:
        """Write and read back the crash-critical migration journal."""
        if self._durable_store is None:
            return
        hass_state = getattr(self._hass, "state", None)
        hass_is_stopping = bool(
            getattr(self._hass, "is_stopping", False)
        )
        raw_state = getattr(hass_state, "value", hass_state)
        normalized_state = (
            raw_state.casefold() if isinstance(raw_state, str) else ""
        )
        if hass_is_stopping or normalized_state in {
            "stopping",
            "final_write",
            "stopped",
        }:
            # Store defers writes to Home Assistant's final-write event while
            # stopping. Its in-memory pending payload would make an immediate
            # ``async_load`` look successful even though nothing has reached
            # disk yet, so no Recorder mutation may rely on it.
            raise RuntimeError(
                "Cannot persist smart1 history journal while stopping"
            )
        expected = self._durable_time_zone_state()

        async def _async_save_and_verify() -> None:
            await self._durable_store.async_save(expected)
            # Home Assistant's Store logs low-level write failures instead of
            # propagating them. Reading the file back is therefore required
            # before an irreversible Recorder operation may rely on this
            # journal.
            persisted = await self._durable_store.async_load()
            if persisted != expected:
                raise OSError(
                    "smart1 history migration journal was not persisted"
                )

        await async_drain_store_operation(_async_save_and_verify())

    async def async_initialize(self) -> None:
        """Load the durable migration journal before Recorder work starts."""
        if self._durable_store is None:
            return
        async with self._durable_store_lock:
            if not self._active:
                raise RuntimeError("Cannot initialize an inactive history state")
            stored = await self._durable_store.async_load()
            if stored is None:
                # Seed the store from config-entry data for upgrades. No
                # Recorder mutation can start until this awaited write ends.
                await self._async_save_durable_time_zone_state()
                return
            if not isinstance(stored, dict):
                raise ValueError("Invalid smart1 history migration store")

            raw_source_time_zones = stored.get(
                HISTORY_SOURCE_TIME_ZONES_KEY,
                {},
            )
            durable_source_time_zones = {
                statistic_id: time_zone
                for statistic_id, time_zone in raw_source_time_zones.items()
                if isinstance(statistic_id, str)
                and isinstance(time_zone, str)
                and time_zone
            } if isinstance(raw_source_time_zones, dict) else {}
            durable_pending = self._parse_pending_time_zone_migrations(
                stored.get(HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY, {})
            )
            durable_deferred = self._parse_deferred_time_zone_partitions(
                stored.get(
                    HISTORY_DEFERRED_TIME_ZONE_PARTITIONS_KEY,
                    {},
                )
            )

            # Once the dedicated store exists it is authoritative for both
            # source fingerprints and pending work. In particular, an absent
            # source row must win over a config-entry copy whose delayed save
            # survived after orphan cleanup, just as an empty pending map must
            # win after a completed migration.
            changed = self._source_time_zones != durable_source_time_zones
            if changed:
                self._source_time_zones = durable_source_time_zones
            if self._pending_time_zone_migrations != durable_pending:
                self._pending_time_zone_migrations = durable_pending
                changed = True
            if self._deferred_time_zone_partitions != durable_deferred:
                self._deferred_time_zone_partitions = durable_deferred
                changed = True
            if changed:
                self._persist()

    @staticmethod
    def _parse_date(value: str) -> date | None:
        """Return a stored ISO date, ignoring malformed entry data."""
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    def _persist(self) -> None:
        """Persist all history state maps in one config-entry update."""
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                HISTORY_SCHEMA_VERSIONS_KEY: dict(self._versions),
                HISTORY_DATA_PRESENCE_KEY: {
                    stored_statistic_id: {
                        str(stored_schema_version): stored_has_data
                        for stored_schema_version, stored_has_data in sorted(
                            schema_versions.items()
                        )
                    }
                    for stored_statistic_id, schema_versions in (
                        self._data_presence.items()
                    )
                },
                HISTORY_COVERAGE_KEY: {
                    stored_statistic_id: {
                        str(stored_schema_version): checked_through.isoformat()
                        for stored_schema_version, checked_through in sorted(
                            schema_versions.items()
                        )
                    }
                    for stored_statistic_id, schema_versions in (
                        self._coverage.items()
                    )
                },
                HISTORY_EMPTY_DAYS_KEY: {
                    stored_statistic_id: {
                        str(stored_schema_version): [
                            empty_day.isoformat()
                            for empty_day in sorted(empty_days)
                        ]
                        for stored_schema_version, empty_days in sorted(
                            schema_versions.items()
                        )
                        if empty_days
                    }
                    for stored_statistic_id, schema_versions in (
                        self._empty_days.items()
                    )
                    if any(schema_versions.values())
                },
                HISTORY_EMPTY_RETRY_CURSORS_KEY: {
                    stored_statistic_id: {
                        str(stored_schema_version): retry_cursor.isoformat()
                        for stored_schema_version, retry_cursor in sorted(
                            schema_versions.items()
                        )
                    }
                    for stored_statistic_id, schema_versions in (
                        self._empty_retry_cursors.items()
                    )
                    if schema_versions
                },
                HISTORY_EMPTY_RETRY_RUNS_KEY: {
                    stored_statistic_id: {
                        str(stored_schema_version): retry_run.isoformat()
                        for stored_schema_version, retry_run in sorted(
                            schema_versions.items()
                        )
                    }
                    for stored_statistic_id, schema_versions in (
                        self._empty_retry_runs.items()
                    )
                    if schema_versions
                },
                HISTORY_SOURCE_TIME_ZONES_KEY: dict(
                    self._source_time_zones
                ),
                HISTORY_PENDING_TIME_ZONE_MIGRATIONS_KEY: {
                    stored_statistic_id: {
                        "source_time_zone": migration.source_time_zone,
                        "target_time_zone": migration.target_time_zone,
                        "generation": migration.generation,
                        **(
                            {
                                "fallback_daily_energy": {
                                    target_date.isoformat(): energy_kwh
                                    for target_date, energy_kwh in (
                                        migration.fallback_daily_energy
                                    )
                                }
                            }
                            if migration.fallback_daily_energy is not None
                            else {}
                        ),
                        **(
                            {
                                "clear_rebuild_baseline_sum": (
                                    migration.clear_rebuild_baseline_sum
                                )
                            }
                            if migration.clear_rebuild_baseline_sum
                            is not None
                            else {}
                        ),
                        **(
                            {
                                "replacement_hourly_energy": [
                                    {
                                        "start": start.isoformat(),
                                        "state": energy_kwh,
                                    }
                                    for start, energy_kwh in (
                                        migration.replacement_hourly_energy
                                    )
                                ]
                            }
                            if migration.replacement_hourly_energy is not None
                            else {}
                        ),
                        **(
                            {"replacement_hourly_energy": None}
                            if migration.replacement_hourly_energy_invalid
                            else {}
                        ),
                    }
                    for stored_statistic_id, migration in (
                        self._pending_time_zone_migrations.items()
                    )
                },
                HISTORY_DEFERRED_TIME_ZONE_PARTITIONS_KEY: (
                    self._serialized_deferred_time_zone_partitions()
                ),
            },
        )

    async def deactivate(self) -> None:
        """Deactivate this runtime and drain every durable store operation."""
        # Set the flag before waiting. Any operation already queued ahead of
        # this waiter must recheck it after acquiring the shared key lock and
        # return without writing. Once the empty critical section completes,
        # no write from this runtime can outlive the unload.
        self._active = False
        async with self._durable_store_lock:
            pass

    def current_schema_version(self, statistic_id: str) -> int:
        """Return the currently persisted schema version for a statistic."""
        return self._versions.get(statistic_id, 0)

    def is_complete(self, statistic_id: str, schema_version: int) -> bool:
        """Return whether a full import completed for this schema."""
        return self._versions.get(statistic_id, 0) >= schema_version

    def is_current_schema(
        self,
        statistic_id: str,
        schema_version: int,
    ) -> bool:
        """Return whether this is the statistic's active storage schema."""
        return self._versions.get(statistic_id, 0) == schema_version

    def data_presence(
        self,
        statistic_id: str,
        schema_version: int,
    ) -> bool | None:
        """Return whether a completed backfill produced recorder rows."""
        if not self.is_complete(statistic_id, schema_version):
            return None
        return self._data_presence.get(statistic_id, {}).get(schema_version)

    def checked_through(
        self,
        statistic_id: str,
        schema_version: int,
    ) -> date | None:
        """Return the last day fully queried for the active schema."""
        if not self.is_current_schema(statistic_id, schema_version):
            return None
        return self._coverage.get(statistic_id, {}).get(schema_version)

    def source_time_zone(self, statistic_id: str) -> str | None:
        """Return the time zone used to map one statistic's source rows."""
        return self._source_time_zones.get(statistic_id)

    def pending_time_zone_migration(
        self,
        statistic_id: str,
    ) -> PendingTimeZoneMigration | None:
        """Return the persisted in-flight time-zone migration, if any."""
        return self._pending_time_zone_migrations.get(statistic_id)

    def deferred_time_zone_partition(
        self,
        statistic_id: str,
    ) -> DeferredTimeZonePartition | None:
        """Return fail-closed evidence for an indivisible date partition."""
        return self._deferred_time_zone_partitions.get(statistic_id)

    def defer_time_zone_partition(
        self,
        statistic_id: str,
        source_time_zone: str,
        target_time_zone: str,
        schema_version: int,
        *,
        partition_dates: set[date],
        next_probe_on: date,
        probe_cursor: int = 0,
    ) -> str:
        """Persist a bounded-retry deferral for an indivisible UTC bucket."""
        if not self._active:
            raise RuntimeError("Cannot mutate an inactive history state")
        if not isinstance(statistic_id, str) or not statistic_id:
            raise ValueError("A statistic ID is required")
        if not (
            isinstance(source_time_zone, str)
            and source_time_zone
            and isinstance(target_time_zone, str)
            and target_time_zone
            and isinstance(schema_version, int)
            and not isinstance(schema_version, bool)
            and schema_version > 0
            and isinstance(next_probe_on, date)
            and not isinstance(next_probe_on, datetime)
            and isinstance(probe_cursor, int)
            and not isinstance(probe_cursor, bool)
            and probe_cursor >= 0
        ):
            raise ValueError("Invalid time-zone partition deferral")
        normalized_dates = {
            partition_date
            for partition_date in partition_dates
            if isinstance(partition_date, date)
            and not isinstance(partition_date, datetime)
        }
        if not normalized_dates or len(normalized_dates) != len(
            partition_dates
        ):
            raise ValueError("Partition dates must be complete")
        sorted_dates = tuple(sorted(normalized_dates))
        current = self.deferred_time_zone_partition(statistic_id)
        generation = (
            current.generation
            if current is not None
            and current.source_time_zone == source_time_zone
            and current.target_time_zone == target_time_zone
            and current.schema_version == schema_version
            else uuid4().hex
        )
        replacement = DeferredTimeZonePartition(
            source_time_zone=source_time_zone,
            target_time_zone=target_time_zone,
            schema_version=schema_version,
            generation=generation,
            partition_dates=sorted_dates,
            next_probe_on=next_probe_on,
            probe_cursor=probe_cursor % len(sorted_dates),
        )
        if current == replacement:
            return generation
        self._deferred_time_zone_partitions[statistic_id] = replacement
        self._persist()
        return generation

    async def async_defer_time_zone_partition(
        self,
        statistic_id: str,
        source_time_zone: str,
        target_time_zone: str,
        schema_version: int,
        *,
        partition_dates: set[date],
        next_probe_on: date,
        probe_cursor: int = 0,
    ) -> str:
        """Durably store a deferral before ending a completed annual scan."""
        async with self._durable_store_lock:
            if not self._active:
                raise RuntimeError("Cannot mutate an inactive history state")
            previous = self.deferred_time_zone_partition(statistic_id)
            generation = self.defer_time_zone_partition(
                statistic_id,
                source_time_zone,
                target_time_zone,
                schema_version,
                partition_dates=partition_dates,
                next_probe_on=next_probe_on,
                probe_cursor=probe_cursor,
            )
            if self._durable_store is None:
                return generation
            try:
                await self._async_save_durable_time_zone_state()
            except Exception:
                if previous is None:
                    self._deferred_time_zone_partitions.pop(
                        statistic_id,
                        None,
                    )
                else:
                    self._deferred_time_zone_partitions[statistic_id] = (
                        previous
                    )
                self._persist()
                raise
            return generation

    async def async_clear_deferred_time_zone_partition(
        self,
        statistic_id: str,
        generation: str,
    ) -> bool:
        """Durably clear exactly one partition-deferral generation."""
        async with self._durable_store_lock:
            if not self._active:
                return False
            current = self.deferred_time_zone_partition(statistic_id)
            if current is None or current.generation != generation:
                return False
            del self._deferred_time_zone_partitions[statistic_id]
            self._persist()
            if self._durable_store is None:
                return True
            try:
                await self._async_save_durable_time_zone_state()
            except Exception:
                self._deferred_time_zone_partitions[statistic_id] = current
                self._persist()
                return False
            return True

    def effective_source_time_zone(
        self,
        statistic_id: str,
        default: str,
    ) -> str:
        """Return the zone represented by current or in-flight rows.

        A pending migration's target is effective because Recorder writes may
        already have started before their persistence can be verified.  The
        committed fingerprint remains unchanged until the verified scan is
        atomically committed.
        """
        pending = self.pending_time_zone_migration(statistic_id)
        if pending is not None:
            return pending.target_time_zone
        stored = self.source_time_zone(statistic_id)
        return stored if stored is not None else default

    def begin_time_zone_migration(
        self,
        statistic_id: str,
        source_time_zone: str,
        target_time_zone: str,
        *,
        fallback_daily_energy: Mapping[date, float],
        clear_rebuild_baseline_sum: float,
        replacement_hourly_energy: Any | None = None,
    ) -> str:
        """Persist and return a generation token for a zone migration."""
        if not self._active:
            raise RuntimeError("Cannot mutate an inactive history state")
        if not isinstance(statistic_id, str) or not statistic_id:
            raise ValueError("A statistic ID is required")
        if not (
            isinstance(source_time_zone, str)
            and source_time_zone
            and isinstance(target_time_zone, str)
            and target_time_zone
        ):
            raise ValueError("Source and target time zones are required")
        normalized_fallback = self._normalize_fallback_daily_energy(
            fallback_daily_energy
        )
        if normalized_fallback is None:
            raise ValueError("Fallback daily energy must be complete")
        normalized_baseline = float(clear_rebuild_baseline_sum)
        if not math.isfinite(normalized_baseline):
            raise ValueError("Clear-rebuild baseline must be finite")
        normalized_replacement = (
            self._normalize_replacement_hourly_energy(
                replacement_hourly_energy
            )
            if replacement_hourly_energy is not None
            else None
        )
        if (
            replacement_hourly_energy is not None
            and normalized_replacement is None
        ):
            raise ValueError("Replacement hourly energy must be complete")
        if (
            not normalized_fallback
            and normalized_replacement is None
            and normalized_baseline != 0.0
        ):
            raise ValueError(
                "A non-zero clear-rebuild baseline requires an anchor row"
            )
        pending = self.pending_time_zone_migration(statistic_id)
        if (
            pending is not None
            and pending.source_time_zone == source_time_zone
            and pending.target_time_zone == target_time_zone
        ):
            refreshed_fallback = normalized_fallback
            refreshed_baseline = normalized_baseline
            if (
                pending.fallback_daily_energy != refreshed_fallback
                or pending.clear_rebuild_baseline_sum != refreshed_baseline
                or pending.replacement_hourly_energy
                != normalized_replacement
            ):
                # The exact target batch may change between retries as late
                # portal data arrives. Update the same generation before each
                # new Recorder enqueue so crash recovery always describes the
                # most recently queued batch.
                self._pending_time_zone_migrations[statistic_id] = (
                    PendingTimeZoneMigration(
                        source_time_zone=source_time_zone,
                        target_time_zone=target_time_zone,
                        generation=pending.generation,
                        fallback_daily_energy=refreshed_fallback,
                        clear_rebuild_baseline_sum=refreshed_baseline,
                        replacement_hourly_energy=normalized_replacement,
                        replacement_hourly_energy_invalid=False,
                    )
                )
                self._persist()
            return pending.generation
        if pending is not None:
            # A queued Recorder write can have committed before its marker was
            # persisted. Replacing that journal entry would lose the only
            # record of both possible row layouts. Finish the durable pending
            # migration first; a later run may then start the next transition.
            raise RuntimeError(
                "Cannot replace an unfinished time-zone migration"
            )

        generation = uuid4().hex
        self._pending_time_zone_migrations[statistic_id] = (
            PendingTimeZoneMigration(
                source_time_zone=source_time_zone,
                target_time_zone=target_time_zone,
                generation=generation,
                fallback_daily_energy=normalized_fallback,
                clear_rebuild_baseline_sum=normalized_baseline,
                replacement_hourly_energy=normalized_replacement,
                replacement_hourly_energy_invalid=False,
            )
        )
        self._persist()
        return generation

    async def async_begin_time_zone_migration(
        self,
        statistic_id: str,
        source_time_zone: str,
        target_time_zone: str,
        *,
        fallback_daily_energy: Mapping[date, float],
        clear_rebuild_baseline_sum: float,
        replacement_hourly_energy: Any | None = None,
    ) -> str:
        """Durably journal a migration before Recorder can be mutated."""
        async with self._durable_store_lock:
            if not self._active:
                raise RuntimeError("Cannot mutate an inactive history state")
            previous_pending = self.pending_time_zone_migration(statistic_id)
            generation = self.begin_time_zone_migration(
                statistic_id,
                source_time_zone,
                target_time_zone,
                fallback_daily_energy=fallback_daily_energy,
                clear_rebuild_baseline_sum=clear_rebuild_baseline_sum,
                replacement_hourly_energy=replacement_hourly_energy,
            )
            if self._durable_store is not None:
                try:
                    # ``Store.async_save`` performs the write immediately.
                    # This closes the delayed config-entry save window before
                    # the caller queues any external statistics.
                    await self._async_save_durable_time_zone_state()
                except Exception:
                    # A failed write must not leave an in-memory generation
                    # that a later repair could reuse without attempting the
                    # durable write again.
                    if previous_pending is None:
                        self._pending_time_zone_migrations.pop(
                            statistic_id,
                            None,
                        )
                    else:
                        self._pending_time_zone_migrations[statistic_id] = (
                            previous_pending
                        )
                    self._persist()
                    raise
            return generation

    def clear_time_zone_migration(
        self,
        statistic_id: str,
        generation: str,
    ) -> bool:
        """Clear exactly one pending migration generation."""
        if not self._active:
            return False
        pending = self.pending_time_zone_migration(statistic_id)
        if pending is None or pending.generation != generation:
            return False
        del self._pending_time_zone_migrations[statistic_id]
        self._persist()
        return True

    def forget_statistics(self, statistic_ids: set[str]) -> None:
        """Discard completion state for Recorder statistics being removed."""
        if not self._active:
            return
        changed = False
        for statistic_id in statistic_ids:
            changed = self._versions.pop(statistic_id, None) is not None or changed
            changed = (
                self._data_presence.pop(statistic_id, None) is not None
                or changed
            )
            changed = self._coverage.pop(statistic_id, None) is not None or changed
            changed = (
                self._empty_days.pop(statistic_id, None) is not None or changed
            )
            changed = (
                self._empty_retry_cursors.pop(statistic_id, None) is not None
                or changed
            )
            changed = (
                self._empty_retry_runs.pop(statistic_id, None) is not None
                or changed
            )
            changed = (
                self._source_time_zones.pop(statistic_id, None) is not None
                or changed
            )
            changed = (
                self._pending_time_zone_migrations.pop(statistic_id, None)
                is not None
                or changed
            )
            changed = (
                self._deferred_time_zone_partitions.pop(statistic_id, None)
                is not None
                or changed
            )
        if not changed:
            return
        self._persist()

    async def async_forget_statistics(self, statistic_ids: set[str]) -> None:
        """Durably discard state before Recorder removes the statistics."""
        async with self._durable_store_lock:
            if not self._active:
                return
            self.forget_statistics(statistic_ids)
            if self._durable_store is not None:
                # The dedicated journal is authoritative after a restart. Its
                # source-zone rows must be removed before Recorder receives a
                # clear, otherwise a later setup could resurrect a forgotten
                # fingerprint from this store.
                await self._async_save_durable_time_zone_state()

    def mark_complete(
        self,
        statistic_id: str,
        schema_version: int,
        *,
        has_data: bool,
        checked_through: date | None = None,
    ) -> None:
        """Persist a completed import without rewriting unchanged entries."""
        if not self._active:
            return
        current_version = self._versions.get(statistic_id, 0)
        current_coverage = self._coverage.get(statistic_id, {}).get(
            schema_version
        )
        if (
            current_version == schema_version
            and self._data_presence.get(statistic_id, {}).get(schema_version)
            is has_data
            and (
                checked_through is None
                or (
                    current_coverage is not None
                    and current_coverage >= checked_through
                )
            )
        ):
            return

        self._versions[statistic_id] = schema_version
        self._data_presence.setdefault(statistic_id, {})[schema_version] = (
            has_data
        )
        if checked_through is not None and (
            current_coverage is None or checked_through > current_coverage
        ):
            self._coverage.setdefault(statistic_id, {})[schema_version] = (
                checked_through
            )
        self._persist()

    def mark_checked_through(
        self,
        statistic_id: str,
        schema_version: int,
        checked_through: date,
    ) -> bool:
        """Advance the fully queried day for the active storage schema."""
        if (
            not self._active
            or not self.is_current_schema(statistic_id, schema_version)
        ):
            return False
        current = self._coverage.get(statistic_id, {}).get(schema_version)
        if current is not None and current >= checked_through:
            return True
        self._coverage.setdefault(statistic_id, {})[schema_version] = (
            checked_through
        )
        self._persist()
        return True

    def next_empty_retry_date(
        self,
        statistic_id: str,
        schema_version: int,
        *,
        oldest_supported: date,
        before: date,
        today: date,
    ) -> date | None:
        """Return one old empty day, rotating a bounded retry queue."""
        if (
            not self._active
            or not self.is_current_schema(statistic_id, schema_version)
        ):
            return None
        if (
            self._empty_retry_runs.get(statistic_id, {}).get(schema_version)
            == today
        ):
            return None
        candidates = sorted(
            empty_day
            for empty_day in self._empty_days.get(statistic_id, {}).get(
                schema_version,
                set(),
            )
            if oldest_supported <= empty_day < before
        )
        if not candidates:
            return None
        cursor = self._empty_retry_cursors.get(statistic_id, {}).get(
            schema_version
        )
        return next(
            (
                candidate
                for candidate in candidates
                if cursor is None or candidate > cursor
            ),
            candidates[0],
        )

    def empty_days_for_schema(
        self,
        statistic_id: str,
        schema_version: int,
    ) -> set[date]:
        """Return a defensive copy of retryable empty/partial source days."""
        if (
            not self._active
            or not self.is_current_schema(statistic_id, schema_version)
        ):
            return set()
        return set(
            self._empty_days.get(statistic_id, {}).get(schema_version, set())
        )

    def record_empty_day_results(
        self,
        statistic_id: str,
        schema_version: int,
        *,
        empty_days: set[date],
        nonempty_days: set[date],
        oldest_supported: date,
        retried_day: date | None = None,
        checked_on: date | None = None,
    ) -> bool:
        """Persist bounded empty-day evidence and one completed retry."""
        if (
            not self._active
            or not self.is_current_schema(statistic_id, schema_version)
        ):
            return False

        changed = self._update_empty_day_results(
            statistic_id,
            schema_version,
            empty_days=empty_days,
            nonempty_days=nonempty_days,
            oldest_supported=oldest_supported,
            retried_day=retried_day,
            checked_on=checked_on,
        )
        if changed:
            self._persist()
        return True

    def _update_empty_day_results(
        self,
        statistic_id: str,
        schema_version: int,
        *,
        empty_days: set[date],
        nonempty_days: set[date],
        oldest_supported: date,
        retried_day: date | None,
        checked_on: date | None,
    ) -> bool:
        """Update empty-day state in memory and return whether it changed."""

        stored_versions = self._empty_days.setdefault(statistic_id, {})
        previous = stored_versions.get(schema_version, set())
        current = {
            empty_day
            for empty_day in previous
            if empty_day >= oldest_supported
        }
        current.update(
            empty_day
            for empty_day in empty_days
            if empty_day >= oldest_supported
        )
        current.difference_update(nonempty_days)
        changed = current != previous
        if current:
            stored_versions[schema_version] = current
        else:
            stored_versions.pop(schema_version, None)
            if not stored_versions:
                self._empty_days.pop(statistic_id, None)

        if retried_day is not None:
            cursor_versions = self._empty_retry_cursors.setdefault(
                statistic_id,
                {},
            )
            if cursor_versions.get(schema_version) != retried_day:
                cursor_versions[schema_version] = retried_day
                changed = True

        # Rate-limit only a retry that actually completed.  Ordinary empty
        # days from the contiguous main scan must not consume the retry slot,
        # and a failed retry must leave its cursor/run state untouched.
        if checked_on is not None and retried_day is not None:
            retry_runs = self._empty_retry_runs.setdefault(statistic_id, {})
            if retry_runs.get(schema_version) != checked_on:
                retry_runs[schema_version] = checked_on
                changed = True

        return changed

    def commit_scan_if_unchanged(
        self,
        statistic_id: str,
        schema_version: int,
        *,
        has_data: bool,
        expected_version: int,
        checked_through: date | None,
        empty_days: set[date],
        nonempty_days: set[date],
        oldest_supported: date,
        retried_day: date | None = None,
        checked_on: date | None = None,
        source_time_zone: str | None = None,
        time_zone_migration_generation: str | None = None,
        reset_history_progress: bool = False,
    ) -> bool:
        """Atomically commit schema, coverage and scan-result state."""
        if (
            not self._active
            or self._versions.get(statistic_id, 0) != expected_version
        ):
            return False

        pending_migration = self.pending_time_zone_migration(statistic_id)
        if time_zone_migration_generation is not None and (
            pending_migration is None
            or pending_migration.generation
            != time_zone_migration_generation
            or source_time_zone is None
            or pending_migration.target_time_zone != source_time_zone
        ):
            return False

        changed = False
        if self._versions.get(statistic_id, 0) != schema_version:
            self._versions[statistic_id] = schema_version
            changed = True
        presence_versions = self._data_presence.setdefault(statistic_id, {})
        if presence_versions.get(schema_version) is not has_data:
            presence_versions[schema_version] = has_data
            changed = True
        if reset_history_progress:
            # An exact migration-journal replay intentionally ignores the
            # concurrently fetched portal response.  Drop all progress for
            # the target schema while closing the journal so the next normal
            # (non-destructive) repair queries the complete supported window
            # and can apply data that appeared after the journal was written.
            for progress_store in (
                self._coverage,
                self._empty_days,
                self._empty_retry_cursors,
                self._empty_retry_runs,
            ):
                versions = progress_store.get(statistic_id)
                if versions is None or schema_version not in versions:
                    continue
                del versions[schema_version]
                if not versions:
                    del progress_store[statistic_id]
                changed = True
        else:
            current_coverage = self._coverage.get(statistic_id, {}).get(
                schema_version
            )
            if checked_through is not None and (
                current_coverage is None
                or checked_through > current_coverage
            ):
                self._coverage.setdefault(statistic_id, {})[
                    schema_version
                ] = checked_through
                changed = True
            changed = self._update_empty_day_results(
                statistic_id,
                schema_version,
                empty_days=empty_days,
                nonempty_days=nonempty_days,
                oldest_supported=oldest_supported,
                retried_day=retried_day,
                checked_on=checked_on,
            ) or changed
        if (
            source_time_zone is not None
            and (
                pending_migration is None
                or time_zone_migration_generation is not None
            )
            and self._source_time_zones.get(statistic_id)
            != source_time_zone
        ):
            self._source_time_zones[statistic_id] = source_time_zone
            changed = True
        if time_zone_migration_generation is not None:
            # The generation and target were validated before any mutation.
            # Removing the journal and committing its target fingerprint in
            # this same update makes late callbacks from older generations
            # harmless.
            del self._pending_time_zone_migrations[statistic_id]
            changed = True
            if (
                self._deferred_time_zone_partitions.pop(statistic_id, None)
                is not None
            ):
                changed = True
        if changed:
            self._persist()
        return True

    async def async_commit_scan_if_unchanged(
        self,
        statistic_id: str,
        schema_version: int,
        *,
        has_data: bool,
        expected_version: int,
        checked_through: date | None,
        empty_days: set[date],
        nonempty_days: set[date],
        oldest_supported: date,
        retried_day: date | None = None,
        checked_on: date | None = None,
        source_time_zone: str | None = None,
        time_zone_migration_generation: str | None = None,
        reset_history_progress: bool = False,
    ) -> bool:
        """Commit scan state and durably close a migration generation."""
        async with self._durable_store_lock:
            if not self._active:
                return False
            missing = object()
            mutable_state = (
                self._versions,
                self._data_presence,
                self._coverage,
                self._empty_days,
                self._empty_retry_cursors,
                self._empty_retry_runs,
                self._source_time_zones,
                self._pending_time_zone_migrations,
                self._deferred_time_zone_partitions,
            )
            previous_state = tuple(
                (
                    missing
                    if (value := state.get(statistic_id, missing)) is missing
                    else deepcopy(value)
                )
                for state in mutable_state
            )
            committed = self.commit_scan_if_unchanged(
                statistic_id,
                schema_version,
                has_data=has_data,
                expected_version=expected_version,
                checked_through=checked_through,
                empty_days=empty_days,
                nonempty_days=nonempty_days,
                oldest_supported=oldest_supported,
                retried_day=retried_day,
                checked_on=checked_on,
                source_time_zone=source_time_zone,
                time_zone_migration_generation=(
                    time_zone_migration_generation
                ),
                reset_history_progress=reset_history_progress,
            )
            if not committed:
                return False
            if self._durable_store is None or source_time_zone is None:
                return True
            try:
                # Recorder has already been read back. Persist every source
                # fingerprint immediately; for a migration this also removes
                # its journal generation in the same durable snapshot. A crash
                # therefore sees either the old pending generation or the
                # completed target, never an unjournaled ambiguous row layout.
                await self._async_save_durable_time_zone_state()
            except Exception as err:  # noqa: BLE001
                for state, previous in zip(
                    mutable_state,
                    previous_state,
                    strict=True,
                ):
                    if previous is missing:
                        state.pop(statistic_id, None)
                    else:
                        state[statistic_id] = previous
                self._persist()
                _LOGGER.warning(
                    "Unable to persist smart1 history migration journal "
                    "(%s)",
                    type(err).__name__,
                )
                return False
            return True

    def mark_complete_if_unchanged(
        self,
        statistic_id: str,
        schema_version: int,
        *,
        has_data: bool,
        expected_version: int,
        checked_through: date | None = None,
    ) -> bool:
        """Persist late completion only for the still-active generation."""
        if (
            not self._active
            or self._versions.get(statistic_id, 0) != expected_version
        ):
            return False
        self.mark_complete(
            statistic_id,
            schema_version,
            has_data=has_data,
            checked_through=checked_through,
        )
        return True
