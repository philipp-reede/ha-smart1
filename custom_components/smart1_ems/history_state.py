"""Persistent schema, data-presence and coverage state for history."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
from typing import Any


STATISTICS_NAMESPACE_KEY = "statistics_namespace"
HISTORY_SCHEMA_VERSIONS_KEY = "history_schema_versions"
HISTORY_DATA_PRESENCE_KEY = "history_data_presence"
HISTORY_COVERAGE_KEY = "history_coverage"
HISTORY_EMPTY_DAYS_KEY = "history_empty_days"
HISTORY_EMPTY_RETRY_CURSORS_KEY = "history_empty_retry_cursors"
HISTORY_EMPTY_RETRY_RUNS_KEY = "history_empty_retry_runs"
LEGACY_HISTORY_REBUILD_KEY = "legacy_history_rebuild"

# Version 6 gives current daily-only history an unambiguous completion marker.
# Historical versions overlap by mode: v2 was normally daily, v3 could be
# either daily or hourly, and v4/v5 were hourly.  Some v0.7.2 mode switches
# could additionally leave hourly-shaped rows behind a v2 marker. Importers
# therefore inspect legacy row structure before adopting versions 2 through 5.
PV_DAILY_HISTORY_SCHEMA_VERSION = 6
# Version 5 applies the same recovery to hourly version-4 markers. Non-empty
# hourly history can be adopted as current storage; independent coverage may
# still request one supported-window audit.
PV_HISTORY_SCHEMA_VERSION = 5
# Apply the same one-time recovery to derived statistics.  A genuinely empty
# role is marked complete at version 2 after the successful 365-day check, so
# it returns to the normal short refresh window instead of rebuilding forever.
DERIVED_HISTORY_SCHEMA_VERSION = 2


def statistics_namespace_for_device(device_id: str) -> str:
    """Return a stable, non-identifying namespace for one installation."""
    normalized = device_id.strip()
    if not normalized:
        raise ValueError("A device ID is required for the statistics namespace")
    return sha256(normalized.encode()).hexdigest()[:10]


def scoped_statistic_id(statistic_id: str, namespace: str) -> str:
    """Scope a statistic ID while preserving legacy IDs for their owner."""
    return statistic_id if not namespace else f"{statistic_id}_{namespace}"


class Smart1HistoryState:
    """Persist completed history schemas in the owning config entry."""

    def __init__(self, hass: Any, entry: Any) -> None:
        self._hass = hass
        self._entry = entry
        self._active = True
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
            },
        )

    def deactivate(self) -> None:
        """Prevent callbacks from an unloaded entry mutating current state."""
        self._active = False

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
        if not changed:
            return
        self._persist()

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
    ) -> bool:
        """Atomically commit schema, coverage and scan-result state."""
        if (
            not self._active
            or self._versions.get(statistic_id, 0) != expected_version
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
        current_coverage = self._coverage.get(statistic_id, {}).get(
            schema_version
        )
        if checked_through is not None and (
            current_coverage is None or checked_through > current_coverage
        ):
            self._coverage.setdefault(statistic_id, {})[schema_version] = (
                checked_through
            )
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
        if changed:
            self._persist()
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
