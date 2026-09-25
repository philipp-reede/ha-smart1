# Project status

## Working

- Custom integration loads through Home Assistant
- Config flow asks for the API key in a translated, masked password field
- `/plants` is queried automatically
- A single plant is selected automatically; when an API key exposes multiple
  plants, the config flow asks the user to select one
- Config entries use the stable smart1 `DeviceId` as their unique ID. Existing
  entries are migrated in place, while modern and legacy duplicates are
  rejected without changing device or entity identifiers
- External PV and derived-energy statistic IDs are scoped to their smart1
  installation. During migration, one deterministic legacy config entry keeps
  the established unscoped IDs and therefore its existing Energy Dashboard
  selections; additional existing entries and all new entries receive stable,
  installation-specific IDs. When several legacy installations may already
  have shared those IDs, the retained statistics are cleared and rebuilt from
  a complete replacement fetch for their deterministic owner
- Rejected API keys trigger a translated Home Assistant reauthentication flow.
  A replacement key is stored only when it still exposes the original plant;
  temporary portal and network failures continue through normal retries
- Sensors and counters are discovered automatically
- Live values are fetched through one filtered linear request and selected by
  their parsed CSV timestamps, independent of portal row order
- An installation without active linear points performs a small required plant
  probe so rejected credentials or lost access to the configured plant still
  trigger Home Assistant reauthentication
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
- Obsolete inverter, PV-string, module-field and bus entities are removed only
  after the corresponding optional endpoint returns an authoritative known
  topology; missing endpoints, unknown schemas and request failures never
  trigger registry cleanup
- Inverter diagnostics report only redacted capability metadata and never
  expose inverter IDs, serial numbers, timestamps or measurements
- Active inverter communication buses are discovered from the optional
  documented bus endpoint and exposed as static diagnostic entities on the EMS
  device
- Empty bus slots are omitted. Home Assistant entity attributes may show the
  documented manufacturer protocol names, while the redacted downloadable
  diagnostics expose only capability counts instead of those names
- Bus diagnostics distinguish a missing or empty endpoint from an unrecognized
  portal response using only status, row count and column names
- Measurement roles now share one logical Smart1 EMS device
- Heat pumps and heating elements have dedicated classification rules
- Direct battery, e-car, heat-pump, and grid-meter interfaces are classified
  by their parsed smart1 service
- Multi-device counter calculations remain assigned to the EMS
- Redacted diagnostics expose classification metadata without credentials or values
- Portal, HTTP and network errors are reduced to privacy-safe codes or exception
  types before they reach logs, update failures or downloadable diagnostics;
  failed required discovery is retried through Home Assistant without retaining
  the API-key-bearing request exception
- Diagnostics probe the previous complete day for linear cumulative support
  without exporting IDs, timestamps, or measurements
- On the reference installation, the completed-day probe returned an API error
  row rather than cumulative measurements
- Diagnostics can calibrate guarded five-minute power integration against the
  exact PV daily total without exporting power or energy values
- The latest calibration on the reference installation produced a 0.78%
  difference with all 288 expected five-minute samples and no skipped gaps
- Integration name and domain are now `smart1 EMS` and `smart1_ems`
- The latest 365 days of documented PV daily production are imported as
  external long-term statistics in the background
- Exact PV daily totals are distributed across UTC-aligned hours using the
  measured five-minute `pv_global` profile and normalized back to the exact
  cumulative daily value, avoiding a midnight residual-consumption spike
- Existing daily PV records for the deterministic legacy owner migrate in
  place under the unchanged `smart1_ems:pv_production` statistic ID; scoped
  installations use their own PV statistic IDs
- Recent PV statistics are refreshed every six hours to capture corrections
- A successfully fetched PV day replaces that complete local day. Obsolete
  hourly buckets are explicitly zeroed before cumulative sums are rebuilt, so
  daily fallbacks and changes in detail-profile availability cannot duplicate
  the portal's exact daily total
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
- Since version 0.6.4, an initial PV or derived-energy backfill is written only
  after the complete requested history range has been fetched. Temporary daily
  request failures are retried and an incomplete initial import is deferred
  instead of being stored as a permanent partial baseline
- Successful initial backfills now persist their schema completion even when
  the portal returns no samples. Legitimate PV daily fallbacks also persist the
  completed hourly schema, preventing repeated year-long repair sweeps while
  retaining the scheduled recent-history and current-day refreshes. Completion
  state records per schema whether Recorder rows were created, so a later loss
  of previously populated statistics triggers one complete rebuild instead of
  being mistaken for a legitimately empty history
- Current-day PV and derived statistics refresh every 15 minutes while the
  wider historical window continues to refresh every six hours. A pending
  initial repair is not restarted by every current-day refresh
- Five-minute power integration preserves both occurrences of naive local
  timestamps during the autumn daylight-saving-time fold; timestamps that
  already include an offset continue to be used directly
- Redacted diagnostics expose the history import result and repair state
  without statistic source IDs or measurement values
- Derived grid import and export statistics were accepted by the real Home
  Assistant Energy Dashboard
- The auxiliary-heater remote counter is the preferred source because its bus
  counter has no available data on the current installation
- Diagnostics expose configured energy roles through redacted point numbers
  instead of linear IDs
- The Energy-role Options Flow exits with a translated not-loaded message when
  runtime discovery data is unavailable instead of raising an internal error
- The real battery state-of-charge point is identified from its structured
  `SOC` signal and exposed as a Home Assistant battery percentage sensor
- Other battery-related percentage points such as state of health and
  EnergyCloud enablement flags are not offered as battery state of charge
- Entities use Home Assistant's device-aware naming convention
- The repository contains HACS metadata, public installation documentation,
  English and German custom-integration translations, and automated HACS and
  Hassfest validation for the current 0.7.1 early-beta release
- CI also imports every integration module against pinned Home Assistant Core
  2026.9.3 on Python 3.14, while setup tests cover startup history import and
  the separate scheduled repair and current-day refresh paths. GitHub's
  checkout and Python setup actions use version 7, and the unit-test and import
  jobs expose stable check names suitable for repository rules
- The reference installation was upgraded in place from version 0.6.6 to 0.7.0
  with Energy Hero software 1.28.59, Home Assistant OS 18.3 and Core 2026.9.3.
  Supervisor was version 2026.09.2. It started without integration warnings or
  observed anomalies, retained 77 plausible measurement points and all seven
  Energy Dashboard roles, completed both history importers and showed no Energy
  Dashboard anomalies. Diagnostics also confirmed one inverter with two active
  strings, two complete module fields and a gap-free 288-sample PV calibration
  with a 0.78% difference. This supports a non-disruptive in-place upgrade.
  Reauthentication was not exercised, and the redacted diagnostics do not
  expose the config-entry unique ID or schema version; those cases remain
  covered by automated tests. The known `via_device` removal scheduled for
  Home Assistant 2027.8 is covered through `via_device_id` on supported
  versions, with a compatibility path for Home Assistant 2026.7 and earlier
- The integration is designed for the documented smart1 portal API generally;
  real-world hardware validation currently covers M-TEC Energy Hero EMS,
  Energy Heater, Energy Butler inverter/storage, M-TEC AP440 heat pump and a
  KEBA wallbox

## Known limitations

- Device-specific cumulative totals for wallbox, heat pump, heating element,
  grid and battery are unavailable on the reference installation. Their
  optional energy statistics are therefore estimates derived from five-minute
  power samples
- Historical rows that multiple pre-migration config entries may already have
  written into the former shared external-statistic IDs cannot be attributed
  to their originating installations. Migration therefore discards those
  shared rows after a complete replacement fetch, rebuilds up to 365 available
  days for the deterministic legacy owner and starts isolated histories for
  the other entries; unavailable older shared history cannot be recovered
- The version 0.6.4 backfill protection is preventive. Existing historical
  gaps are not rebuilt automatically because a date for which the portal
  supplied no data cannot be distinguished reliably from an earlier failed
  fetch

## Remaining work

- Validate discovery and device assignment with additional smart1 portals and
  hardware combinations
- The preferred source needs confirmation where the installation exposes
  multiple measurement paths for one physical device
- Validate inverter and PV-string diagnostics with additional inverter models,
  string layouts and multi-inverter installations
- Validate module-field assignments, derived installed capacity and orientation
  metadata with additional roof layouts
- Validate inverter-bus discovery and manufacturer protocols with additional
  EMS and inverter combinations
- Add a configurable history range if real-world installations need it
- Monitor the pending default HACS catalogue review in
  [hacs/default#10379](https://github.com/hacs/default/pull/10379) and address
  review feedback

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
available. After a successful detailed-inverter update, a transient request
failure retains the previous `pv_strings` samples. Live values continue
updating in all cases.

Static module-field metadata is stored separately in the config-entry runtime
data because it is read only during setup and does not require five-minute
polling. Optional configured inverter buses are stored in the same way.
