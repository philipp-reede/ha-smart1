from __future__ import annotations

import asyncio
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
sensor = sys.modules.setdefault(
    "homeassistant.components.sensor",
    types.ModuleType("homeassistant.components.sensor"),
)
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
sensor.SensorStateClass = types.SimpleNamespace(
    MEASUREMENT="measurement",
    TOTAL_INCREASING="total_increasing",
)
sensor.SensorEntity = object
sys.modules["homeassistant.components.sensor"] = sensor

const = types.ModuleType("homeassistant.const")
const.DEGREE = "°"
const.EntityCategory = types.SimpleNamespace(DIAGNOSTIC="diagnostic")
const.UnitOfElectricPotential = types.SimpleNamespace(VOLT="V")
const.UnitOfEnergy = types.SimpleNamespace(KILO_WATT_HOUR="kWh")
const.UnitOfPower = types.SimpleNamespace(WATT="W")
const.UnitOfTemperature = types.SimpleNamespace(CELSIUS="°C")
sys.modules["homeassistant.const"] = const

helpers = sys.modules.setdefault(
    "homeassistant.helpers",
    types.ModuleType("homeassistant.helpers"),
)
helpers.__path__ = []
update_coordinator = sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator",
    types.ModuleType("homeassistant.helpers.update_coordinator"),
)


class CoordinatorEntity:
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator

    @property
    def available(self) -> bool:
        return True


update_coordinator.CoordinatorEntity = CoordinatorEntity


class FakeEntityRegistry:
    def __init__(self) -> None:
        self.entities = {}
        self.removed = []

    def async_get_entity_id(self, domain, platform, unique_id):
        return self.entities.get((domain, platform, unique_id))

    def async_remove(self, entity_id) -> None:
        self.removed.append(entity_id)


entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
entity_registry.async_get = lambda hass: hass.entity_registry
sys.modules["homeassistant.helpers.entity_registry"] = entity_registry

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

inverter_module = importlib.import_module("custom_components.smart1_ems.inverter")
module_field_module = importlib.import_module(
    "custom_components.smart1_ems.module_field"
)
sensor_module = importlib.import_module("custom_components.smart1_ems.sensor")


class InverterSensorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.inverter = inverter_module.Smart1Inverter(
            id="Inverter_B2_A1",
            bus=2,
            address=1,
            name="Energy Butler",
            manufacturer="M-TEC",
            model="E-SMART",
            serial_number="serial-1",
            string_count=2,
            string_capacities_w=(5000.0, 6000.0),
            string_module_fields=("East", "West"),
        )
        self.samples = {
            (2, 1, 1): inverter_module.Smart1PvStringSample(
                bus=2,
                address=1,
                string_id=1,
                timestamp="2026-08-05 12:05:00",
                ac_power_w=1200.0,
                dc_power_w=1300.0,
                dc_voltage_v=500.0,
                inverter_temperature_c=41.0,
            ),
            (2, 1, 2): inverter_module.Smart1PvStringSample(
                bus=2,
                address=1,
                string_id=2,
                timestamp="2026-08-05 12:10:00",
                ac_power_w=800.0,
                dc_power_w=900.0,
                dc_voltage_v=450.0,
                inverter_temperature_c=42.0,
            ),
        }
        self.coordinator = types.SimpleNamespace(data={"pv_strings": self.samples})
        self.entity_registry = FakeEntityRegistry()

    def test_string_sensor_uses_physical_inverter_device(self) -> None:
        entity = sensor_module.Smart1InverterStringSensor(
            self.coordinator,
            "entry-1",
            self.inverter,
            2,
            sensor_module.INVERTER_STRING_METRICS[2],
        )

        self.assertEqual(entity.native_value, 450.0)
        self.assertTrue(entity.available)
        self.assertEqual(entity._attr_translation_key, "inverter_string_dc_voltage")
        self.assertEqual(entity._attr_translation_placeholders, {"string_id": "2"})
        self.assertEqual(
            entity._attr_device_info["identifiers"],
            {("smart1_ems", "entry-1:inverter:Inverter_B2_A1")},
        )
        self.assertEqual(entity._attr_device_info["name"], "Energy Butler")
        self.assertEqual(entity._attr_device_info["serial_number"], "serial-1")
        self.assertEqual(
            entity.extra_state_attributes,
            {
                "string_id": 2,
                "configured_capacity_w": 6000.0,
                "module_field": "West",
            },
        )

    def test_temperature_uses_newest_string_sample(self) -> None:
        entity = sensor_module.Smart1InverterTemperatureSensor(
            self.coordinator,
            "entry-1",
            self.inverter,
        )

        self.assertEqual(entity.native_value, 42.0)
        self.assertTrue(entity.available)

    def test_setup_adds_static_module_field_configuration(self) -> None:
        module_field = module_field_module.Smart1ModuleField(
            id="Modulfield_1",
            reference="1",
            name="West",
            tilt_degrees=23.0,
            azimuth_degrees=65.0,
            shadow_from="11:00:00",
            shadow_until="13:00:00",
            monitoring="on",
            configured="ok",
        )
        module_inverter = inverter_module.Smart1Inverter(
            id="Inverter_B2_A1",
            bus=2,
            address=1,
            string_count=2,
            string_capacities_w=(5000.0, 6000.0),
            string_module_fields=("1", "2"),
        )
        hass = types.SimpleNamespace(
            entity_registry=self.entity_registry,
            data={
                "smart1_ems": {
                    "entry-1": {
                        "coordinator": self.coordinator,
                        "devices": [],
                        "discovery": types.SimpleNamespace(has_pv=False),
                        "inverters": [module_inverter],
                        "module_fields": [module_field],
                    }
                }
            },
        )
        added = []

        asyncio.run(
            sensor_module.async_setup_entry(
                hass,
                types.SimpleNamespace(entry_id="entry-1"),
                added.extend,
            )
        )

        module_entities = [
            entity
            for entity in added
            if isinstance(entity, sensor_module.Smart1ModuleFieldSensor)
        ]
        self.assertEqual(len(module_entities), 3)
        values = {
            entity._attr_translation_key: entity._attr_native_value
            for entity in module_entities
        }
        self.assertEqual(
            values,
            {
                "module_field_installed_capacity": 5000.0,
                "module_field_azimuth": 65.0,
                "module_field_tilt": 23.0,
            },
        )
        self.assertEqual(
            module_entities[0]._attr_device_info["identifiers"],
            {("smart1_ems", "entry-1:pv")},
        )
        self.assertEqual(
            module_entities[0].extra_state_attributes,
            {
                "module_field_reference": "1",
                "shadow_from": "11:00:00",
                "shadow_until": "13:00:00",
                "monitoring": "on",
                "configured": "ok",
            },
        )

    def test_setup_adds_three_metrics_per_string_and_temperature(self) -> None:
        hass = types.SimpleNamespace(
            entity_registry=self.entity_registry,
            data={
                "smart1_ems": {
                    "entry-1": {
                        "coordinator": self.coordinator,
                        "devices": [],
                        "discovery": types.SimpleNamespace(has_pv=False),
                        "inverters": [self.inverter],
                    }
                }
            }
        )
        entry = types.SimpleNamespace(entry_id="entry-1")
        added = []

        asyncio.run(
            sensor_module.async_setup_entry(
                hass,
                entry,
                added.extend,
            )
        )

        self.assertEqual(len(added), 7)
        self.assertEqual(
            len(
                [
                    entity
                    for entity in added
                    if isinstance(
                        entity,
                        sensor_module.Smart1InverterStringSensor,
                    )
                ]
            ),
            6,
        )
        self.assertIsInstance(
            added[-1],
            sensor_module.Smart1InverterTemperatureSensor,
        )

    def test_setup_prefers_reported_strings_over_metadata(self) -> None:
        inverter = inverter_module.Smart1Inverter(
            id="Inverter_B2_A1",
            bus=2,
            address=1,
            name="Energy Butler",
            string_count=4,
            string_capacities_w=(5000.0, 6000.0, None, None),
            string_module_fields=("East", "West", "3", ""),
        )
        inactive_entity_ids = []
        for string_id in (3, 4):
            for metric in sensor_module.INVERTER_STRING_METRICS:
                unique_id = sensor_module._inverter_string_unique_id(
                    "entry-1",
                    inverter,
                    string_id,
                    metric,
                )
                entity_id = f"sensor.unused_{string_id}_{metric.key}"
                self.entity_registry.entities[
                    ("sensor", "smart1_ems", unique_id)
                ] = entity_id
                inactive_entity_ids.append(entity_id)

        hass = types.SimpleNamespace(
            entity_registry=self.entity_registry,
            data={
                "smart1_ems": {
                    "entry-1": {
                        "coordinator": self.coordinator,
                        "devices": [],
                        "discovery": types.SimpleNamespace(has_pv=False),
                        "inverters": [inverter],
                    }
                }
            },
        )
        added = []

        asyncio.run(
            sensor_module.async_setup_entry(
                hass,
                types.SimpleNamespace(entry_id="entry-1"),
                added.extend,
            )
        )

        self.assertEqual(len(added), 7)
        self.assertEqual(
            set(self.entity_registry.removed),
            set(inactive_entity_ids),
        )

    def test_setup_uses_metadata_before_detailed_rows_are_available(self) -> None:
        inverter = inverter_module.Smart1Inverter(
            id="Inverter_B2_A1",
            bus=2,
            address=1,
            name="Energy Butler",
            string_count=4,
            string_capacities_w=(5000.0, 6000.0, 0.0, None),
            string_module_fields=("East", "West", "0", ""),
        )
        coordinator = types.SimpleNamespace(data={"pv_strings": {}})
        hass = types.SimpleNamespace(
            entity_registry=self.entity_registry,
            data={
                "smart1_ems": {
                    "entry-1": {
                        "coordinator": coordinator,
                        "devices": [],
                        "discovery": types.SimpleNamespace(has_pv=False),
                        "inverters": [inverter],
                    }
                }
            },
        )
        added = []

        asyncio.run(
            sensor_module.async_setup_entry(
                hass,
                types.SimpleNamespace(entry_id="entry-1"),
                added.extend,
            )
        )

        self.assertEqual(len(added), 7)


if __name__ == "__main__":
    unittest.main()
