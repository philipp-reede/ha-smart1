from __future__ import annotations

from collections.abc import Mapping


def _value(row: Mapping[str, str], *names: str) -> str | None:
    """Return the first populated CSV field matching one of the names."""

    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value).strip()

    return None


def parse_pv_cumulative_energy(rows: list[dict[str, str]]) -> float | None:
    """Parse smart1 cumulative PV rows and return production in kWh.

    smart1 reports production in Wh in Value1. Production is documented as
    being stored only on the first used string of each inverter. Selecting
    the largest value per bus/address also avoids double counting if rows for
    several strings or timestamps are present.
    """

    production_by_inverter: dict[tuple[str, str], float] = {}

    for row in rows:
        raw = _value(row, "Value1", "Value 1", '"Value 1"')
        if raw in (None, "No value", '"No value"'):
            continue

        try:
            production_wh = float(raw.replace(",", "."))
        except ValueError:
            continue

        if production_wh < 0:
            continue

        bus = _value(row, "Bus") or ""
        address = _value(row, "Address") or ""
        inverter = (bus, address)
        production_by_inverter[inverter] = max(
            production_wh,
            production_by_inverter.get(inverter, 0.0),
        )

    if not production_by_inverter:
        return None

    return sum(production_by_inverter.values()) / 1000
