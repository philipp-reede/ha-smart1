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
smart1_csv = sys.modules.setdefault(
    "custom_components.smart1_csv",
    types.ModuleType("custom_components.smart1_csv"),
)
smart1_csv.__path__ = [str(ROOT / "custom_components" / "smart1_csv")]

classifier = importlib.import_module("custom_components.smart1_csv.classifier")
interface = importlib.import_module("custom_components.smart1_csv.interface")
point_module = importlib.import_module("custom_components.smart1_csv.point")

Smart1Category = classifier.Smart1Category
classify_point = classifier.classify_point
parse_interface = interface.parse_interface
Smart1Point = point_module.Smart1Point


def make_point(name: str, interface_value: str = "") -> Smart1Point:
    return Smart1Point(
        id="test",
        name=name,
        type="power",
        source="sensor",
        interface=interface_value,
        parsed_interface=parse_interface(interface_value),
    )


class ClassifyPointTest(unittest.TestCase):
    def test_heat_pump_from_interface(self) -> None:
        point = make_point("Vorlauf", "modbus:1_2_heatpump_3:temperature")

        self.assertEqual(classify_point(point), Smart1Category.HEAT_PUMP)

    def test_heat_pump_from_standalone_wp_name(self) -> None:
        self.assertEqual(
            classify_point(make_point("WP Leistung")),
            Smart1Category.HEAT_PUMP,
        )

    def test_wp_must_be_standalone(self) -> None:
        self.assertNotEqual(
            classify_point(make_point("Powerpoint")),
            Smart1Category.HEAT_PUMP,
        )

    def test_energy_heater_from_product_name(self) -> None:
        self.assertEqual(
            classify_point(make_point("Energy Heater Leistung")),
            Smart1Category.ENERGY_HEATER,
        )

    def test_heating_element_from_german_name(self) -> None:
        self.assertEqual(
            classify_point(make_point("Heizstab Leistung")),
            Smart1Category.ENERGY_HEATER,
        )


if __name__ == "__main__":
    unittest.main()
