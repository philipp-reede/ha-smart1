# Changelog

All notable changes to this project are documented in this file.

## [0.6.0] - 2026-08-05

### Added

- Discover configured inverter bus systems through the optional documented
  `/bus/{deviceId}` endpoint
- Add one static diagnostic entity for every active inverter bus to the
  existing smart1 EMS device
- Expose the documented bus status and manufacturer protocols as entity
  attributes
- Add redacted bus capability counts to Home Assistant diagnostics

### Reliability

- Ignore unconfigured bus slots and keep all existing entities available when
  the optional bus endpoint is absent or temporarily fails

## [0.5.1] - 2026-08-05

### Fixed

- Use Home Assistant's supported `DEGREE` constant for module-field azimuth
  and tilt sensors instead of the unavailable `UnitOfAngle` import

## [0.5.0] - 2026-08-05

### Added

- Discover optional PV module-field configuration through the documented
  `/modulfields/{deviceId}` endpoint
- Add diagnostic entities for installed module-field capacity, azimuth and
  tilt to the existing photovoltaic device
- Expose documented shadow intervals and configuration status as attributes
- Add redacted module-field capability metadata to Home Assistant diagnostics

### Documentation

- Disclose that the integration was created through an AI-assisted vibe-coding
  workflow with OpenAI Codex

## [0.4.2] - 2026-08-05

### Fixed

- Prefer strings actually reported by the detailed inverter endpoint over
  ambiguous module-field metadata
- Keep metadata-based string discovery as a fallback until detailed rows are
  available

## [0.4.1] - 2026-08-05

### Fixed

- Create PV-string entities only for strings with an active configuration or
  actual measurements
- Remove previously registered diagnostic entities for unused inverter strings

## [0.4.0] - 2026-08-05

### Added

- Discover physical inverters through the documented smart1 inverter endpoint
- Add optional diagnostic sensors for per-string AC/DC power, DC voltage and
  inverter temperature
- Include redacted inverter capability metadata in Home Assistant diagnostics

### Reliability

- Keep all existing linear entities and Energy Dashboard statistics available
  when optional inverter endpoints are missing or temporarily fail

### Documentation

- Add a compact project overview and clearer navigation to the English and
  German README files
- Add anonymized setup, device-overview and battery Energy Dashboard screenshots
- Add release and license badges plus a direct "Open in HACS" installation link
- Document the next read-only API opportunities and project roadmap
- Replace the plain support link with a GitHub-compatible "Buy me a beer"
  button

## [0.3.1] - 2026-08-05

### Added

- Add local 256 px and 512 px brand icons for Home Assistant and the HACS
  repository store.

## [0.3.0] - 2026-08-05

First HACS-ready public beta.

### Added

- UI configuration with masked API-key input and automatic plant discovery
- Device and entity discovery for the complete smart1 EMS
- Exact PV production and automatic 365-day historical import
- Explicit Energy Dashboard roles for grid, battery, wallbox, heat pump and
  auxiliary-heater energy
- Redacted Home Assistant diagnostics for discovery and history troubleshooting
- English and German setup translations
- HACS metadata, automated repository validation and an MIT license

### Changed

- Distribute exact daily PV totals across the measured hourly production profile
- Store derived non-PV energy in UTC-aligned hourly statistics
- Refresh current-day statistics every 15 minutes and recent history every six
  hours
- Repair legacy daily-only statistics in place while preserving stable statistic
  IDs

### Limitations

- Non-PV energy is estimated from five-minute power samples because native
  cumulative linear totals are unavailable on the validated installation
- The integration is read-only and does not control the EMS or connected devices
