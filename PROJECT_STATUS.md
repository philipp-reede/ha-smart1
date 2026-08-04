# Project status

## Working

- Custom integration loads through Home Assistant
- Config flow asks for API key
- `/plants` is queried automatically
- A single plant is selected automatically
- Sensors and counters are discovered automatically
- Live values are fetched through one filtered linear request
- DataUpdateCoordinator updates values periodically
- Points are represented as `Smart1Point`
- Interface parser exists
- Classification exists
- Entities are grouped in Home Assistant devices
- Device registry identifiers use valid `(DOMAIN, identifier)` pairs
- PV production is read from the documented photovoltaics cumulative endpoint
- PV production is aggregated once per inverter and exposed in kWh
- A failure of optional PV cumulative data no longer blocks live values

## Known problems

- Device assignment is still too coarse
- Heat pump detection needs:
  - `heatpump` in interface
  - `WP` in point name
- Heating element needs its own logical device
  - M-TEC product name: `Energy Heater`
  - generic term: `Heizstab`
- Consumption must not be represented as a device
- Temperature is a property and should be assigned to its source device
- Device-specific energy data for wallbox, heat pump, heating element, grid,
  and battery remains undocumented and must not be inferred

## Current code behavior

The coordinator currently stores approximately:

```python
{
    "live": {...},
    "pv_energy_today": 12.345,
}
```

`pv_energy_today` is `None` when the optional PV cumulative endpoint is not
available. Live values continue updating in that case.
