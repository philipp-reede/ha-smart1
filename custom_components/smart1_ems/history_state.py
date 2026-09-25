"""Persistent identity and completion state for imported statistics."""

from __future__ import annotations

from hashlib import sha256
from typing import Any


STATISTICS_NAMESPACE_KEY = "statistics_namespace"
HISTORY_SCHEMA_VERSIONS_KEY = "history_schema_versions"
HISTORY_DATA_PRESENCE_KEY = "history_data_presence"
LEGACY_HISTORY_REBUILD_KEY = "legacy_history_rebuild"

PV_DAILY_HISTORY_SCHEMA_VERSION = 2
PV_HISTORY_SCHEMA_VERSION = 3
DERIVED_HISTORY_SCHEMA_VERSION = 1


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

    def is_complete(self, statistic_id: str, schema_version: int) -> bool:
        """Return whether a full import completed for this schema."""
        return self._versions.get(statistic_id, 0) >= schema_version

    def data_presence(
        self,
        statistic_id: str,
        schema_version: int,
    ) -> bool | None:
        """Return whether a completed backfill produced recorder rows."""
        if not self.is_complete(statistic_id, schema_version):
            return None
        return self._data_presence.get(statistic_id, {}).get(schema_version)

    def mark_complete(
        self,
        statistic_id: str,
        schema_version: int,
        *,
        has_data: bool,
    ) -> None:
        """Persist a completed import without rewriting unchanged entries."""
        current_version = self._versions.get(statistic_id, 0)
        if (
            current_version >= schema_version
            and self._data_presence.get(statistic_id, {}).get(schema_version)
            is has_data
        ):
            return

        self._versions[statistic_id] = max(current_version, schema_version)
        self._data_presence.setdefault(statistic_id, {})[schema_version] = (
            has_data
        )
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
            },
        )
