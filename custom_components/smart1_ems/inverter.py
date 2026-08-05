"""Models and parsers for documented smart1 inverter data."""

from __future__ import annotations

from dataclasses import dataclass
import re


_INVERTER_ID_PATTERN = re.compile(
    r"^Inverter_B(?P<bus>\d+)_A(?P<address>\d+)$",
    re.IGNORECASE,
)
_MISSING_VALUES = {"", "no value", '"no value"'}


def _clean_text(value: object) -> str:
    """Return a normalized metadata string."""
    text = str(value or "").strip().strip('"')
    return "" if text.casefold() in _MISSING_VALUES else text


def _optional_float(value: object) -> float | None:
    """Parse one optional smart1 numeric value."""
    text = _clean_text(value)
    if not text:
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def _optional_int(value: object) -> int | None:
    """Parse one optional smart1 integer value."""
    number = _optional_float(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _row_value(row: dict[str, str], *keys: str) -> str:
    """Return the first documented spelling present in a CSV row."""
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return ""


@dataclass(frozen=True, slots=True)
class Smart1Inverter:
    """Static inverter metadata returned by `/inverters/{deviceId}`."""

    id: str
    bus: int
    address: int
    name: str = ""
    manufacturer: str = ""
    model: str = ""
    serial_number: str = ""
    capacity_w: float | None = None
    inverter_power_w: float | None = None
    module_field: str = ""
    string_count: int = 0
    string_capacities_w: tuple[float | None, ...] = ()
    string_module_fields: tuple[str, ...] = ()
    monitoring: str = ""
    configured: str = ""

    @property
    def key(self) -> tuple[int, int]:
        """Return the bus and address used by PV data endpoints."""
        return (self.bus, self.address)

    @property
    def string_ids(self) -> tuple[int, ...]:
        """Return configured one-based string identifiers."""
        return tuple(range(1, self.string_count + 1))


@dataclass(frozen=True, slots=True)
class Smart1PvStringSample:
    """Latest documented five-minute values for one inverter string."""

    bus: int
    address: int
    string_id: int
    timestamp: str
    ac_power_w: float | None
    dc_power_w: float | None
    dc_voltage_v: float | None
    inverter_temperature_c: float | None

    @property
    def key(self) -> tuple[int, int, int]:
        """Return the stable lookup key for this string."""
        return (self.bus, self.address, self.string_id)


def parse_inverters(rows: list[dict[str, str]]) -> list[Smart1Inverter]:
    """Parse inverter metadata and ignore rows without a documented ID."""
    inverters: dict[str, Smart1Inverter] = {}

    for row in rows:
        inverter_id = _clean_text(
            _row_value(row, "Inverter Id", "InverterId", '"Inverter Id"')
        )
        match = _INVERTER_ID_PATTERN.match(inverter_id)
        if match is None:
            continue

        declared_strings = _optional_int(row.get("Strings")) or 0
        documented_slots = max(declared_strings, 3)
        capacities = tuple(
            _optional_float(
                _row_value(
                    row,
                    f"String {string_id} Capacity",
                    f'"String {string_id} Capacity"',
                )
            )
            for string_id in range(1, documented_slots + 1)
        )
        module_fields = tuple(
            _clean_text(
                _row_value(
                    row,
                    f"String {string_id} Modulfield",
                    f'"String {string_id} Modulfield"',
                )
            )
            for string_id in range(1, documented_slots + 1)
        )

        if declared_strings == 0:
            configured_slots = [
                index
                for index, (capacity, module_field) in enumerate(
                    zip(capacities, module_fields, strict=True),
                    start=1,
                )
                if capacity not in (None, 0) or module_field
            ]
            declared_strings = max(configured_slots, default=0)

        inverters[inverter_id] = Smart1Inverter(
            id=inverter_id,
            bus=int(match.group("bus")),
            address=int(match.group("address")),
            name=_clean_text(row.get("Name")),
            manufacturer=_clean_text(
                _row_value(row, "Manufactor", "Manufacturer")
            ),
            model=_clean_text(row.get("Type")),
            serial_number=_clean_text(
                _row_value(row, "Serial No", "SerialNo", '"Serial No"')
            ),
            capacity_w=_optional_float(row.get("Capacity")),
            inverter_power_w=_optional_float(
                _row_value(
                    row,
                    "Inverter Power",
                    "InverterPower",
                    '"Inverter Power"',
                )
            ),
            module_field=_clean_text(row.get("Modulfield")),
            string_count=declared_strings,
            string_capacities_w=capacities[:declared_strings],
            string_module_fields=module_fields[:declared_strings],
            monitoring=_clean_text(row.get("Monitoring")),
            configured=_clean_text(row.get("Configured")),
        )

    return list(inverters.values())


def parse_latest_pv_string_samples(
    rows: list[dict[str, str]],
) -> dict[tuple[int, int, int], Smart1PvStringSample]:
    """Return the newest documented row for every inverter string."""
    samples: dict[tuple[int, int, int], Smart1PvStringSample] = {}

    for row in rows:
        bus = _optional_int(row.get("Bus"))
        address = _optional_int(row.get("Address"))
        string_id = _optional_int(
            _row_value(row, "StringId", "String Id", '"String Id"')
        )
        timestamp = _clean_text(row.get("Timestamp"))
        if bus is None or address is None or string_id is None or not timestamp:
            continue

        sample = Smart1PvStringSample(
            bus=bus,
            address=address,
            string_id=string_id,
            timestamp=timestamp,
            ac_power_w=_optional_float(
                _row_value(row, "Value1", "Value 1", '"Value 1"')
            ),
            dc_power_w=_optional_float(
                _row_value(row, "Value2", "Value 2", '"Value 2"')
            ),
            dc_voltage_v=_optional_float(
                _row_value(row, "Value3", "Value 3", '"Value 3"')
            ),
            inverter_temperature_c=_optional_float(
                _row_value(row, "Value4", "Value 4", '"Value 4"')
            ),
        )
        previous = samples.get(sample.key)
        if previous is None or sample.timestamp >= previous.timestamp:
            samples[sample.key] = sample

    return samples
