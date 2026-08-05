"""Models and parsers for documented smart1 inverter-bus metadata."""

from __future__ import annotations

from dataclasses import dataclass
import re


_BUS_ID_PATTERN = re.compile(r"^Bus[_\s-]*(?P<number>\d+)$", re.IGNORECASE)
_MANUFACTURER_FIELD_PATTERN = re.compile(
    r"^busmanufact(?:or|urer)(?P<number>\d+)$",
    re.IGNORECASE,
)
_MISSING_VALUES = {"", "no value", '"no value"'}


def _clean_text(value: object) -> str:
    """Return a normalized bus metadata string."""
    text = str(value or "").strip().strip('"')
    return "" if text.casefold() in _MISSING_VALUES else text


def _normalized_field_name(value: object) -> str:
    """Return a CSV field name without quoting, spaces, or underscores."""
    return re.sub(r"[\s_]+", "", str(value or "").strip().strip('"'))


def _optional_int(value: object) -> int | None:
    """Parse one optional integer value."""
    text = _clean_text(value)
    if not text:
        return None
    try:
        return int(float(text.replace(",", ".")))
    except ValueError:
        return None


def _row_bus_id(row: dict[str, str]) -> str:
    """Return the normalized bus identifier from one API row."""
    return _clean_text(
        row.get("BusId")
        or row.get("Bus Id")
        or row.get('"BusId"')
    )


def _row_manufacturers(row: dict[str, str]) -> dict[int, str]:
    """Return non-empty manufacturer protocols indexed by API position."""
    manufacturers: dict[int, str] = {}
    for field, value in row.items():
        field_match = _MANUFACTURER_FIELD_PATTERN.match(
            _normalized_field_name(field)
        )
        if field_match is None:
            continue
        manufacturer = _clean_text(value)
        if manufacturer:
            manufacturers[int(field_match.group("number"))] = manufacturer
    return manufacturers


def bus_response_diagnostics(
    rows: list[dict[str, str]],
) -> dict[str, int]:
    """Classify bus rows without exposing identifiers or response values."""
    result = {
        "rows_with_documented_bus_id": 0,
        "rows_with_numeric_bus_id": 0,
        "rows_with_other_bus_id": 0,
        "rows_without_bus_id": 0,
        "rows_with_configuration_status": 0,
        "rows_with_manufacturer_count": 0,
        "rows_with_manufacturer_protocols": 0,
    }

    for row in rows:
        bus_id = _row_bus_id(row)
        if _BUS_ID_PATTERN.fullmatch(bus_id):
            result["rows_with_documented_bus_id"] += 1
        elif _optional_int(bus_id) is not None:
            result["rows_with_numeric_bus_id"] += 1
        elif bus_id:
            result["rows_with_other_bus_id"] += 1
        else:
            result["rows_without_bus_id"] += 1

        if _clean_text(row.get("BusConfigured")):
            result["rows_with_configuration_status"] += 1
        if _clean_text(
            row.get("BusManufactors")
            or row.get("BusManufacturers")
        ):
            result["rows_with_manufacturer_count"] += 1
        if _row_manufacturers(row):
            result["rows_with_manufacturer_protocols"] += 1

    return result


@dataclass(frozen=True, slots=True)
class Smart1BusSystem:
    """Static metadata returned by `/bus/{deviceId}`."""

    id: str
    number: int
    configured: str = ""
    documented_manufacturer_count: int | None = None
    manufacturers: tuple[str, ...] = ()

    @property
    def is_active(self) -> bool:
        """Return whether the portal reports a configured bus system."""
        return bool(self.configured or self.manufacturers)


def parse_bus_systems(rows: list[dict[str, str]]) -> list[Smart1BusSystem]:
    """Parse configured inverter buses and omit empty bus slots."""
    bus_systems: dict[int, Smart1BusSystem] = {}

    for row in rows:
        bus_id = _row_bus_id(row)
        match = _BUS_ID_PATTERN.match(bus_id)
        if match is None:
            continue

        manufacturers_by_index = _row_manufacturers(row)

        manufacturers = tuple(
            dict.fromkeys(
                manufacturers_by_index[index]
                for index in sorted(manufacturers_by_index)
            )
        )
        bus_system = Smart1BusSystem(
            id=bus_id,
            number=int(match.group("number")),
            configured=_clean_text(row.get("BusConfigured")),
            documented_manufacturer_count=_optional_int(
                row.get("BusManufactors")
                or row.get("BusManufacturers")
            ),
            manufacturers=manufacturers,
        )
        if bus_system.is_active:
            bus_systems[bus_system.number] = bus_system

    return [bus_systems[number] for number in sorted(bus_systems)]
