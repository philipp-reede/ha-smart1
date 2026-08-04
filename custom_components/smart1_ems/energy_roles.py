"""Energy Dashboard roles and point recommendations for smart1 EMS."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256

from .classifier import Smart1Category, classify_point
from .const import DOMAIN
from .point import Smart1Point


@dataclass(frozen=True, slots=True)
class EnergyRole:
    """One derived energy statistic exposed by the integration."""

    key: str
    category: Smart1Category
    name: str


ENERGY_ROLES = (
    EnergyRole("grid_import", Smart1Category.GRID, "smart1 EMS grid import"),
    EnergyRole("grid_export", Smart1Category.GRID, "smart1 EMS grid export"),
    EnergyRole("battery_charge", Smart1Category.BATTERY, "smart1 EMS battery charge"),
    EnergyRole(
        "battery_discharge",
        Smart1Category.BATTERY,
        "smart1 EMS battery discharge",
    ),
    EnergyRole(
        "wallbox_consumption",
        Smart1Category.WALLBOX,
        "smart1 EMS wallbox consumption",
    ),
    EnergyRole(
        "heat_pump_consumption",
        Smart1Category.HEAT_PUMP,
        "smart1 EMS heat pump consumption",
    ),
    EnergyRole(
        "auxiliary_heater_consumption",
        Smart1Category.ENERGY_HEATER,
        "smart1 EMS auxiliary heater consumption",
    ),
)
ENERGY_ROLES_BY_KEY = {role.key: role for role in ENERGY_ROLES}


def energy_candidates(
    points: list[Smart1Point],
    role: EnergyRole,
) -> list[Smart1Point]:
    """Return energy-typed counters that can provide one role."""
    candidates = [
        point
        for point in points
        if point.source == "counter"
        and point.type.lower() == "energy"
        and classify_point(point) == role.category
    ]

    if role.key == "grid_import":
        return [
            point
            for point in candidates
            if _signal(point) == "usage" or "bezug" in point.name.lower()
        ]
    if role.key == "grid_export":
        return [
            point
            for point in candidates
            if _signal(point) == "overproduction"
            or any(
                token in point.name.lower()
                for token in ("überschuss", "ueberschuss", "einspeisung")
            )
        ]
    if role.key == "battery_charge":
        return [
            point
            for point in candidates
            if _signal(point) == "charge_power"
            or (
                "laden" in point.name.lower()
                and "entladen" not in point.name.lower()
            )
        ]
    if role.key == "battery_discharge":
        return [
            point
            for point in candidates
            if _signal(point) == "discharge_power"
            or "entladen" in point.name.lower()
        ]

    return candidates


def _signal(point: Smart1Point) -> str:
    parsed = point.parsed_interface
    return (parsed.signal or "").lower() if parsed else ""


def recommend_energy_roles(points: list[Smart1Point]) -> dict[str, str]:
    """Recommend unambiguous sources while leaving duplicate-prone roles explicit."""
    recommendations: dict[str, str] = {}

    def first_matching(
        role_key: str,
        predicate: Callable[[Smart1Point], bool],
    ) -> None:
        role = ENERGY_ROLES_BY_KEY[role_key]
        if point := next(
            (point for point in energy_candidates(points, role) if predicate(point)),
            None,
        ):
            recommendations[role_key] = point.id

    first_matching("grid_import", lambda point: _signal(point) == "usage")
    first_matching("grid_export", lambda point: _signal(point) == "overproduction")
    first_matching("battery_charge", lambda point: _signal(point) == "charge_power")
    first_matching(
        "battery_discharge",
        lambda point: _signal(point) == "discharge_power",
    )
    first_matching(
        "wallbox_consumption",
        lambda point: _signal(point) == "meter_power"
        and "monitoring" not in point.name.lower(),
    )
    first_matching(
        "heat_pump_consumption",
        lambda point: "bezug gesamt" in point.name.lower(),
    )
    first_matching(
        "auxiliary_heater_consumption",
        lambda point: point.hardware.lower() == "buscounter",
    )

    return recommendations


def statistic_id_for_role(role_key: str, point_id: str) -> str:
    """Return a stable statistic ID that changes when its source changes."""
    source_hash = sha256(point_id.encode()).hexdigest()[:8]
    return f"{DOMAIN}:{role_key}_{source_hash}"
