# Changelog

All notable changes to this project are documented in this file.

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
