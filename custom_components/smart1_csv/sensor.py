from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .classifier import Smart1Category
from .const import DOMAIN
from .entity_mapper import get_entity_descriptions

PV_DEVICE_NAME = "Smart1 Photovoltaik"


def _device_identifier(
    entry_id: str,
    category: Smart1Category,
) -> tuple[str, str]:
    """Return a stable Home Assistant device identifier."""
    return (DOMAIN, f"{entry_id}:{category.value}")


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    devices = hass.data[DOMAIN][entry.entry_id]["devices"]
    discovery = hass.data[DOMAIN][entry.entry_id]["discovery"]

    entities = []

    for device in devices:
        for description in get_entity_descriptions(device):
            entities.append(
                Smart1Sensor(coordinator, entry.entry_id, device, description)
            )

    if discovery.has_pv:
        entities.append(Smart1PvEnergySensor(coordinator, entry.entry_id))

    async_add_entities(entities)


class Smart1Sensor(CoordinatorEntity, SensorEntity):
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
            Smart1Category.CONSUMPTION: "Smart1 EMS",
            Smart1Category.TEMPERATURE: "Smart1 EMS",
            Smart1Category.WEATHER: "Smart1 EMS",
            Smart1Category.DIAGNOSTIC: "Smart1 EMS",
            Smart1Category.OTHER: "Smart1 EMS",
        }

        self._attr_device_info = {
            "identifiers": {_device_identifier(entry_id, category)},
            "name": device_names.get(category, "Smart1 EMS"),
            "manufacturer": "smart1",
            "model": "Smart1 EMS",
        }

        self._attr_unique_id = (
            f"smart1_{entry_id}_{self._linear_id}_{description.value_source}"
        )
        self._attr_name = f"Smart1 {self._name}{description.suffix}"
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
    _attr_name = "Smart1 PV Energy Today"

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"smart1_{entry_id}_pv_energy_today"
        self._attr_device_info = {
            "identifiers": {
                _device_identifier(entry_id, Smart1Category.PV),
            },
            "name": PV_DEVICE_NAME,
            "manufacturer": "smart1",
            "model": "Smart1 EMS",
        }

    @property
    def native_value(self):
        """Return today's PV production in kWh."""
        return self.coordinator.data.get("pv_energy_today")

    @property
    def available(self):
        """Return whether cumulative PV production is available."""
        return super().available and self.native_value is not None
