from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .classifier import Smart1Category
from .const import DOMAIN
from .entity_mapper import get_entity_descriptions


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    devices = hass.data[DOMAIN][entry.entry_id]["devices"]

    entities = []

    for device in devices:
        for description in get_entity_descriptions(device):
            entities.append(
                Smart1Sensor(coordinator, entry.entry_id, device, description)
            )

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
            Smart1Category.PV: "Smart1 Photovoltaik",
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
            "identifiers": {
                ("smart1_csv", entry_id, device_names.get(category, "Smart1 EMS")),
            },
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