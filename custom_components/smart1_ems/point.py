from __future__ import annotations

from dataclasses import dataclass
from .interface import Smart1Interface


@dataclass(slots=True)
class Smart1Point:
    """Represents one linear measurement point from the smart1 EMS."""

    id: str
    name: str
    type: str
    source: str

    hardware: str = ""
    interface: str = ""

    max: str = ""
    color: str = ""
    index: str = ""

    raw: dict[str, str] | None = None
    parsed_interface: Smart1Interface | None = None
