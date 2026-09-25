from __future__ import annotations

from dataclasses import dataclass, field

from .classifier import Smart1Category, classify_point
from .interface import parse_interface
from .point import Smart1Point


@dataclass(slots=True)
class Smart1DiscoveryResult:
    """Describes the capabilities of a smart1 installation."""

    hardware: set[str] = field(default_factory=set)
    interface_types: set[tuple[str, str, str, str, bool, bool]] = field(
        default_factory=set
    )
    interface_count: int = 0
    unparsed_interface_count: int = 0
    point_types: set[str] = field(default_factory=set)

    has_pv: bool = False
    has_battery: bool = False
    has_wallbox: bool = False
    has_heat_pump: bool = False
    has_energy_heater: bool = False
    inverter_count: int = 0
    module_field_count: int = 0
    bus_count: int = 0

    def add_pv_evidence(
        self,
        *,
        inverter_count: int,
        module_field_count: int,
        cumulative_energy: float | None,
    ) -> None:
        """Add PV capability evidence from optional portal endpoints."""
        self.inverter_count = inverter_count
        self.module_field_count = module_field_count
        self.has_pv = self.has_pv or any(
            (
                inverter_count > 0,
                module_field_count > 0,
                cumulative_energy is not None,
            )
        )

    def to_dict(self) -> dict:
        """Return identifier-free discovery details for diagnostics and logs."""
        return {
            "hardware": sorted(self.hardware),
            "interface_count": self.interface_count,
            "unparsed_interface_count": self.unparsed_interface_count,
            "interface_types": [
                {
                    "protocol": protocol or None,
                    "service": service or None,
                    "object_type": object_type or None,
                    "signal": signal or None,
                    "service_id_present": service_id_present,
                    "object_id_present": object_id_present,
                }
                for (
                    protocol,
                    service,
                    object_type,
                    signal,
                    service_id_present,
                    object_id_present,
                ) in sorted(self.interface_types)
            ],
            "point_types": sorted(self.point_types),
            "has_pv": self.has_pv,
            "has_battery": self.has_battery,
            "has_wallbox": self.has_wallbox,
            "has_heat_pump": self.has_heat_pump,
            "has_energy_heater": self.has_energy_heater,
            "inverter_count": self.inverter_count,
            "module_field_count": self.module_field_count,
            "bus_count": self.bus_count,
        }


class Smart1Discovery:
    """Analyze a smart1 installation."""

    def analyze(self, points: list[Smart1Point]) -> Smart1DiscoveryResult:
        result = Smart1DiscoveryResult()
        raw_interfaces: set[str] = set()

        for point in points:
            if point.hardware:
                result.hardware.add(point.hardware.lower())

            if point.interface:
                raw_interfaces.add(point.interface.lower())
                parsed = point.parsed_interface or parse_interface(
                    point.interface
                )
                if not any(
                    (
                        parsed.protocol,
                        parsed.service,
                        parsed.object_type,
                        parsed.signal,
                    )
                ):
                    result.unparsed_interface_count += 1
                else:
                    result.interface_types.add(
                        (
                            (parsed.protocol or "").lower(),
                            (parsed.service or "").lower(),
                            (parsed.object_type or "").lower(),
                            (parsed.signal or "").lower(),
                            parsed.service_id is not None,
                            parsed.object_id is not None,
                        )
                    )

            if point.type:
                result.point_types.add(point.type.lower())

            category = classify_point(point)

            if category == Smart1Category.PV:
                result.has_pv = True

            if category == Smart1Category.BATTERY:
                result.has_battery = True

            if category == Smart1Category.WALLBOX:
                result.has_wallbox = True

            if category == Smart1Category.HEAT_PUMP:
                result.has_heat_pump = True

            if category == Smart1Category.ENERGY_HEATER:
                result.has_energy_heater = True

        result.interface_count = len(raw_interfaces)
        return result
