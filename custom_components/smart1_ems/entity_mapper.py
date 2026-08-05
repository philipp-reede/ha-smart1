from __future__ import annotations

from dataclasses import dataclass
import re

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)

from .classifier import Smart1Category, classify_point
from .point import Smart1Point


def _is_battery_state_of_charge(point: Smart1Point) -> bool:
    """Return whether a percentage point represents battery state of charge."""
    parsed = point.parsed_interface
    signal = (parsed.signal or "").casefold() if parsed else ""
    if signal == "soc":
        return True

    name = point.name.casefold()
    if re.search(r"(?<!\w)soc(?!\w)", name):
        return True

    return "ladezustand" in name and any(
        token in name for token in ("batt", "batterie", "battery", "speicher")
    )


@dataclass(slots=True)
class Smart1EntityDescription:
    """Description of a Home Assistant entity generated from a smart1 point."""

    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT
    native_unit_of_measurement: str | None = None
    icon: str | None = None
    entity_category: str | None = None
    category: Smart1Category = Smart1Category.OTHER
    value_source: str = "live"
    suffix: str = ""


def get_entity_description(point: Smart1Point) -> Smart1EntityDescription:
    """Map smart1 metadata to Home Assistant entity properties."""
    smart1_type = point.type.lower()
    name = point.name.lower()

    description = Smart1EntityDescription()
    description.category = classify_point(point)

    if smart1_type == "temperature":
        description.device_class = SensorDeviceClass.TEMPERATURE
        description.native_unit_of_measurement = "°C"

    elif smart1_type == "percent":
        description.native_unit_of_measurement = "%"

        if _is_battery_state_of_charge(point):
            description.device_class = SensorDeviceClass.BATTERY

    elif smart1_type == "voltage":
        description.device_class = SensorDeviceClass.VOLTAGE
        description.native_unit_of_measurement = "V"

    elif smart1_type == "ampere":
        description.device_class = SensorDeviceClass.CURRENT
        description.native_unit_of_measurement = "A"

    elif smart1_type in ("herz", "hertz"):
        description.device_class = SensorDeviceClass.FREQUENCY
        description.native_unit_of_measurement = "Hz"

    elif smart1_type in ("energy", "power"):
        description.device_class = SensorDeviceClass.POWER
        description.native_unit_of_measurement = "W"

    elif smart1_type == "pressure":
        description.device_class = SensorDeviceClass.PRESSURE
        description.native_unit_of_measurement = "hPa"

    elif smart1_type == "light":
        description.device_class = SensorDeviceClass.ILLUMINANCE
        description.native_unit_of_measurement = "lx"
        description.entity_category = "diagnostic"

    elif smart1_type == "speed_kmh":
        description.device_class = SensorDeviceClass.SPEED
        description.native_unit_of_measurement = "km/h"

    return description


def get_entity_descriptions(point: Smart1Point) -> list[Smart1EntityDescription]:
    """Create all Home Assistant entities for one smart1 point."""
    return [get_entity_description(point)]
