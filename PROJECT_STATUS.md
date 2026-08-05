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
- Physical inverters are discovered from the documented metadata endpoint and
  represented as separate Home Assistant devices
- The detailed photovoltaic endpoint supplies per-string AC/DC power and DC
  voltage plus inverter temperature as optional diagnostic sensors
- Missing or failed inverter endpoints do not block linear entities, PV totals
  or Energy Dashboard statistics; transient failures retain the latest sample
- Inverter diagnostics report only redacted capability metadata and never
  expose inverter IDs, serial numbers, timestamps or measurements
- Active inverter communication buses are discovered from the optional
  documented bus endpoint and exposed as static diagnostics on the EMS device
- Empty bus slots are omitted, and bus diagnostics expose only capability
  counts instead of manufacturer protocol names
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
- Exact PV daily totals are distributed across UTC-aligned hours using the
  measured five-minute `pv_global` profile and normalized back to the exact
  cumulative daily value, avoiding a midnight residual-consumption spike
- Existing daily PV records migrate in place under the unchanged
  `smart1_ems:pv_production` statistic ID
- Recent PV statistics are refreshed every six hours to capture corrections
- An Options Flow lets the user explicitly map grid, battery, wallbox, heat
  pump and auxiliary-heater power points to derived energy roles
- Selected roles are integrated into source-specific external kWh statistics
  with the same 365-day import and three-day refresh behavior
- Derived power history is split into UTC-aligned hourly energy statistics;
  existing daily statistics migrate in place without changing statistic IDs
- Daily-only or non-monotonic derived statistics are cleared only after a
  complete replacement fetch and then rebuilt under the same statistic IDs;
  exact PV history and statistics from other integrations are not cleared
- A role without replacement data no longer blocks repairs for other roles;
  transient detailed-history requests are retried before a repair is deferred
- Current-day PV and derived statistics refresh every 15 minutes while the
  wider historical window continues to refresh every six hours
- Redacted diagnostics expose the history import result and repair state
  without statistic source IDs or measurement values
- Derived grid import and export statistics were accepted by the real Home
  Assistant Energy Dashboard
- The auxiliary-heater remote counter is the preferred source because its bus
  counter has no available data on the current installation
- Diagnostics expose configured energy roles through redacted point numbers
  instead of linear IDs
- The real battery state-of-charge point is identified from its structured
  `SOC` signal and exposed as a Home Assistant battery percentage sensor
- Other battery-related percentage points such as state of health and
  EnergyCloud enablement flags are not offered as battery state of charge
- Entities use Home Assistant's device-aware naming convention
- The repository contains HACS metadata, public installation documentation,
  English and German custom-integration translations, and automated HACS and
  Hassfest validation for the 0.3.0 public beta
- The integration is designed for the documented smart1 portal API generally;
  real-world hardware validation currently covers M-TEC Energy Hero EMS,
  Energy Heater, Energy Butler inverter/storage and a KEBA wallbox

## Remaining work

- Validate discovery and device assignment with additional smart1 portals and
  hardware combinations
- Device-specific cumulative totals for wallbox, heat pump, heating element,
  grid and battery remain unavailable; their optional energy statistics are
  estimates derived from five-minute power samples
- The preferred source needs confirmation where the installation exposes
  multiple measurement paths for one physical device
- Validate inverter and PV-string diagnostics with additional inverter models,
  string layouts and multi-inverter installations
- Validate module-field assignments, derived installed capacity and orientation
  metadata with additional roof layouts
- Validate inverter-bus discovery and manufacturer protocols with additional
  EMS and inverter combinations
- Add a configurable history range if real-world installations need it
- Apply for inclusion in the default HACS catalogue after broader validation

## Current code behavior

The coordinator currently stores approximately:

```python
{
    "live": {...},
    "pv_energy_today": 12.345,
    "pv_strings": {(2, 1, 1): Smart1PvStringSample(...)},
}
```

`pv_energy_today` is `None` when the optional PV cumulative endpoint is not
available. `pv_strings` is empty when the optional inverter endpoints are not
available. Live values continue updating in both cases.

Static module-field metadata is stored separately in the config-entry runtime
data because it is read only during setup and does not require five-minute
polling. Optional configured inverter buses are stored in the same way.
