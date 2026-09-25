from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).parents[1]

custom_components = sys.modules.setdefault(
    "custom_components",
    types.ModuleType("custom_components"),
)
custom_components.__path__ = [str(ROOT / "custom_components")]
smart1_ems = sys.modules.setdefault(
    "custom_components.smart1_ems",
    types.ModuleType("custom_components.smart1_ems"),
)
smart1_ems.__path__ = [str(ROOT / "custom_components" / "smart1_ems")]

discovery = importlib.import_module("custom_components.smart1_ems.discovery")
interface = importlib.import_module("custom_components.smart1_ems.interface")
point_module = importlib.import_module("custom_components.smart1_ems.point")

Smart1Discovery = discovery.Smart1Discovery
Smart1Point = point_module.Smart1Point


def make_point(
    name: str,
    interface_value: str = "",
    hardware: str = "",
) -> Smart1Point:
    return Smart1Point(
        id=name,
        name=name,
        type="power",
        source="sensor",
        hardware=hardware,
        interface=interface_value,
        parsed_interface=interface.parse_interface(interface_value),
    )


class Smart1DiscoveryTest(unittest.TestCase):
    def test_shared_classifier_detects_ecar_service_and_wp_name(self) -> None:
        result = Smart1Discovery().analyze(
            [
                make_point(
                    "Minimum current",
                    "rio:ecar_1526467313:MinChargeCurrent",
                ),
                make_point("WP Leistung"),
            ]
        )

        self.assertTrue(result.has_wallbox)
        self.assertTrue(result.has_heat_pump)

    def test_shared_classifier_preserves_previous_discovery_signals(
        self,
    ) -> None:
        cases = (
            ("pv_hardware", make_point("Power A", hardware="PV"), "has_pv"),
            (
                "photovoltaic_service",
                make_point("Power B", "rio:photovoltaic_123:AC_POWER"),
                "has_pv",
            ),
            (
                "battery_interface",
                make_point("Power C", "battery"),
                "has_battery",
            ),
            (
                "heater_interface",
                make_point("Power D", "heater"),
                "has_energy_heater",
            ),
        )

        for case_name, point, capability in cases:
            with self.subTest(case=case_name):
                result = Smart1Discovery().analyze([point])
                self.assertTrue(getattr(result, capability))

    def test_shared_classifier_excludes_ems_calculation_and_embedded_wp(
        self,
    ) -> None:
        result = Smart1Discovery().analyze(
            [
                make_point(
                    "Verbrauch ohne WP ECAR HZ",
                    hardware="CounterCalculation",
                ),
                make_point("Powerpoint"),
            ]
        )

        self.assertFalse(result.has_wallbox)
        self.assertFalse(result.has_heat_pump)

if __name__ == "__main__":
    unittest.main()
