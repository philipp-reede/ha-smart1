# Changelog

All notable changes to this project are documented in this file.

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

