from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


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
        self.registry_entries = []
        self.removed = []

    def async_get_entity_id(self, domain, platform, unique_id):
        return self.entities.get((domain, platform, unique_id))

    def async_remove(self, entity_id) -> None:
        self.removed.append(entity_id)

    def add_entry(
        self,
        entity_id,
        unique_id,
        *,
        config_entry_id="entry-1",
        domain="sensor",
        platform="smart1_ems",
        name=None,
        unit_of_measurement=None,
        options=None,
        aliases=None,
    ) -> None:
        self.entities[(domain, platform, unique_id)] = entity_id
        self.registry_entries.append(
            types.SimpleNamespace(
                config_entry_id=config_entry_id,
                domain=domain,
                entity_id=entity_id,
                platform=platform,
                unique_id=unique_id,
                name=name,
                unit_of_measurement=unit_of_measurement,
                options=options or {},
                aliases=[entity_registry.COMPUTED_NAME]
                if aliases is None
                else aliases,
            )
        )


class FakeDeviceRegistry:
    def __init__(self) -> None:
        self.created = []

    def async_get_or_create(self, **kwargs):
        self.created.append(kwargs)
        return types.SimpleNamespace(id="ems-device-id")


def async_get_device_registry(hass):
    if not hasattr(hass, "device_registry"):
        hass.device_registry = FakeDeviceRegistry()
    return hass.device_registry


device_registry = types.ModuleType("homeassistant.helpers.device_registry")


class DeviceInfo(dict):
    __annotations__ = {"via_device_id": str}


device_registry.DeviceInfo = DeviceInfo
device_registry.async_get = async_get_device_registry
sys.modules["homeassistant.helpers.device_registry"] = device_registry


entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
entity_registry.COMPUTED_NAME = None
entity_registry.async_get = lambda hass: hass.entity_registry
entity_registry.async_entries_for_config_entry = (
    lambda registry, entry_id: [
        entry
        for entry in registry.registry_entries
        if entry.config_entry_id == entry_id
    ]
)
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

bus_module = importlib.import_module("custom_components.smart1_ems.bus")
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
        self.device_registry = FakeDeviceRegistry()
        self.entity_registry = FakeEntityRegistry()

    def test_string_sensor_uses_physical_inverter_device(self) -> None:
        entity = sensor_module.Smart1InverterStringSensor(
            self.coordinator,
            "entry-1",
            self.inverter,
            2,
            sensor_module.INVERTER_STRING_METRICS[2],
            "ems-device-id",
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
            entity._attr_device_info["via_device_id"],
            "ems-device-id",
        )
        self.assertNotIn("via_device", entity._attr_device_info)
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
            "ems-device-id",
        )

        self.assertEqual(entity.native_value, 42.0)
        self.assertTrue(entity.available)

    def test_temperature_compares_dst_offsets_chronologically(self) -> None:
        self.coordinator.data["pv_strings"] = {
            (2, 1, 1): inverter_module.Smart1PvStringSample(
                bus=2,
                address=1,
                string_id=1,
                timestamp="2026-10-25T02:55:00+02:00",
                ac_power_w=None,
                dc_power_w=None,
                dc_voltage_v=None,
                inverter_temperature_c=41.0,
            ),
            (2, 1, 2): inverter_module.Smart1PvStringSample(
                bus=2,
                address=1,
                string_id=2,
                timestamp="2026-10-25T02:05:00+01:00",
                ac_power_w=None,
                dc_power_w=None,
                dc_voltage_v=None,
                inverter_temperature_c=42.0,
            ),
        }
        entity = sensor_module.Smart1InverterTemperatureSensor(
            self.coordinator,
            "entry-1",
            self.inverter,
            "ems-device-id",
        )

        self.assertEqual(entity.native_value, 42.0)

    def test_inverter_device_info_supports_home_assistant_2026_7(self) -> None:
        class LegacyDeviceInfo(dict):
            __annotations__ = {"via_device": tuple}

        with patch.object(sensor_module.dr, "DeviceInfo", LegacyDeviceInfo):
            device_info = sensor_module._inverter_device_info(
                "entry-1",
                self.inverter,
                "unused-registry-id",
            )

        self.assertEqual(
            device_info["via_device"],
            ("smart1_ems", "entry-1:other"),
        )
        self.assertNotIn("via_device_id", device_info)

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
            device_registry=self.device_registry,
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

    def test_setup_adds_active_bus_configuration_to_ems(self) -> None:
        bus = bus_module.Smart1BusSystem(
            id="Bus2",
            number=2,
            configured="ok",
            documented_manufacturer_count=1,
            manufacturers=("M-TEC",),
        )
        hass = types.SimpleNamespace(
            entity_registry=self.entity_registry,
            data={
                "smart1_ems": {
                    "entry-1": {
                        "coordinator": self.coordinator,
                        "devices": [],
                        "discovery": types.SimpleNamespace(has_pv=False),
                        "inverters": [],
                        "buses": [bus],
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

        self.assertEqual(len(added), 1)
        entity = added[0]
        self.assertIsInstance(
            entity,
            sensor_module.Smart1BusConfigurationSensor,
        )
        self.assertEqual(entity._attr_native_value, "ok")
        self.assertEqual(entity._attr_translation_placeholders, {"bus": "2"})
        self.assertEqual(
            entity._attr_device_info["identifiers"],
            {("smart1_ems", "entry-1:other")},
        )
        self.assertEqual(
            entity.extra_state_attributes,
            {
                "bus_number": 2,
                "manufacturers": ["M-TEC"],
                "documented_manufacturer_count": 1,
            },
        )

    def test_setup_adds_three_metrics_per_string_and_temperature(self) -> None:
        hass = types.SimpleNamespace(
            device_registry=self.device_registry,
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
        self.assertEqual(
            self.device_registry.created,
            [
                {
                    "config_entry_id": "entry-1",
                    "identifiers": {("smart1_ems", "entry-1:other")},
                    "name": "Smart1 EMS",
                    "manufacturer": "smart1",
                    "model": "Smart1 EMS",
                }
            ],
        )
        self.assertTrue(
            all(
                entity._attr_device_info["via_device_id"]
                == "ems-device-id"
                for entity in added
            )
        )
        self.assertTrue(
            all(
                "via_device" not in entity._attr_device_info
                for entity in added
            )
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
                self.entity_registry.add_entry(
                    entity_id,
                    unique_id,
                    # HA stores the entity's integration-provided current
                    # unit at top level; this is not a user override.
                    unit_of_measurement=metric.unit,
                    options={
                        "sensor": {
                            "suggested_display_precision": metric.precision,
                        },
                        # Home Assistant may populate assistant exposure
                        # automatically. It must not make an inactive string
                        # look explicitly customized by the user.
                        "conversation": {"should_expose": False},
                    },
                )
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
                        "inverter_discovery_authoritative": True,
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

    def test_explicit_sensor_option_marks_registry_entry_as_customized(
        self,
    ) -> None:
        registry_entry = types.SimpleNamespace(
            aliases=[entity_registry.COMPUTED_NAME],
            options={
                "sensor": {
                    "display_precision": 0,
                    "suggested_display_precision": 1,
                },
                "conversation": {"should_expose": False},
            },
        )

        self.assertTrue(
            sensor_module._registry_entry_is_user_customized(registry_entry)
        )

    def test_setup_removes_only_stale_owned_topology_after_authoritative_discovery(
        self,
    ) -> None:
        stale_ids = {
            "sensor.old_string": (
                "smart1_entry-1_inverter_2_1_string_3_ac_power_w"
            ),
            "sensor.old_temperature": (
                "smart1_entry-1_inverter_2_2_temperature"
            ),
            "sensor.old_module": (
                "smart1_entry-1_module_field_old_tilt_degrees"
            ),
            "sensor.old_module_capacity": (
                "smart1_entry-1_module_field_old_installed_capacity_w"
            ),
            "sensor.old_bus": (
                "smart1_entry-1_inverter_bus_3_configuration"
            ),
        }
        for entity_id, unique_id in stale_ids.items():
            self.entity_registry.add_entry(entity_id, unique_id)

        preserved_ids = {
            "sensor.linear": "smart1_entry-1_counter_1_live",
            "sensor.future_topology": (
                "smart1_entry-1_inverter_2_1_future_metric"
            ),
        }
        for entity_id, unique_id in preserved_ids.items():
            self.entity_registry.add_entry(entity_id, unique_id)
        self.entity_registry.add_entry(
            "sensor.other_entry",
            "smart1_other-entry_inverter_2_1_temperature",
            config_entry_id="other-entry",
        )
        self.entity_registry.add_entry(
            "sensor.other_platform",
            "smart1_entry-1_inverter_2_1_temperature",
            platform="other_integration",
        )

        hass = types.SimpleNamespace(
            entity_registry=self.entity_registry,
            data={
                "smart1_ems": {
                    "entry-1": {
                        "coordinator": types.SimpleNamespace(
                            data={"pv_strings": {}}
                        ),
                        "devices": [],
                        "discovery": types.SimpleNamespace(has_pv=False),
                        "inverters": [],
                        "module_fields": [],
                        "buses": [],
                        "inverter_discovery_authoritative": True,
                        "module_field_discovery_authoritative": True,
                        "bus_discovery_authoritative": True,
                    }
                }
            },
        )

        asyncio.run(
            sensor_module.async_setup_entry(
                hass,
                types.SimpleNamespace(entry_id="entry-1"),
                lambda _entities: None,
            )
        )

        self.assertEqual(
            set(self.entity_registry.removed),
            set(stale_ids),
        )

    def test_setup_preserves_stale_topology_when_discovery_is_not_authoritative(
        self,
    ) -> None:
        stale_ids = {
            "sensor.old_string": (
                "smart1_entry-1_inverter_2_1_string_3_ac_power_w"
            ),
            "sensor.old_module": (
                "smart1_entry-1_module_field_old_tilt_degrees"
            ),
            "sensor.old_bus": (
                "smart1_entry-1_inverter_bus_3_configuration"
            ),
        }
        for entity_id, unique_id in stale_ids.items():
            self.entity_registry.add_entry(entity_id, unique_id)

        hass = types.SimpleNamespace(
            entity_registry=self.entity_registry,
            data={
                "smart1_ems": {
                    "entry-1": {
                        "coordinator": types.SimpleNamespace(
                            data={"pv_strings": {}}
                        ),
                        "devices": [],
                        "discovery": types.SimpleNamespace(has_pv=False),
                        "inverters": [],
                        "module_fields": [],
                        "buses": [],
                    }
                }
            },
        )

        asyncio.run(
            sensor_module.async_setup_entry(
                hass,
                types.SimpleNamespace(entry_id="entry-1"),
                lambda _entities: None,
            )
        )

        self.assertEqual(self.entity_registry.removed, [])

    def test_module_capacity_waits_for_authoritative_inverter_discovery(
        self,
    ) -> None:
        module_field = module_field_module.Smart1ModuleField(
            id="Modulfield_1",
            reference="1",
            name="West",
            tilt_degrees=23.0,
            azimuth_degrees=65.0,
        )
        capacity_unique_id = sensor_module._module_field_unique_id(
            "entry-1",
            module_field,
            sensor_module.MODULE_FIELD_METRICS[0],
        )
        self.entity_registry.add_entry(
            "sensor.module_capacity",
            capacity_unique_id,
        )

        hass = types.SimpleNamespace(
            entity_registry=self.entity_registry,
            data={
                "smart1_ems": {
                    "entry-1": {
                        "coordinator": types.SimpleNamespace(
                            data={"pv_strings": {}}
                        ),
                        "devices": [],
                        "discovery": types.SimpleNamespace(has_pv=False),
                        "inverters": [],
                        "module_fields": [module_field],
                        "buses": [],
                        "inverter_discovery_authoritative": False,
                        "module_field_discovery_authoritative": True,
                        "bus_discovery_authoritative": False,
                    }
                }
            },
        )

        asyncio.run(
            sensor_module.async_setup_entry(
                hass,
                types.SimpleNamespace(entry_id="entry-1"),
                lambda _entities: None,
            )
        )

        self.assertEqual(self.entity_registry.removed, [])

    def test_authoritative_discovery_preserves_current_topology(self) -> None:
        bus = bus_module.Smart1BusSystem(
            id="Bus2",
            number=2,
            configured="ok",
        )
        module_field = module_field_module.Smart1ModuleField(
            id="Modulfield_1",
            reference="1",
            tilt_degrees=23.0,
            azimuth_degrees=65.0,
        )
        inverter = inverter_module.Smart1Inverter(
            id="Inverter_B2_A1",
            bus=2,
            address=1,
            string_count=1,
            string_capacities_w=(5000.0,),
            string_module_fields=("1",),
        )
        current_ids = {
            sensor_module._bus_configuration_unique_id("entry-1", bus),
            sensor_module._inverter_temperature_unique_id(
                "entry-1",
                inverter,
            ),
        }
        current_ids.update(
            sensor_module._inverter_string_unique_id(
                "entry-1",
                inverter,
                1,
                metric,
            )
            for metric in sensor_module.INVERTER_STRING_METRICS
        )
        current_ids.update(
            sensor_module._module_field_unique_id(
                "entry-1",
                module_field,
                metric,
            )
            for metric in sensor_module.MODULE_FIELD_METRICS
        )
        for index, unique_id in enumerate(current_ids):
            self.entity_registry.add_entry(f"sensor.current_{index}", unique_id)

        coordinator = types.SimpleNamespace(
            data={
                "pv_strings": {
                    (2, 1, 1): inverter_module.Smart1PvStringSample(
                        bus=2,
                        address=1,
                        string_id=1,
                        timestamp="2026-08-05 12:05:00",
                        ac_power_w=1200.0,
                        dc_power_w=1300.0,
                        dc_voltage_v=500.0,
                        inverter_temperature_c=41.0,
                    )
                }
            }
        )
        hass = types.SimpleNamespace(
            device_registry=self.device_registry,
            entity_registry=self.entity_registry,
            data={
                "smart1_ems": {
                    "entry-1": {
                        "coordinator": coordinator,
                        "devices": [],
                        "discovery": types.SimpleNamespace(has_pv=False),
                        "inverters": [inverter],
                        "module_fields": [module_field],
                        "buses": [bus],
                        "inverter_discovery_authoritative": True,
                        "module_field_discovery_authoritative": True,
                        "bus_discovery_authoritative": True,
                    }
                }
            },
        )

        asyncio.run(
            sensor_module.async_setup_entry(
                hass,
                types.SimpleNamespace(entry_id="entry-1"),
                lambda _entities: None,
            )
        )

        self.assertEqual(self.entity_registry.removed, [])

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

    def test_failed_first_detail_request_preserves_registered_strings(
        self,
    ) -> None:
        inverter = inverter_module.Smart1Inverter(
            id="Inverter_B2_A1",
            bus=2,
            address=1,
            name="Energy Butler",
            string_count=1,
            string_capacities_w=(5000.0,),
            string_module_fields=("East",),
        )
        existing_unique_id = sensor_module._inverter_string_unique_id(
            "entry-1",
            inverter,
            2,
            sensor_module.INVERTER_STRING_METRICS[0],
        )
        self.entity_registry.add_entry(
            "sensor.existing_string_2",
            existing_unique_id,
        )
        coordinator = types.SimpleNamespace(
            data={
                "pv_strings": {},
                "pv_strings_authoritative": False,
            }
        )
        hass = types.SimpleNamespace(
            device_registry=self.device_registry,
            entity_registry=self.entity_registry,
            data={
                "smart1_ems": {
                    "entry-1": {
                        "coordinator": coordinator,
                        "devices": [],
                        "discovery": types.SimpleNamespace(has_pv=False),
                        "inverters": [inverter],
                        "inverter_discovery_authoritative": True,
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

        self.assertEqual(self.entity_registry.removed, [])
        string_2_entities = [
            entity
            for entity in added
            if isinstance(entity, sensor_module.Smart1InverterStringSensor)
            and entity._string_id == 2
        ]
        self.assertEqual(len(string_2_entities), 3)

        coordinator.data["pv_strings"] = {
            (2, 1, 2): inverter_module.Smart1PvStringSample(
                bus=2,
                address=1,
                string_id=2,
                timestamp="2026-08-05 12:05:00",
                ac_power_w=1200.0,
                dc_power_w=1300.0,
                dc_voltage_v=500.0,
                inverter_temperature_c=41.0,
            )
        }
        self.assertEqual(string_2_entities[0].native_value, 1200.0)
        self.assertTrue(string_2_entities[0].available)

    def test_successful_empty_detail_request_preserves_registered_strings(
        self,
    ) -> None:
        inverter = inverter_module.Smart1Inverter(
            id="Inverter_B2_A1",
            bus=2,
            address=1,
            name="Energy Butler",
            string_count=1,
            string_capacities_w=(5000.0,),
            string_module_fields=("East",),
        )
        existing_unique_id = sensor_module._inverter_string_unique_id(
            "entry-1",
            inverter,
            2,
            sensor_module.INVERTER_STRING_METRICS[0],
        )
        self.entity_registry.add_entry(
            "sensor.existing_string_2",
            existing_unique_id,
        )
        coordinator = types.SimpleNamespace(
            data={
                "pv_strings": {},
                "pv_strings_authoritative": False,
            }
        )
        hass = types.SimpleNamespace(
            device_registry=self.device_registry,
            entity_registry=self.entity_registry,
            data={
                "smart1_ems": {
                    "entry-1": {
                        "coordinator": coordinator,
                        "devices": [],
                        "discovery": types.SimpleNamespace(has_pv=False),
                        "inverters": [inverter],
                        "inverter_discovery_authoritative": True,
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

        self.assertEqual(self.entity_registry.removed, [])
        self.assertEqual(
            len(
                [
                    entity
                    for entity in added
                    if isinstance(
                        entity,
                        sensor_module.Smart1InverterStringSensor,
                    )
                    and entity._string_id == 2
                ]
            ),
            3,
        )

    def test_later_nonempty_detail_reloads_provisional_strings(self) -> None:
        inverter = inverter_module.Smart1Inverter(
            id="Inverter_B2_A1",
            bus=2,
            address=1,
            name="Energy Butler",
            string_count=1,
            string_capacities_w=(5000.0,),
            string_module_fields=("East",),
        )
        stale_unique_id = sensor_module._inverter_string_unique_id(
            "entry-1",
            inverter,
            2,
            sensor_module.INVERTER_STRING_METRICS[0],
        )
        self.entity_registry.add_entry(
            "sensor.stale_string_2",
            stale_unique_id,
        )

        class ListeningCoordinator:
            def __init__(self) -> None:
                self.data = {"pv_strings": {}}
                self.listener = None

            def async_add_listener(self, listener):
                self.listener = listener
                return lambda: None

        coordinator = ListeningCoordinator()
        reload_coroutines = []
        async_reload = AsyncMock(return_value=True)

        def async_create_task(coroutine, name):
            reload_coroutines.append((coroutine, name))

        entry = types.SimpleNamespace(
            entry_id="entry-1",
            async_on_unload=lambda _callback: None,
        )
        hass = types.SimpleNamespace(
            async_create_task=async_create_task,
            config_entries=types.SimpleNamespace(async_reload=async_reload),
            device_registry=self.device_registry,
            entity_registry=self.entity_registry,
            data={
                "smart1_ems": {
                    "entry-1": {
                        "coordinator": coordinator,
                        "devices": [],
                        "discovery": types.SimpleNamespace(has_pv=False),
                        "inverters": [inverter],
                        "inverter_discovery_authoritative": True,
                    }
                }
            },
        )

        asyncio.run(
            sensor_module.async_setup_entry(hass, entry, lambda _items: None)
        )
        self.assertEqual(self.entity_registry.removed, [])

        coordinator.data["pv_strings"] = {
            (2, 1, 1): inverter_module.Smart1PvStringSample(
                bus=2,
                address=1,
                string_id=1,
                timestamp="2026-08-05 12:05:00",
                ac_power_w=1000.0,
                dc_power_w=1100.0,
                dc_voltage_v=500.0,
                inverter_temperature_c=41.0,
            )
        }
        coordinator.listener()
        coordinator.listener()

        self.assertEqual(len(reload_coroutines), 1)
        reload, task_name = reload_coroutines[0]
        self.assertEqual(
            task_name,
            "smart1 EMS inverter string topology reload",
        )
        asyncio.run(reload)
        async_reload.assert_awaited_once_with("entry-1")

    def test_later_nonempty_detail_reloads_second_inverter_once(self) -> None:
        inverter_a = inverter_module.Smart1Inverter(
            id="Inverter_B2_A1",
            bus=2,
            address=1,
            name="Energy Butler A",
            string_count=1,
            string_capacities_w=(5000.0,),
            string_module_fields=("East",),
        )
        inverter_b = inverter_module.Smart1Inverter(
            id="Inverter_B2_A2",
            bus=2,
            address=2,
            name="Energy Butler B",
            string_count=1,
            string_capacities_w=(6000.0,),
            string_module_fields=("West",),
        )
        provisional_unique_id = sensor_module._inverter_string_unique_id(
            "entry-1",
            inverter_b,
            2,
            sensor_module.INVERTER_STRING_METRICS[0],
        )
        self.entity_registry.add_entry(
            "sensor.provisional_inverter_b_string_2",
            provisional_unique_id,
        )

        def sample(inverter, power: float):
            return inverter_module.Smart1PvStringSample(
                bus=inverter.bus,
                address=inverter.address,
                string_id=1,
                timestamp="2026-08-05 12:05:00",
                ac_power_w=power,
                dc_power_w=power + 100.0,
                dc_voltage_v=500.0,
                inverter_temperature_c=41.0,
            )

        class ListeningCoordinator:
            def __init__(self) -> None:
                self.data = {
                    "pv_strings": {
                        (2, 1, 1): sample(inverter_a, 1000.0),
                    }
                }
                self.listener = None

            def async_add_listener(self, listener):
                self.listener = listener
                return lambda: None

        coordinator = ListeningCoordinator()
        reload_coroutines = []
        async_reload = AsyncMock(return_value=True)

        def async_create_task(coroutine, name):
            reload_coroutines.append((coroutine, name))

        entry = types.SimpleNamespace(
            entry_id="entry-1",
            async_on_unload=lambda _callback: None,
        )
        hass = types.SimpleNamespace(
            async_create_task=async_create_task,
            config_entries=types.SimpleNamespace(async_reload=async_reload),
            device_registry=self.device_registry,
            entity_registry=self.entity_registry,
            data={
                "smart1_ems": {
                    "entry-1": {
                        "coordinator": coordinator,
                        "devices": [],
                        "discovery": types.SimpleNamespace(has_pv=False),
                        "inverters": [inverter_a, inverter_b],
                        "inverter_discovery_authoritative": True,
                    }
                }
            },
        )

        asyncio.run(
            sensor_module.async_setup_entry(hass, entry, lambda _items: None)
        )
        self.assertEqual(self.entity_registry.removed, [])
        self.assertEqual(reload_coroutines, [])

        coordinator.data["pv_strings"][(2, 2, 1)] = sample(
            inverter_b,
            1200.0,
        )
        coordinator.listener()
        coordinator.listener()

        self.assertEqual(len(reload_coroutines), 1)
        reload, task_name = reload_coroutines[0]
        self.assertEqual(
            task_name,
            "smart1 EMS inverter string topology reload",
        )
        asyncio.run(reload)
        async_reload.assert_awaited_once_with("entry-1")

    def test_partial_detail_request_preserves_registered_strings(
        self,
    ) -> None:
        inverter = inverter_module.Smart1Inverter(
            id="Inverter_B2_A1",
            bus=2,
            address=1,
            name="Energy Butler",
            string_count=1,
            string_capacities_w=(5000.0,),
            string_module_fields=("East",),
        )

        def sample(string_id: int, power: float):
            return inverter_module.Smart1PvStringSample(
                bus=2,
                address=1,
                string_id=string_id,
                timestamp="2026-08-05 12:05:00",
                ac_power_w=power,
                dc_power_w=power,
                dc_voltage_v=500.0,
                inverter_temperature_c=41.0,
            )

        existing_unique_id = sensor_module._inverter_string_unique_id(
            "entry-1",
            inverter,
            2,
            sensor_module.INVERTER_STRING_METRICS[0],
        )
        self.entity_registry.add_entry(
            "sensor.existing_string_2",
            existing_unique_id,
            name="Customized west string",
        )

        class ListeningCoordinator:
            def __init__(self) -> None:
                self.data = {
                    "pv_strings": {(2, 1, 1): sample(1, 1000.0)},
                    "pv_strings_authoritative": True,
                }
                self.listener = None

            def async_add_listener(self, listener):
                self.listener = listener
                return lambda: None

        coordinator = ListeningCoordinator()
        unload_callbacks = []
        entry = types.SimpleNamespace(
            entry_id="entry-1",
            async_on_unload=unload_callbacks.append,
        )
        hass = types.SimpleNamespace(
            device_registry=self.device_registry,
            entity_registry=self.entity_registry,
            data={
                "smart1_ems": {
                    "entry-1": {
                        "coordinator": coordinator,
                        "devices": [],
                        "discovery": types.SimpleNamespace(has_pv=False),
                        "inverters": [inverter],
                        "inverter_discovery_authoritative": True,
                    }
                }
            },
        )
        added = []

        asyncio.run(
            sensor_module.async_setup_entry(hass, entry, added.extend)
        )

        self.assertEqual(self.entity_registry.removed, [])
        string_2_entities = [
            entity
            for entity in added
            if isinstance(entity, sensor_module.Smart1InverterStringSensor)
            and entity._string_id == 2
        ]
        self.assertEqual(len(string_2_entities), 3)
        self.assertFalse(string_2_entities[0].available)

        coordinator.data["pv_strings"][(2, 1, 2)] = sample(2, 1200.0)
        coordinator.listener()

        self.assertEqual(
            len(
                [
                    entity
                    for entity in added
                    if isinstance(
                        entity,
                        sensor_module.Smart1InverterStringSensor,
                    )
                    and entity._string_id == 2
                ]
            ),
            3,
        )
        self.assertEqual(string_2_entities[0].native_value, 1200.0)
        self.assertTrue(string_2_entities[0].available)

    def test_later_detail_poll_adds_string_missing_from_partial_setup(
        self,
    ) -> None:
        inverter = inverter_module.Smart1Inverter(
            id="Inverter_B2_A1",
            bus=2,
            address=1,
            name="Energy Butler",
            string_count=1,
            string_capacities_w=(5000.0,),
            string_module_fields=("East",),
        )

        def sample(string_id: int, power: float):
            return inverter_module.Smart1PvStringSample(
                bus=2,
                address=1,
                string_id=string_id,
                timestamp="2026-08-05 12:05:00",
                ac_power_w=power,
                dc_power_w=power,
                dc_voltage_v=500.0,
                inverter_temperature_c=41.0,
            )

        class ListeningCoordinator:
            def __init__(self) -> None:
                self.data = {
                    "pv_strings": {(2, 1, 1): sample(1, 1000.0)},
                    "pv_strings_authoritative": True,
                }
                self.listener = None

            def async_add_listener(self, listener):
                self.listener = listener
                return lambda: None

        coordinator = ListeningCoordinator()
        unload_callbacks = []
        entry = types.SimpleNamespace(
            entry_id="entry-1",
            async_on_unload=unload_callbacks.append,
        )
        hass = types.SimpleNamespace(
            device_registry=self.device_registry,
            entity_registry=self.entity_registry,
            data={
                "smart1_ems": {
                    "entry-1": {
                        "coordinator": coordinator,
                        "devices": [],
                        "discovery": types.SimpleNamespace(has_pv=False),
                        "inverters": [inverter],
                        "inverter_discovery_authoritative": True,
                    }
                }
            },
        )
        added = []

        asyncio.run(
            sensor_module.async_setup_entry(hass, entry, added.extend)
        )
        self.assertEqual(len(unload_callbacks), 1)
        self.assertIsNotNone(coordinator.listener)
        self.assertFalse(
            any(
                isinstance(
                    entity,
                    sensor_module.Smart1InverterStringSensor,
                )
                and entity._string_id == 2
                for entity in added
            )
        )

        coordinator.data["pv_strings"][(2, 1, 2)] = sample(2, 1200.0)
        coordinator.listener()

        string_2_entities = [
            entity
            for entity in added
            if isinstance(entity, sensor_module.Smart1InverterStringSensor)
            and entity._string_id == 2
        ]
        self.assertEqual(len(string_2_entities), 3)
        self.assertEqual(string_2_entities[0].native_value, 1200.0)


if __name__ == "__main__":
    unittest.main()
