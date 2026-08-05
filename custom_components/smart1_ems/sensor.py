from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    DEGREE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .classifier import Smart1Category
from .const import DOMAIN
from .entity_mapper import get_entity_descriptions
from .inverter import Smart1Inverter
from .module_field import Smart1ModuleField

PV_DEVICE_NAME = "Smart1 Photovoltaik"


@dataclass(frozen=True, slots=True)
class Smart1InverterMetric:
    """One documented value exposed for every inverter string."""

    key: str
    translation_key: str
    device_class: SensorDeviceClass
    unit: str
    precision: int


INVERTER_STRING_METRICS = (
    Smart1InverterMetric(
        key="ac_power_w",
        translation_key="inverter_string_ac_power",
        device_class=SensorDeviceClass.POWER,
        unit=UnitOfPower.WATT,
        precision=0,
    ),
    Smart1InverterMetric(
        key="dc_power_w",
        translation_key="inverter_string_dc_power",
        device_class=SensorDeviceClass.POWER,
        unit=UnitOfPower.WATT,
        precision=0,
    ),
    Smart1InverterMetric(
        key="dc_voltage_v",
        translation_key="inverter_string_dc_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        unit=UnitOfElectricPotential.VOLT,
        precision=1,
    ),
)


@dataclass(frozen=True, slots=True)
class Smart1ModuleFieldMetric:
    """One static value exposed for every documented PV module field."""

    key: str
    translation_key: str
    device_class: SensorDeviceClass | None
    unit: str
    precision: int


MODULE_FIELD_METRICS = (
    Smart1ModuleFieldMetric(
        key="installed_capacity_w",
        translation_key="module_field_installed_capacity",
        device_class=SensorDeviceClass.POWER,
        unit=UnitOfPower.WATT,
        precision=0,
    ),
    Smart1ModuleFieldMetric(
        key="azimuth_degrees",
        translation_key="module_field_azimuth",
        device_class=None,
        unit=DEGREE,
        precision=1,
    ),
    Smart1ModuleFieldMetric(
        key="tilt_degrees",
        translation_key="module_field_tilt",
        device_class=None,
        unit=DEGREE,
        precision=1,
    ),
)


def _inverter_string_unique_id(
    entry_id: str,
    inverter: Smart1Inverter,
    string_id: int,
    metric: Smart1InverterMetric,
) -> str:
    """Return the stable unique ID for one inverter-string metric."""
    return (
        f"smart1_{entry_id}_inverter_{inverter.bus}_{inverter.address}_"
        f"string_{string_id}_{metric.key}"
    )


def _device_category(category: Smart1Category) -> Smart1Category:
    """Map measurement roles to the logical EMS device."""
    if category in {
        Smart1Category.CONSUMPTION,
        Smart1Category.TEMPERATURE,
        Smart1Category.WEATHER,
        Smart1Category.DIAGNOSTIC,
    }:
        return Smart1Category.OTHER

    return category


def _device_identifier(
    entry_id: str,
    category: Smart1Category,
) -> tuple[str, str]:
    """Return a stable Home Assistant device identifier."""
    device_category = _device_category(category)
    return (DOMAIN, f"{entry_id}:{device_category.value}")


def _pv_device_info(entry_id: str) -> dict:
    """Return device registry information for the installation PV system."""
    return {
        "identifiers": {
            _device_identifier(entry_id, Smart1Category.PV),
        },
        "name": PV_DEVICE_NAME,
        "manufacturer": "smart1",
        "model": "Smart1 EMS",
    }


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    devices = hass.data[DOMAIN][entry.entry_id]["devices"]
    discovery = hass.data[DOMAIN][entry.entry_id]["discovery"]
    inverters = hass.data[DOMAIN][entry.entry_id].get("inverters", [])
    module_fields = hass.data[DOMAIN][entry.entry_id].get("module_fields", [])

    entities = []

    for device in devices:
        for description in get_entity_descriptions(device):
            entities.append(
                Smart1Sensor(coordinator, entry.entry_id, device, description)
            )

    if discovery.has_pv:
        entities.append(Smart1PvEnergySensor(coordinator, entry.entry_id))

    for module_field in module_fields:
        for metric in MODULE_FIELD_METRICS:
            value = _module_field_metric_value(
                module_field,
                inverters,
                metric,
            )
            if value is not None:
                entities.append(
                    Smart1ModuleFieldSensor(
                        entry.entry_id,
                        module_field,
                        metric,
                        value,
                    )
                )

    pv_strings = coordinator.data.get("pv_strings", {})
    entity_registry = er.async_get(hass)
    for inverter in inverters:
        detailed_string_ids = {
            string_id
            for (bus, address, string_id), sample in pv_strings.items()
            if (bus, address) == inverter.key
            and string_id > 0
            and sample.has_measurement
        }
        # The detailed endpoint reflects the strings actually reported by the
        # inverter. Prefer it over metadata because some portals assign module
        # fields to unused inputs. Metadata remains the discovery fallback when
        # no detailed rows have been received yet.
        string_ids = (
            detailed_string_ids
            if detailed_string_ids
            else set(inverter.active_string_ids)
        )
        for string_id in set(inverter.string_ids) - string_ids:
            for metric in INVERTER_STRING_METRICS:
                entity_id = entity_registry.async_get_entity_id(
                    "sensor",
                    DOMAIN,
                    _inverter_string_unique_id(
                        entry.entry_id,
                        inverter,
                        string_id,
                        metric,
                    ),
                )
                if entity_id is not None:
                    entity_registry.async_remove(entity_id)

        for string_id in sorted(string_ids):
            for metric in INVERTER_STRING_METRICS:
                entities.append(
                    Smart1InverterStringSensor(
                        coordinator,
                        entry.entry_id,
                        inverter,
                        string_id,
                        metric,
                    )
                )
        entities.append(
            Smart1InverterTemperatureSensor(
                coordinator,
                entry.entry_id,
                inverter,
            )
        )

    async_add_entities(entities)


class Smart1Sensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry_id, device, description):
        super().__init__(coordinator)

        self._linear_id = device.id
        self._name = device.name
        self._type = device.type
        self._source = device.source
        self._description = description

        category = description.category

        device_names = {
            Smart1Category.PV: PV_DEVICE_NAME,
            Smart1Category.GRID: "Smart1 Netz",
            Smart1Category.BATTERY: "Smart1 Batterie",
            Smart1Category.WALLBOX: "Smart1 Wallbox",
            Smart1Category.HEAT_PUMP: "Smart1 Wärmepumpe",
            Smart1Category.ENERGY_HEATER: "Smart1 Zusatzheizung",
            Smart1Category.CONSUMPTION: "Smart1 EMS",
            Smart1Category.TEMPERATURE: "Smart1 EMS",
            Smart1Category.WEATHER: "Smart1 EMS",
            Smart1Category.DIAGNOSTIC: "Smart1 EMS",
            Smart1Category.OTHER: "Smart1 EMS",
        }

        device_category = _device_category(category)

        self._attr_device_info = {
            "identifiers": {_device_identifier(entry_id, category)},
            "name": device_names.get(device_category, "Smart1 EMS"),
            "manufacturer": "smart1",
            "model": "Smart1 EMS",
        }

        self._attr_unique_id = (
            f"smart1_{entry_id}_{self._linear_id}_{description.value_source}"
        )
        self._attr_name = f"{self._name}{description.suffix}"
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement
        self._attr_icon = description.icon
        self._attr_entity_category = description.entity_category

    @property
    def native_value(self):
        return self.coordinator.data.get(
            self._description.value_source, {}
        ).get(self._linear_id)

    @property
    def extra_state_attributes(self):
        return {
            "linear_id": self._linear_id,
            "smart1_type": self._type,
            "source": self._source,
            "smart1_category": str(self._description.category),
            "value_source": self._description.value_source,
        }


class Smart1PvEnergySensor(CoordinatorEntity, SensorEntity):
    """Cumulative PV production for the current day."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3
    _attr_has_entity_name = True
    _attr_translation_key = "pv_energy_today"

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"smart1_{entry_id}_pv_energy_today"
        self._attr_device_info = _pv_device_info(entry_id)

    @property
    def native_value(self):
        """Return today's PV production in kWh."""
        return self.coordinator.data.get("pv_energy_today")

    @property
    def available(self):
        """Return whether cumulative PV production is available."""
        return super().available and self.native_value is not None


def _module_field_metric_value(
    module_field: Smart1ModuleField,
    inverters: list[Smart1Inverter],
    metric: Smart1ModuleFieldMetric,
) -> float | None:
    """Return one static module-field value."""
    if metric.key == "installed_capacity_w":
        return module_field.installed_capacity_w(inverters)
    return getattr(module_field, metric.key)


class Smart1ModuleFieldSensor(SensorEntity):
    """Static configuration for one documented PV module field."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        entry_id: str,
        module_field: Smart1ModuleField,
        metric: Smart1ModuleFieldMetric,
        value: float,
    ) -> None:
        self._module_field = module_field
        self._attr_unique_id = (
            f"smart1_{entry_id}_module_field_{module_field.reference}_"
            f"{metric.key}"
        )
        self._attr_device_info = _pv_device_info(entry_id)
        self._attr_translation_key = metric.translation_key
        self._attr_translation_placeholders = {
            "module_field": module_field.name or module_field.reference,
        }
        self._attr_native_value = value
        self._attr_native_unit_of_measurement = metric.unit
        self._attr_suggested_display_precision = metric.precision
        if metric.device_class is not None:
            self._attr_device_class = metric.device_class

    @property
    def extra_state_attributes(self):
        """Return documented module-field configuration attributes."""
        return {
            "module_field_reference": self._module_field.reference,
            "shadow_from": self._module_field.shadow_from or None,
            "shadow_until": self._module_field.shadow_until or None,
            "monitoring": self._module_field.monitoring or None,
            "configured": self._module_field.configured or None,
        }


def _inverter_device_info(
    entry_id: str,
    inverter: Smart1Inverter,
) -> dict:
    """Return device registry information for one physical inverter."""
    device_info = {
        "identifiers": {(DOMAIN, f"{entry_id}:inverter:{inverter.id}")},
        "name": inverter.name
        or f"Smart1 Inverter B{inverter.bus} A{inverter.address}",
        "manufacturer": inverter.manufacturer or "smart1",
        "model": inverter.model or "Inverter",
        "via_device": _device_identifier(entry_id, Smart1Category.OTHER),
    }
    if inverter.serial_number:
        device_info["serial_number"] = inverter.serial_number
    return device_info


class Smart1InverterStringSensor(CoordinatorEntity, SensorEntity):
    """A documented five-minute measurement for one inverter string."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator,
        entry_id: str,
        inverter: Smart1Inverter,
        string_id: int,
        metric: Smart1InverterMetric,
    ) -> None:
        super().__init__(coordinator)
        self._inverter = inverter
        self._string_id = string_id
        self._metric = metric
        self._attr_unique_id = _inverter_string_unique_id(
            entry_id,
            inverter,
            string_id,
            metric,
        )
        self._attr_device_info = _inverter_device_info(entry_id, inverter)
        self._attr_translation_key = metric.translation_key
        self._attr_translation_placeholders = {"string_id": str(string_id)}
        self._attr_device_class = metric.device_class
        self._attr_native_unit_of_measurement = metric.unit
        self._attr_suggested_display_precision = metric.precision

    @property
    def native_value(self):
        """Return the latest value for this inverter string."""
        sample = self.coordinator.data.get("pv_strings", {}).get(
            (*self._inverter.key, self._string_id)
        )
        return getattr(sample, self._metric.key) if sample else None

    @property
    def available(self):
        """Return whether the optional detailed endpoint has this value."""
        return super().available and self.native_value is not None

    @property
    def extra_state_attributes(self):
        """Return static string configuration without volatile metadata."""
        index = self._string_id - 1
        capacity = (
            self._inverter.string_capacities_w[index]
            if index < len(self._inverter.string_capacities_w)
            else None
        )
        module_field = (
            self._inverter.string_module_fields[index]
            if index < len(self._inverter.string_module_fields)
            else ""
        )
        return {
            "string_id": self._string_id,
            "configured_capacity_w": capacity,
            "module_field": module_field or None,
        }


class Smart1InverterTemperatureSensor(CoordinatorEntity, SensorEntity):
    """Latest documented temperature reported for one inverter."""

    _attr_has_entity_name = True
    _attr_translation_key = "inverter_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator,
        entry_id: str,
        inverter: Smart1Inverter,
    ) -> None:
        super().__init__(coordinator)
        self._inverter = inverter
        self._attr_unique_id = (
            f"smart1_{entry_id}_inverter_{inverter.bus}_{inverter.address}_"
            "temperature"
        )
        self._attr_device_info = _inverter_device_info(entry_id, inverter)

    @property
    def native_value(self):
        """Return the newest available temperature across inverter strings."""
        samples = [
            sample
            for (bus, address, _string_id), sample in self.coordinator.data.get(
                "pv_strings", {}
            ).items()
            if (bus, address) == self._inverter.key
            and sample.inverter_temperature_c is not None
        ]
        if not samples:
            return None
        return max(samples, key=lambda sample: sample.timestamp).inverter_temperature_c

    @property
    def available(self):
        """Return whether the optional detailed endpoint has a temperature."""
        return super().available and self.native_value is not None
