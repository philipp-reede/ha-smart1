from __future__ import annotations

from dataclasses import dataclass, field

from .point import Smart1Point


@dataclass(slots=True)
class Smart1DiscoveryResult:
    """Describes the capabilities of a smart1 installation."""

    hardware: set[str] = field(default_factory=set)
    interfaces: set[str] = field(default_factory=set)
    point_types: set[str] = field(default_factory=set)

    has_pv: bool = False
    has_battery: bool = False
    has_wallbox: bool = False
    has_heat_pump: bool = False
    has_energy_heater: bool = False
    inverter_count: int = 0
    module_field_count: int = 0

    def to_dict(self) -> dict:
        return {
            "hardware": sorted(self.hardware),
            "interfaces": sorted(self.interfaces),
            "point_types": sorted(self.point_types),
            "has_pv": self.has_pv,
            "has_battery": self.has_battery,
            "has_wallbox": self.has_wallbox,
            "has_heat_pump": self.has_heat_pump,
            "has_energy_heater": self.has_energy_heater,
            "inverter_count": self.inverter_count,
            "module_field_count": self.module_field_count,
        }


class Smart1Discovery:
    """Analyze a smart1 installation."""

    def analyze(self, points: list[Smart1Point]) -> Smart1DiscoveryResult:
        result = Smart1DiscoveryResult()

        for point in points:
            if point.hardware:
                result.hardware.add(point.hardware.lower())

            if point.interface:
                result.interfaces.add(point.interface.lower())

            if point.type:
                result.point_types.add(point.type.lower())

            interface = (point.interface or "").lower()
            hardware = (point.hardware or "").lower()
            name = (point.name or "").lower()

            if "pv" in hardware or "photovoltaic" in interface:
                result.has_pv = True

            if "battery" in interface or "energytrader" in interface:
                result.has_battery = True

            if "wallbox" in interface or "ecar" in name:
                result.has_wallbox = True

            if (
                "heatpump" in interface
                or "wärmepumpe" in name
                or "waermepumpe" in name
            ):
                result.has_heat_pump = True

            if (
                "heater" in interface
                or "energy heater" in name
                or "energyheater" in name
                or "heizstab" in name
            ):
                result.has_energy_heater = True

        return result
