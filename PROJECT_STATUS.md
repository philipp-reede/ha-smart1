# Project status

## Working

- Custom integration loads through Home Assistant
- Config flow asks for the API key in a translated, masked password field
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
- Measurement roles now share one logical Smart1 EMS device
- Heat pumps and heating elements have dedicated classification rules
- Direct battery, e-car, heat-pump, and grid-meter interfaces are classified
  by their parsed smart1 service
- Multi-device counter calculations remain assigned to the EMS
- Redacted diagnostics expose classification metadata without credentials or values
- Diagnostics probe the previous complete day for linear cumulative support
  without exporting IDs, timestamps, or measurements
- The completed-day probe returned an API error row rather than cumulative
  measurements for the current installation
- Diagnostics can calibrate guarded five-minute power integration against the
  exact PV daily total without exporting power or energy values
- Real-installation calibration produced a 0.43% difference with all 288
  expected five-minute samples and no skipped gaps
- Integration name and domain are now `smart1 EMS` and `smart1_ems`
- The latest 365 days of documented PV daily production are imported as
  external long-term statistics in the background
- Recent PV statistics are refreshed every six hours to capture corrections
- An Options Flow lets the user explicitly map grid, battery, wallbox, heat
  pump and auxiliary-heater power points to derived energy roles
- Selected roles are integrated into source-specific external kWh statistics
  with the same 365-day import and three-day refresh behavior
- Derived grid import and export statistics were accepted by the real Home
  Assistant Energy Dashboard
- The auxiliary-heater remote counter is the preferred source because its bus
  counter has no available data on the current installation
- Diagnostics expose configured energy roles through redacted point numbers
  instead of linear IDs
- Entities use Home Assistant's device-aware naming convention

## Known problems

- Device assignment needs validation against real installation metadata
- Temperature is a property and should be assigned to its source device
- Device-specific cumulative totals for wallbox, heat pump, heating element,
  grid and battery remain unavailable; their optional energy statistics are
  estimates derived from five-minute power samples
- The preferred source needs confirmation where the installation exposes
  multiple measurement paths for one physical device
- The domain change from `smart1_csv` to `smart1_ems` is intentionally
  breaking while the integration is still under development

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
