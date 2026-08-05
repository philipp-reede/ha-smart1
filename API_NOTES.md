# smart1 EMS portal API notes

Source document: `2020_Discription_API_CSV_Portal.pdf`, version 2020_1.0.

## Authentication and base URL

- Base URL: `https://portal.smart1.eu/export`
- Every request uses the user-specific `apikey` query parameter.
- Never log, store in fixtures, or commit a real API key.

## Configuration endpoints

- `/plants`
- `/plants/{deviceId}`
- `/sensors/{deviceId}`
- `/counters/{deviceId}`
- `/inverters/{deviceId}`
- `/modulfields/{deviceId}`
- `/bus/{deviceId}`

`SensorId` and `Counter Id` are the linear IDs used by the linear data
endpoint. `InverterId` follows the form `Inverter_B{bus}_A{address}`.

## Linear live data

Endpoint:

`/data/csv/{deviceId}/linear/{period}/detailed/{YYYYMMDD}/{linearIds}`

- Supported periods: `day`, `month`, `year`.
- `linearIds` is an optional comma-separated list, but the documentation
  recommends always supplying it.
- Response fields include `DeviceId`, `LinearId`, `Timestamp`, `Value1`, and
  `Value2`.
- `Value1` contains the point value in the base unit indicated by its type.
- `Value2` is reserved.

## Photovoltaic detailed data

Endpoint:

`/data/csv/{deviceId}/photovoltaics/{period}/detailed/{YYYYMMDD}/{bus}/{address}/{stringId}`

The bus, address, and string filters are optional for day and month. Bus and
address are mandatory for year requests.

- `Value1`: AC power in W
- `Value2`: DC power in W
- `Value3`: DC voltage in V
- `Value4`: inverter temperature in °C

The integration first discovers the physical inverter layout through
`/inverters/{deviceId}` and then reads the unfiltered current-day detailed
response every five minutes. The newest row for each `(Bus, Address, StringId)`
is exposed as diagnostic entities on a separate physical inverter device:

- AC power, DC power and DC voltage per active string
- One inverter temperature using the newest temperature reported by its strings

Once the detailed endpoint returns rows for an inverter, those rows are the
authoritative list of active strings. This avoids false positives from portals
that assign module fields to unused inverter inputs. Until detailed rows are
available, a positive configured capacity or a meaningful module-field
assignment is used as a discovery fallback. Declared but unused inverter inputs
do not create unavailable Home Assistant entities. Registry entries created by
earlier versions for such inputs are removed when the integration is reloaded.

The inverter name, manufacturer, model and serial number populate the Home
Assistant device registry. The API key, plant ID, inverter ID, serial number,
timestamps and measurements remain excluded from integration diagnostics.
Both endpoints are optional: a missing or failed inverter request does not make
the existing linear entities or Energy Dashboard statistics unavailable.

## Photovoltaic module-field configuration

Endpoint:

`/modulfields/{deviceId}`

The documented response identifies every module field as `Modulfield_{id}`.
Inverter string metadata refers to the same numeric ID. Available fields are:

- `Name`
- `Bias`: module tilt in degrees
- `Direction`: azimuth in degrees
- `ShadowFrom` and `ShadowTill`: configured shadow interval
- `Reward` and `Variation`: documented configuration values without stated
  units
- `Monitoring` and `Configured`: configuration status

The integration exposes tilt and azimuth as static diagnostic entities on the
existing photovoltaic device. Installed module-field capacity is derived by
summing the documented capacities of inverter strings assigned to that module
field. Shadow intervals and status values are attributes. `Reward` and
`Variation` are parsed but not exposed because the API document does not define
their units or Home Assistant semantics.

The module-field endpoint is optional. Failure or absence does not affect live
measurements, inverter diagnostics, historical imports or Energy Dashboard
statistics. Integration diagnostics report only capability counts, not module-
field names, IDs, angles or other configuration values.

## Inverter bus configuration

Endpoint:

`/bus/{deviceId}`

The documented response contains one row per available bus slot. `BusId`
identifies the bus, `BusConfigured` reports its configuration status,
`BusManufactors` contains the documented protocol count, and numbered
`BusManufactorN` columns contain the manufacturer protocols. The API document
uses the `Manufactor` spelling; the parser also accepts corrected
`Manufacturer` headers for portal compatibility.

Rows where both the configuration status and all manufacturer fields contain
`No value` are empty slots and are omitted. Every active bus becomes one static
diagnostic entity on the existing smart1 EMS device. It is not represented as
a separate physical device because the endpoint describes a communication bus,
not a distinct inverter.

The bus endpoint is optional. Missing or failed requests do not affect live
measurements, inverter devices, historical imports or Energy Dashboard
statistics. Integration diagnostics report bus and protocol counts plus a
privacy-safe endpoint probe. The probe distinguishes missing, empty, failed and
data-bearing responses and includes only HTTP status, row count and column
names. It also classifies bus ID formats and reports how many rows contain
configuration or manufacturer metadata. It does not expose bus values, IDs or
manufacturer names.

## Photovoltaic cumulative data

Endpoint:

`/data/csv/{deviceId}/photovoltaics/{period}/cumulative/{YYYYMMDD}/{bus}/{address}/{stringId}`

- Supported periods: `day`, `month`, `year`.
- `Value1` contains PV production in Wh.
- `Value2` through `Value4` are reserved.
- Production is logged only on the first used string of an inverter, usually
  string 1.
- When requesting all strings, aggregate one production value per
  `(Bus, Address)` to avoid double counting.

## Linear cumulative data

The overview lists:

`/data/csv/{deviceId}/linear/{period}/cumulative/{YYYYMMDD}/{linearIds}`

The document does not provide a corresponding response definition or field
semantics. The current installation returned `404 / No entries or data found`.
The integration must therefore not generate energy entities from this
endpoint without installation-specific evidence that the endpoint is
available and meaningful.

The diagnostics export performs one read-only, value-free capability probe
for the previous complete day. It reports only the response shape and the
point numbers for which rows exist. API keys, linear IDs, timestamps and
measurements are excluded.

The real-installation probe requested all 38 energy-typed counters for a
completed day. The portal returned an API error row with `Errorcode` and
`Errormessage` columns instead of cumulative measurements. The linear
cumulative endpoint is therefore not currently a usable source for these
Energy Dashboard totals.

## Detailed-power calibration

Before deriving non-PV energy, diagnostics compare one completed day of the
`pv_global` five-minute power point with the exact documented PV daily total.
Detailed W samples are integrated in UTC using the trapezoidal rule. Intervals
longer than 15 minutes are skipped so missing data is never bridged.

The diagnostic result exposes only coverage, sample and gap counts, and the
relative percentage difference. It excludes the linear ID, timestamps, raw
power, derived kWh and reference kWh. A difference up to 5% is classified as
`good`, up to 10% as `marginal`, and above 10% as `poor`.

The real-installation calibration for the previous complete day contained
288 samples and 287 integrated intervals, covering 1,435 minutes with no
skipped gaps. The integrated result differed from the documented PV daily
total by 0.43%, which supports using the same guarded integration for
explicitly selected non-PV power points.

## Derived non-PV energy

Because the linear cumulative endpoint does not provide usable totals for the
current installation, non-PV Energy Dashboard data is derived from detailed
five-minute power values. This is deliberately opt-in:

- The Options Flow offers eligible counter points for grid import, grid export,
  battery charge, battery discharge, wallbox, heat pump and auxiliary heater.
- Detected points are suggested, but the user must save the selection before
  any derived statistic is created.
- Each date is fetched once for all selected points and integrated with the
  calibrated trapezoidal method.
- Gaps longer than 15 minutes are excluded rather than estimated.
- Statistics use kWh and a source-specific ID. Changing the selected source
  creates a new statistic instead of combining incompatible histories.
- The initial import covers 365 days; the latest three days are refreshed
  every six hours.

These values are estimates derived from power samples, not native smart1
meter totals. Missing coverage can therefore make them lower than the actual
energy consumption.

## Historical PV production

The day-based photovoltaic cumulative endpoint is the documented source for
exact daily production totals. Month and year requests return aggregate
period values, not a documented daily breakdown. The integration therefore
requests the day endpoint once per date when importing history.

- The initial import covers the most recent 365 days.
- The latest three days are refreshed every six hours so delayed portal data
  can be corrected.
- The exact daily total is distributed into UTC-aligned hourly statistics by
  scaling the integrated five-minute `pv_global` power profile. This preserves
  the documented daily production while keeping the Energy Dashboard's hourly
  source and residual-consumption calculations temporally aligned.
- A zero-valued local-midnight bucket replaces the former single daily bucket
  during migration under the unchanged statistic ID.
- Missing dates remain unknown and are not converted to zero production.
- The import runs in the background and is stored as external Home Assistant
  long-term statistics under `smart1_ems:pv_production`.

## Further documented opportunities

The 2020 API document contains no control or write endpoints. Additional
read-only features could be built from these documented calls:

- Month and year periods are documented for detailed and cumulative calls.
  Cumulative month/year replies are aggregate period totals, so they cannot
  replace the day-by-day import required for hourly Energy Dashboard history.

Plant details can include address and coordinates. They are not currently
exposed because they add little Home Assistant value and increase privacy risk.
Future additions should remain optional, avoid duplicating equivalent linear
points and preserve the integration's read-only scope.
