from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Smart1Interface:
    raw: str
    protocol: str | None = None
    service: str | None = None
    service_id: str | None = None
    object_type: str | None = None
    object_id: str | None = None
    signal: str | None = None


def parse_interface(raw: str) -> Smart1Interface:
    parsed = Smart1Interface(raw=raw)

    if not raw:
        return parsed

    if ":" not in raw:
        return parsed

    protocol, remainder = raw.split(":", 1)
    parsed.protocol = protocol

    parts = remainder.split(":")
    path = parts[0]
    parsed.signal = parts[1] if len(parts) > 1 else None

    path_parts = path.split("_")

    if len(path_parts) >= 2:
        parsed.service = path_parts[0]
        parsed.service_id = path_parts[1]

    if len(path_parts) >= 4:
        parsed.object_type = path_parts[2]
        parsed.object_id = path_parts[3]

    return parsed