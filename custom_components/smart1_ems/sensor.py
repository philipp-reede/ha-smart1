from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re

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
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .bus import Smart1BusSystem
from .classifier import Smart1Category
from .const import DOMAIN
from .entity_mapper import get_entity_descriptions
from .inverter import Smart1Inverter, pv_timestamp_sort_key
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


def _inverter_temperature_unique_id(
    entry_id: str,
    inverter: Smart1Inverter,
) -> str:
    """Return the stable unique ID for one inverter temperature sensor."""
    return (
        f"smart1_{entry_id}_inverter_{inverter.bus}_{inverter.address}_"
        "temperature"
    )


def _module_field_unique_id(
    entry_id: str,
    module_field: Smart1ModuleField,
    metric: Smart1ModuleFieldMetric,
) -> str:
    """Return the stable unique ID for one module-field metric."""
    return (
        f"smart1_{entry_id}_module_field_{module_field.reference}_"
        f"{metric.key}"
    )


def _bus_configuration_unique_id(
    entry_id: str,
    bus: Smart1BusSystem,
) -> str:
    """Return the stable unique ID for one inverter-bus sensor."""
    return f"smart1_{entry_id}_inverter_bus_{bus.number}_configuration"


def _topology_entity_kind(unique_id: str, entry_id: str) -> str | None:
    """Classify only unique IDs owned by optional topology discovery."""
    prefix = f"smart1_{entry_id}_"
    if not unique_id.startswith(prefix):
        return None

    topology_id = unique_id[len(prefix) :]
    inverter_metrics = "|".join(
        re.escape(metric.key) for metric in INVERTER_STRING_METRICS
    )
    if re.fullmatch(
        rf"inverter_\d+_\d+_(?:temperature|string_\d+_(?:{inverter_metrics}))",
        topology_id,
    ):
        return "inverter"

    if re.fullmatch(r"inverter_bus_\d+_configuration", topology_id):
        return "bus"

    for metric in MODULE_FIELD_METRICS:
        marker = f"module_field_"
        suffix = f"_{metric.key}"
        if (
            topology_id.startswith(marker)
            and len(topology_id) > len(marker) + len(suffix)
            and topology_id.endswith(suffix)
        ):
            return (
                "module_field_capacity"
                if metric.key == "installed_capacity_w"
                else "module_field"
            )

    return None


def _remove_stale_topology_entities(
    entity_registry,
    entry_id: str,
    *,
    expected_inverter_ids: set[str],
    expected_module_field_ids: set[str],
    current_module_field_ids: set[str],
    expected_bus_ids: set[str],
    inverter_discovery_authoritative: bool,
    module_field_discovery_authoritative: bool,
    bus_discovery_authoritative: bool,
) -> None:
    """Remove stale optional topology after authoritative discovery only."""
    for registry_entry in er.async_entries_for_config_entry(
        entity_registry,
        entry_id,
    ):
        if (
            registry_entry.domain != "sensor"
            or registry_entry.platform != DOMAIN
        ):
            continue

        unique_id = registry_entry.unique_id
        kind = _topology_entity_kind(unique_id, entry_id)
        if kind == "inverter":
            should_remove = (
                inverter_discovery_authoritative
                and unique_id not in expected_inverter_ids
            )
        elif kind == "bus":
            should_remove = (
                bus_discovery_authoritative
                and unique_id not in expected_bus_ids
            )
        elif kind == "module_field":
            should_remove = (
                module_field_discovery_authoritative
                and unique_id not in expected_module_field_ids
            )
        elif kind == "module_field_capacity":
            # Capacity comes from the inverter-to-module-field assignment.
            # A missing module field is authoritative on its own, while a
            # still-present field needs both topology endpoints before an
            # absent calculated capacity may be treated as a removal.
            should_remove = (
                module_field_discovery_authoritative
                and unique_id not in expected_module_field_ids
                and (
                    unique_id not in current_module_field_ids
                    or inverter_discovery_authoritative
                )
            )
        else:
            should_remove = False

        if should_remove:
            entity_registry.async_remove(registry_entry.entity_id)


def _registered_inverter_string_ids(
    entity_registry,
    entry_id: str,
    inverter: Smart1Inverter,
    *,
    customized_only: bool = False,
) -> set[int]:
    """Return string IDs already registered for one current inverter."""
    metrics = "|".join(
        re.escape(metric.key) for metric in INVERTER_STRING_METRICS
    )
    pattern = re.compile(
        rf"^smart1_{re.escape(entry_id)}_inverter_{inverter.bus}_"
        rf"{inverter.address}_string_(?P<string_id>\d+)_(?:{metrics})$"
    )
    string_ids: set[int] = set()
    for registry_entry in er.async_entries_for_config_entry(
        entity_registry,
        entry_id,
    ):
        if (
            registry_entry.domain != "sensor"
            or registry_entry.platform != DOMAIN
        ):
            continue
        match = pattern.fullmatch(registry_entry.unique_id)
        if match is None or int(match.group("string_id")) <= 0:
            continue
        if customized_only and not _registry_entry_is_user_customized(
            registry_entry
        ):
            continue
        string_ids.add(int(match.group("string_id")))
    return string_ids


def _registry_entry_is_user_customized(registry_entry) -> bool:
    """Return whether removing an entry would discard user configuration."""
    for attribute in (
        "area_id",
        "categories",
        "device_class",
        "icon",
        "labels",
        "name",
    ):
        value = getattr(registry_entry, attribute, None)
        if value not in (None, "", (), [], {}, set(), frozenset()):
            return True

    aliases = getattr(registry_entry, "aliases", ()) or ()
    if isinstance(aliases, str):
        aliases = (aliases,)
    computed_name = getattr(er, "COMPUTED_NAME", None)
    if any(alias not in (None, computed_name) for alias in aliases):
        return True

    options = getattr(registry_entry, "options", None)
    sensor_options = (
        options.get("sensor", {}) if isinstance(options, Mapping) else {}
    )
    if isinstance(sensor_options, Mapping) and any(
        key in sensor_options
        for key in ("display_precision", "unit_of_measurement")
    ):
        # These are explicit sensor presentation choices. Other option
        # domains can be populated automatically (for example voice-assistant
        # exposure), while ``suggested_*`` sensor options come from the
        # integration itself; neither is proof that the user customized this
        # PV string.
        return True

    for attribute in ("disabled_by", "hidden_by"):
        value = getattr(registry_entry, attribute, None)
        if (
            value is not None
            and str(value).lower().rsplit(".", 1)[-1] == "user"
        ):
            return True
    return False


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


def _ems_device_info(entry_id: str) -> dict:
    """Return device registry information for the smart1 EMS hub."""
    return {
        "identifiers": {
            _device_identifier(entry_id, Smart1Category.OTHER),
        },
        "name": "Smart1 EMS",
        "manufacturer": "smart1",
        "model": "Smart1 EMS",
    }


def _supports_via_device_id() -> bool:
    """Return whether this Home Assistant version supports via_device_id."""
    return "via_device_id" in getattr(
        dr.DeviceInfo,
        "__annotations__",
        {},
    )


async def async_setup_entry(hass, entry, async_add_entities):
    runtime_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = runtime_data["coordinator"]
    devices = runtime_data["devices"]
    discovery = runtime_data["discovery"]
    inverters = runtime_data.get("inverters", [])
    module_fields = runtime_data.get("module_fields", [])
    buses = runtime_data.get("buses", [])
    inverter_discovery_authoritative = runtime_data.get(
        "inverter_discovery_authoritative",
        False,
    )
    module_field_discovery_authoritative = runtime_data.get(
        "module_field_discovery_authoritative",
        False,
    )
    bus_discovery_authoritative = runtime_data.get(
        "bus_discovery_authoritative",
        False,
    )

    ems_device_id = None
    if inverters:
        # Register the parent before its inverter children. Current Home
        # Assistant versions require its registry ID; older supported versions
        # still resolve the legacy identifier tuple.
        ems_device_id = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            **_ems_device_info(entry.entry_id),
        ).id

    entities = []
    expected_inverter_ids: set[str] = set()
    expected_module_field_ids: set[str] = set()
    current_module_field_ids: set[str] = set()
    expected_bus_ids: set[str] = set()

    for device in devices:
        for description in get_entity_descriptions(device):
            entities.append(
                Smart1Sensor(coordinator, entry.entry_id, device, description)
            )

    if discovery.has_pv:
        entities.append(Smart1PvEnergySensor(coordinator, entry.entry_id))

    for bus in buses:
        expected_bus_ids.add(_bus_configuration_unique_id(entry.entry_id, bus))
        entities.append(Smart1BusConfigurationSensor(entry.entry_id, bus))

    for module_field in module_fields:
        for metric in MODULE_FIELD_METRICS:
            unique_id = _module_field_unique_id(
                entry.entry_id,
                module_field,
                metric,
            )
            current_module_field_ids.add(unique_id)
            value = _module_field_metric_value(
                module_field,
                inverters,
                metric,
            )
            if value is not None:
                expected_module_field_ids.add(unique_id)
                entities.append(
                    Smart1ModuleFieldSensor(
                        entry.entry_id,
                        module_field,
                        metric,
                        value,
                    )
                )

    entity_registry = er.async_get(hass)
    pv_strings = coordinator.data.get("pv_strings", {})
    initially_reported_inverters = {
        (bus, address)
        for (bus, address, string_id), sample in pv_strings.items()
        if string_id > 0 and sample.has_measurement
    }
    known_string_ids: dict[tuple[int, int], set[int]] = {}
    inverters_by_key = {inverter.key: inverter for inverter in inverters}
    for inverter in inverters:
        assert ems_device_id is not None
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
        if detailed_string_ids:
            string_ids = detailed_string_ids
            # A non-empty detail response is the best available active-string
            # evidence and deliberately filters stale metadata/registry rows.
            # Preserve only omitted strings carrying explicit user registry
            # customizations; removing those would irreversibly lose settings
            # if this response was merely partial.
            string_ids.update(
                _registered_inverter_string_ids(
                    entity_registry,
                    entry.entry_id,
                    inverter,
                    customized_only=True,
                )
            )
        else:
            string_ids = set(inverter.active_string_ids)
            # A failed or empty first request contains no active-string
            # evidence. Keep every registered string provisionally until a
            # later non-empty response can distinguish active and stale rows.
            string_ids.update(
                _registered_inverter_string_ids(
                    entity_registry,
                    entry.entry_id,
                    inverter,
                )
            )
        known_string_ids[inverter.key] = set(string_ids)
        for string_id in sorted(string_ids):
            for metric in INVERTER_STRING_METRICS:
                expected_inverter_ids.add(
                    _inverter_string_unique_id(
                        entry.entry_id,
                        inverter,
                        string_id,
                        metric,
                    )
                )
                entities.append(
                    Smart1InverterStringSensor(
                        coordinator,
                        entry.entry_id,
                        inverter,
                        string_id,
                        metric,
                        ems_device_id,
                    )
                )
        expected_inverter_ids.add(
            _inverter_temperature_unique_id(entry.entry_id, inverter)
        )
        entities.append(
            Smart1InverterTemperatureSensor(
                coordinator,
                entry.entry_id,
                inverter,
                ems_device_id,
            )
        )

    _remove_stale_topology_entities(
        entity_registry,
        entry.entry_id,
        expected_inverter_ids=expected_inverter_ids,
        expected_module_field_ids=expected_module_field_ids,
        current_module_field_ids=current_module_field_ids,
        expected_bus_ids=expected_bus_ids,
        inverter_discovery_authoritative=inverter_discovery_authoritative,
        module_field_discovery_authoritative=(
            module_field_discovery_authoritative
        ),
        bus_discovery_authoritative=bus_discovery_authoritative,
    )

    async_add_entities(entities)

    add_listener = getattr(coordinator, "async_add_listener", None)
    if inverters and callable(add_listener):
        detail_reload_requested = False

        def _add_new_reported_strings() -> None:
            """Add strings first observed after platform setup."""
            nonlocal detail_reload_requested
            current_strings = coordinator.data.get("pv_strings", {})
            if (
                not detail_reload_requested
                and any(
                    (bus, address) not in initially_reported_inverters
                    and string_id > 0
                    and sample.has_measurement
                    for (bus, address, string_id), sample in (
                        current_strings.items()
                    )
                )
            ):
                # Provisional registered strings were retained because setup
                # had no active-string evidence. Reload once when the first
                # non-empty response arrives so the normal authoritative
                # filter can remove stale uncustomized registry entries.
                detail_reload_requested = True
                hass.async_create_task(
                    hass.config_entries.async_reload(entry.entry_id),
                    "smart1 EMS inverter string topology reload",
                )
                return

            new_entities = []
            for (bus, address, string_id), sample in current_strings.items():
                inverter = inverters_by_key.get((bus, address))
                if (
                    inverter is None
                    or string_id <= 0
                    or not sample.has_measurement
                    or string_id
                    in known_string_ids.setdefault(inverter.key, set())
                ):
                    continue
                known_string_ids[inverter.key].add(string_id)
                for metric in INVERTER_STRING_METRICS:
                    new_entities.append(
                        Smart1InverterStringSensor(
                            coordinator,
                            entry.entry_id,
                            inverter,
                            string_id,
                            metric,
                            ems_device_id,
                        )
                    )
            if new_entities:
                async_add_entities(new_entities)

        entry.async_on_unload(add_listener(_add_new_reported_strings))


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


class Smart1BusConfigurationSensor(SensorEntity):
    """Static configuration for one documented inverter bus."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "inverter_bus_configuration"
    _attr_icon = "mdi:transit-connection-variant"

    def __init__(self, entry_id: str, bus: Smart1BusSystem) -> None:
        self._bus = bus
        self._attr_unique_id = _bus_configuration_unique_id(entry_id, bus)
        self._attr_device_info = _ems_device_info(entry_id)
        self._attr_translation_placeholders = {
            "bus": str(bus.number),
        }
        self._attr_native_value = bus.configured or "configured"

    @property
    def extra_state_attributes(self):
        """Return documented inverter-bus manufacturer configuration."""
        return {
            "bus_number": self._bus.number,
            "manufacturers": list(self._bus.manufacturers),
            "documented_manufacturer_count": (
                self._bus.documented_manufacturer_count
            ),
        }


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
        self._attr_unique_id = _module_field_unique_id(
            entry_id,
            module_field,
            metric,
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
    via_device_id: str,
) -> dict:
    """Return device registry information for one physical inverter."""
    device_info = {
        "identifiers": {(DOMAIN, f"{entry_id}:inverter:{inverter.id}")},
        "name": inverter.name
        or f"Smart1 Inverter B{inverter.bus} A{inverter.address}",
        "manufacturer": inverter.manufacturer or "smart1",
        "model": inverter.model or "Inverter",
    }
    if _supports_via_device_id():
        device_info["via_device_id"] = via_device_id
    else:
        # Home Assistant 2026.7 and earlier do not yet accept via_device_id.
        device_info["via_device"] = _device_identifier(
            entry_id,
            Smart1Category.OTHER,
        )
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
        via_device_id: str,
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
        self._attr_device_info = _inverter_device_info(
            entry_id,
            inverter,
            via_device_id,
        )
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
        via_device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._inverter = inverter
        self._attr_unique_id = _inverter_temperature_unique_id(
            entry_id,
            inverter,
        )
        self._attr_device_info = _inverter_device_info(
            entry_id,
            inverter,
            via_device_id,
        )

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
        return max(
            samples,
            key=lambda sample: pv_timestamp_sort_key(sample.timestamp),
        ).inverter_temperature_c

    @property
    def available(self):
        """Return whether the optional detailed endpoint has a temperature."""
        return super().available and self.native_value is not None
