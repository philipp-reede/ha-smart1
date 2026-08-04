from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).parents[1]

homeassistant = sys.modules.setdefault(
    "homeassistant",
    types.ModuleType("homeassistant"),
)
homeassistant.__path__ = []
components = sys.modules.setdefault(
    "homeassistant.components",
    types.ModuleType("homeassistant.components"),
)
components.__path__ = []
sensor = types.ModuleType("homeassistant.components.sensor")
sensor.SensorDeviceClass = types.SimpleNamespace(
    BATTERY="battery",
    CURRENT="current",
    ENERGY="energy",
    FREQUENCY="frequency",
    ILLUMINANCE="illuminance",
    POWER="power",
    PRESSURE="pressure",
    SPEED="speed",
    TEMPERATURE="temperature",
    VOLTAGE="voltage",
)
sensor.SensorStateClass = types.SimpleNamespace(MEASUREMENT="measurement")
sys.modules["homeassistant.components.sensor"] = sensor

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

entity_mapper = importlib.import_module("custom_components.smart1_ems.entity_mapper")
interface = importlib.import_module("custom_components.smart1_ems.interface")
point_module = importlib.import_module("custom_components.smart1_ems.point")

Smart1Point = point_module.Smart1Point


def make_percent_point(name: str, interface_value: str) -> Smart1Point:
    return Smart1Point(
        id="test",
        name=name,
        type="Percent",
        source="sensor",
        interface=interface_value,
        parsed_interface=interface.parse_interface(interface_value),
    )


class EntityMapperTest(unittest.TestCase):
    def test_soc_signal_is_exposed_as_battery_percentage(self) -> None:
        point = make_percent_point(
            "BATT Ladezustand",
            "rio:battery_1526468329:SOC",
        )

        description = entity_mapper.get_entity_description(point)

        self.assertEqual(description.device_class, "battery")
        self.assertEqual(description.native_unit_of_measurement, "%")
        self.assertEqual(description.state_class, "measurement")

    def test_battery_use_case_flag_is_not_exposed_as_state_of_charge(self) -> None:
        point = make_percent_point(
            "EC Battery UseCase Enabled",
            "rio:energycloud_1_energytrader_2:IS_ENABLED",
        )

        description = entity_mapper.get_entity_description(point)

        self.assertIsNone(description.device_class)
        self.assertEqual(description.native_unit_of_measurement, "%")

    def test_state_of_health_is_not_exposed_as_state_of_charge(self) -> None:
        point = make_percent_point(
            "BATT SOH",
            "rio:battery_1526468329:SOH",
        )

        self.assertIsNone(
            entity_mapper.get_entity_description(point).device_class
        )


if __name__ == "__main__":
    unittest.main()
