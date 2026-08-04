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
- Automatically generated `Today` entities from `linear/cumulative`
  returned unknown values
- Current installation returned:
  `Errorcode 404 / No entries or data found`
  for linear cumulative data
- PV energy must therefore be implemented through the photovoltaics
  cumulative endpoint first

## Current code behavior

The coordinator currently stores approximately:

```python
{
    "live": {...},
    "energy_today": {...},
}