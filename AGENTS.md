# Project instructions

## Project goal

Build a read-only Home Assistant integration for the smart1 energy
management system, intended for publication through HACS.

The integration uses the smart1 CSV cloud API. It must represent the EMS,
not only the inverter. Relevant consumers include wallboxes, heat pumps,
heating elements and other devices known to smart1.

## Scope

- Read data only.
- Do not implement commands, switches or write access.
- Primary goals:
  1. Reliable live sensors
  2. Home Assistant device grouping
  3. Energy Dashboard support
  4. Historical energy data where the API actually provides it
  5. HACS publication

## Architecture

Current integration domain: `smart1_csv`.

Current layers:

- `api.py`: HTTP and CSV parsing only
- `point.py`: `Smart1Point`, one smart1 linear measurement point
- `interface.py`: parses structured smart1 interface strings
- `classifier.py`: assigns semantic categories
- `entity_mapper.py`: maps points to Home Assistant entity properties
- `coordinator.py`: periodic cloud polling
- `sensor.py`: Home Assistant entities and device registry
- `discovery.py`: installation discovery used inside Home Assistant
- `config_flow.py`: API-key setup and automatic plant selection

Do not introduce generic abstractions unless they directly improve this
Home Assistant integration.

## Coding conventions

- Use US English in code and docstrings.
- Keep the integration read-only.
- Prefer Home Assistant-native types and patterns.
- Keep API logic out of `sensor.py`.
- Keep stable entity unique IDs based on config entry ID, linear ID and
  value source.
- Do not log API keys.
- Avoid adding external Python requirements.
- Preserve existing working behavior while refactoring.
- Run available syntax and validation checks after changes.
- Explain any assumption about undocumented smart1 behavior.

## Domain knowledge

A smart1 `Smart1Point` is a measurement point, not necessarily a physical
device.

Known classifications:

- Interface containing `wallbox` → wallbox
- Interface containing `heatpump` → heat pump
- Name containing standalone `WP` may indicate heat pump
- `Energy Heater` is M-TEC's product name for an electric heating element
- `Heizstab` is the generic German description
- `energytrader` currently appears to indicate battery-related EnergyCloud
  points, but this should remain treated as an inference until verified
- Temperatures must belong to their actual logical device where possible;
  do not create a generic “Temperatures” device
- “Consumption” is a measurement role, not a physical device

## smart1 API behavior established so far

Base URL:

`https://portal.smart1.eu/export`

Configuration endpoints:

- `/plants`
- `/sensors/{deviceId}`
- `/counters/{deviceId}`
- `/inverters/{deviceId}`
- `/modulfields/{deviceId}`
- `/bus/{deviceId}`

Live linear values:

`/data/csv/{deviceId}/linear/day/detailed/{YYYYMMDD}/{linearIds}`

- `linearIds` may be a comma-separated list.
- Response commonly uses `LinearId`, `Timestamp`, `Value1`, `Value2`.
- `Value1` is the point value in its base unit.
- This endpoint works for the current installation.

Linear cumulative endpoint:

`/data/csv/{deviceId}/linear/{period}/cumulative/{YYYYMMDD}/{linearIds}`

- Documented periods: `day`, `month`, `year`.
- A test against the current installation returned:
  `404 / No entries or data found`.
- Do not assume it provides energy for every counter.

PV cumulative endpoint:

`/data/csv/{deviceId}/photovoltaics/{period}/cumulative/{YYYYMMDD}`

- Intended for PV production data.
- `Value1` is documented as production in Wh.
- Periods: `day`, `month`, `year`.
- Optional bus/address/string filters exist.
- This is the likely source for PV energy entities.

## Current priorities

1. Correct logical device classification and grouping
2. Detect heat pump and heating element correctly
3. Remove invalid automatically generated linear cumulative entities
4. Implement PV energy using the photovoltaics cumulative endpoint
5. Investigate energy data for wallbox, heat pump, heating element, grid
   and battery without inventing unsupported API behavior
6. Add Options Flow
7. Add diagnostics
8. Prepare GitHub/HACS release

## Safety and secrets

- Never commit an API key.
- Never print full request URLs containing an API key.
- Use redacted or placeholder values in documentation and tests.