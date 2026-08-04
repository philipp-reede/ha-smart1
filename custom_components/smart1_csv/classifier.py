from __future__ import annotations

from enum import StrEnum
import re

from .point import Smart1Point


class Smart1Category(StrEnum):
    """High-level category for a smart1 point."""

    PV = "pv"
    GRID = "grid"
    BATTERY = "battery"
    WALLBOX = "wallbox"
    HEAT_PUMP = "heat_pump"
    ENERGY_HEATER = "energy_heater"
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
        service = (parsed.service or "").lower()
        object_type = (parsed.object_type or "").lower()

        if object_type == "wallbox":
            return Smart1Category.WALLBOX

        if object_type == "energytrader":
            return Smart1Category.BATTERY

        if object_type in {"heatpump", "heat_pump"}:
            return Smart1Category.HEAT_PUMP

        if object_type in {"energyheater", "energy_heater", "heater"}:
            return Smart1Category.ENERGY_HEATER

        if service == "battery":
            return Smart1Category.BATTERY

        if service in {"ecar", "wallbox"}:
            return Smart1Category.WALLBOX

        if service in {"heatpump", "heat_pump"}:
            return Smart1Category.HEAT_PUMP

        if service == "riometer":
            return Smart1Category.GRID

    # 2. Stable metadata
    name = point.name.lower()
    smart1_type = point.type.lower()
    hardware = point.hardware.lower()
    interface = point.interface.lower()

    # Calculations containing several device names are EMS-level measurement
    # roles, not properties of the one device whose name happens to match.
    if (
        hardware in {"arithmetic", "countercalculation"}
        and any(token in name for token in ("ecar", "e-car"))
        and ("+" in name or "-" in name or "ohne" in name)
    ):
        return Smart1Category.OTHER

    if hardware == "pv_global":
        return Smart1Category.PV

    if "wallbox" in interface:
        return Smart1Category.WALLBOX

    if "energytrader" in interface:
        return Smart1Category.BATTERY

    if "heatpump" in interface or "heat_pump" in interface:
        return Smart1Category.HEAT_PUMP

    if any(token in interface for token in ("energyheater", "energy_heater")):
        return Smart1Category.ENERGY_HEATER

    # 3. Name fallback
    if any(token in name for token in ("wallbox", "ladepunkt", "ladestation", "e-car", "ecar")):
        return Smart1Category.WALLBOX

    if (
        any(token in name for token in ("wärmepumpe", "waermepumpe", "heat pump"))
        or re.search(r"(?<!\w)wp(?!\w)", name)
    ):
        return Smart1Category.HEAT_PUMP

    if any(token in name for token in ("energy heater", "energyheater", "heizstab")):
        return Smart1Category.ENERGY_HEATER

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
