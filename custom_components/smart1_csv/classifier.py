from __future__ import annotations

from enum import StrEnum

from .point import Smart1Point


class Smart1Category(StrEnum):
    """High-level category for a smart1 point."""

    PV = "pv"
    GRID = "grid"
    BATTERY = "battery"
    WALLBOX = "wallbox"
    HEAT_PUMP = "heat_pump"
    CONSUMPTION = "consumption"
    TEMPERATURE = "temperature"
    WEATHER = "weather"
    DIAGNOSTIC = "diagnostic"
    OTHER = "other"


def classify_point(point: Smart1Point) -> Smart1Category:
    """Classify a smart1 point using structured metadata first, names as fallback."""

    parsed = point.parsed_interface

    # 1. Structured interface parsing: strongest signal
    if parsed:
        if parsed.object_type == "wallbox":
            return Smart1Category.WALLBOX

        if parsed.object_type == "energytrader":
            return Smart1Category.BATTERY

        if parsed.object_type == "generic":
            return Smart1Category.OTHER

    # 2. Stable metadata
    name = point.name.lower()
    smart1_type = point.type.lower()
    hardware = point.hardware.lower()
    interface = point.interface.lower()

    if hardware == "pv_global":
        return Smart1Category.PV

    if "wallbox" in interface:
        return Smart1Category.WALLBOX

    if "energytrader" in interface:
        return Smart1Category.BATTERY

    # 3. Name fallback
    if any(token in name for token in ("wallbox", "ladepunkt", "ladestation", "e-car", "ecar")):
        return Smart1Category.WALLBOX

    if any(token in name for token in ("wärmepumpe", "waermepumpe", "heat pump")):
        return Smart1Category.HEAT_PUMP

    if any(token in name for token in ("batterie", "battery", "soc", "speicher")):
        return Smart1Category.BATTERY

    if any(token in name for token in ("pv", "photovoltaik", "photovoltaic", "erzeugung", "produktion")):
        return Smart1Category.PV

    if any(token in name for token in ("netz", "bezug", "einspeisung", "überschuss", "ueberschuss", "grid")):
        return Smart1Category.GRID

    if any(token in name for token in ("verbrauch", "consumption", "hausverbrauch", "eigenverbrauch")):
        return Smart1Category.CONSUMPTION

    if smart1_type == "temperature":
        return Smart1Category.TEMPERATURE

    if any(token in name for token in ("außentemperatur", "aussentemperatur", "luftfeuchte", "luftdruck", "wind", "helligkeit")):
        return Smart1Category.WEATHER

    if smart1_type in ("light", "pressure", "speed_kmh", "herz", "hertz", "nounit"):
        return Smart1Category.DIAGNOSTIC

    return Smart1Category.OTHER