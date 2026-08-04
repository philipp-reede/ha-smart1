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

## Historical PV production

The day-based photovoltaic cumulative endpoint is the documented source for
exact daily production totals. Month and year requests return aggregate
period values, not a documented daily breakdown. The integration therefore
requests the day endpoint once per date when importing history.

- The initial import covers the most recent 365 days.
- The latest three days are refreshed every six hours so delayed portal data
  can be corrected.
- Missing dates remain unknown and are not converted to zero production.
- The import runs in the background and is stored as external Home Assistant
  long-term statistics under `smart1_ems:pv_production`.
