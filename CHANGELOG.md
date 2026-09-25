# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Fixed

- Stop completed sparse derived-energy statistics from being mistaken for
  legacy daily data and rebuilt over 365 days every six hours, while retaining
  the full rebuild for decreasing cumulative sums
- Replace stale derived-energy hours with zero-value tombstones when a
  successfully refreshed local day no longer contains those hours
- Track the currently active photovoltaic history schema so temporary switches
  between hourly and daily storage trigger one complete hourly repair instead
  of leaving older midnight buckets behind
- Preserve both occurrences of ambiguous daylight-saving-time samples when
  naive portal rows are interleaved by local wall time

### Testing

- Cover recovery from an incomplete initial 365-day derived-history fetch on
  the next successful attempt

## [0.7.1] - 2026-09-25

### Fixed

- Scope PV and derived Energy Dashboard statistics to their smart1
  installation so multiple config entries can no longer share recorder rows
- Replace every successfully refreshed local PV day as a unit, including
  zeroing obsolete hourly buckets, so a daily fallback or a missing detail
  profile cannot duplicate the exact cumulative total
- Persist completed PV history schemas and successful empty PV or derived
  backfills, preventing legitimate daily fallbacks and empty histories from
  restarting a 365-day import every six hours, while rebuilding once if a
  previously populated external statistic later disappears from Recorder
- Preserve both occurrences of ambiguous naive portal timestamps during the
  autumn daylight-saving-time transition, for ascending and descending CSV
  responses
- Perform a required plant authentication probe when an installation has no
  active linear points, allowing rejected credentials or revoked access to the
  configured plant to enter Home Assistant's reauthentication flow
- Abort the Energy-role Options Flow cleanly when its config entry is not
  loaded instead of raising an internal error
- Remove obsolete inverter, PV-string, module-field and inverter-bus entities
  after a successful authoritative topology response, while retaining them
  when an optional endpoint is missing, has an unknown schema or fails
- Select the newest live value by its parsed CSV timestamp instead of relying
  on the response row order

### Migration

- Keep the established external-statistic IDs for one deterministic legacy
  config entry so its existing Energy Dashboard selections remain valid. If
  several legacy installations could already have shared those IDs, clear and
  rebuild them for that owner only after a complete replacement fetch
- Assign installation-scoped IDs to additional existing entries and all new
  entries. Historical rows that multiple installations may already have
  written to the former shared IDs cannot be attributed automatically; they
  are replaced by the legacy owner's available 365-day history instead

### CI

- Upgrade `actions/checkout` and `actions/setup-python` to version 7 and give
  the unit-test and Home Assistant import jobs stable check names

### Documentation

- Record the successful in-place upgrade and real-world validation of smart1
  EMS `v0.7.0` on Home Assistant OS `18.3`, Supervisor `2026.09.2` and Core
  `2026.9.3`

## [0.7.0] - 2026-09-25

### Added

- Give every configured installation a stable config-entry unique ID based on
  its smart1 `DeviceId` and prevent duplicate configuration of the same plant
- Add a Home Assistant reauthentication flow that validates a replacement API
  key against the existing installation before updating the stored credential

### Reliability

- Migrate existing entries to the new identity metadata without changing their
  device, entity or external-statistic identifiers
- Distinguish rejected credentials from temporary portal and network failures;
  only HTTP/API `401` and `403` responses request reauthentication

### Testing

- Cover single- and multi-plant setup, modern and legacy duplicates,
  reauthentication, config-entry migration and Energy-role option validation

### Documentation

- Record the successful real-world validation of smart1 EMS `v0.6.6` with
  Home Assistant OS `18.3`, Core `2026.9.3` and Energy Hero software `1.28.59`
- Document the validated Energy Dashboard, history-import, inverter and
  privacy-safe diagnostics results from the reference installation

## [0.6.6] - 2026-09-24

### Fixed

- Use the Home Assistant device registry `via_device_id` for inverter parents
  on supported Core versions, while retaining compatibility with Home
  Assistant 2026.7 and earlier

## [0.6.5] - 2026-09-24

### Security

- Sanitize portal, HTTP and network errors before they reach Home Assistant
  logs, update failures or downloadable diagnostics
- Prevent chained client exceptions from exposing request URLs containing the
  personal API key

### Testing

- Add an integration setup test for startup, six-hour repair and 15-minute
  current-day history imports
- Add a pinned Home Assistant `2026.9.3` import smoke test on Python 3.14 to CI

### Reliability

- Let Home Assistant retry required initial discovery failures through
  `ConfigEntryNotReady` without retaining sensitive client exceptions

### Documentation

- Align project status, compatibility notes and issue templates with the
  current `v0.6.5` early-beta release

## [0.6.4] - 2026-09-24

### Fixed

- Retry temporary failures while fetching daily photovoltaic history
- Defer the first photovoltaic or derived-energy backfill when the complete
  history range could not be fetched, preventing permanent gaps caused by a
  partial initial import
- Preserve the existing partial-refresh behavior after statistics have already
  been established
- Keep the 15-minute current-day refresh from repeatedly starting a full
  365-day backfill while an initial import is waiting for repair

### Notes

- The protection is preventive. Existing historical gaps are not rebuilt
  automatically because they cannot be distinguished reliably from dates for
  which the portal supplied no data.

## [0.6.3] - 2026-08-27

### Validation

- Run HACS repository validation without ignored checks in preparation for
  inclusion in the default HACS catalogue
- Confirm operation with Energy Hero software `1.28.59` and Home Assistant Core
  `2026.8.3`

### Documentation

- Record the additional tested software versions in the compatibility matrix

## [0.6.2] - 2026-08-06

### Fixed

- Use absolute GitHub URLs for repository images so HACS can render the icon,
  screenshots and support button from the README
- Replace the dynamic license badge with a static MIT badge and an absolute
  link to the license

## [0.6.1] - 2026-08-05

### Added

- Add a privacy-safe bus endpoint probe to Home Assistant diagnostics
- Report whether the optional endpoint is missing, empty, failed or returned
  data, together with only its HTTP status, row count and column names

### Reliability

- Distinguish an unavailable bus endpoint from a portal response whose column
  layout is not yet understood, without exposing bus values or identifiers

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
