# smart1 EMS for Home Assistant

[![CI](https://github.com/philipp-reede/ha-smart1/actions/workflows/ci.yml/badge.svg)](https://github.com/philipp-reede/ha-smart1/actions/workflows/ci.yml)
[![HACS validation](https://github.com/philipp-reede/ha-smart1/actions/workflows/validate.yml/badge.svg)](https://github.com/philipp-reede/ha-smart1/actions/workflows/validate.yml)
[![Latest release](https://img.shields.io/github/v/release/philipp-reede/ha-smart1?display_name=tag&sort=semver)](https://github.com/philipp-reede/ha-smart1/releases/latest)
[![License](https://img.shields.io/github/license/philipp-reede/ha-smart1)](LICENSE)

<p align="center">
  <img src="custom_components/smart1_ems/brand/icon.png" width="128" alt="smart1 EMS integration icon">
</p>

An unofficial, read-only Home Assistant integration for the smart1 energy
management system. It reads measurements from the smart1 CSV portal API and
represents the complete EMS, including photovoltaic production, grid exchange,
battery, wallbox, heat pump and auxiliary heating.

> **Unofficial community project:** This integration is not an official smart1
> release and is not affiliated with or endorsed by smart1. It is built against
> the [official smart1 CSV portal API documentation](https://data.smart1.eu/s/W3M4E8EkqMAPWqL?dir=/01%20SOFTWARE%20%26%20FIRMWARE/02%20PORTAL&editing=false&openfile=true).

> This project is an early public beta. It has been validated with one real
> smart1 installation and Home Assistant 2026.7.4.

[Deutsche Anleitung](docs/README.de.md) ·
[Installation](#installation-with-hacs) ·
[Screenshots](#screenshots) ·
[Energy Dashboard](#energy-dashboard) ·
[Troubleshooting](#troubleshooting) ·
[Roadmap](#roadmap)

## At a glance

| | |
| --- | --- |
| Access | Read-only cloud polling through the official CSV portal API |
| Setup | Home Assistant UI with a masked personal API key |
| Devices | EMS, PV, inverter, grid, battery, wallbox, heat pump and auxiliary heater |
| Energy Dashboard | PV, grid, battery and selected individual consumers |
| History | Automatic import of up to 365 days |
| Tested hardware | M-TEC Energy Hero, Energy Butler, Energy Heater and KEBA wallbox |

## Features

- UI-based setup using a personal smart1 portal API key
- Automatic plant and measurement-point discovery
- Home Assistant devices for the EMS, PV, grid, battery, wallbox, heat pump and
  auxiliary heater
- Optional physical inverter devices discovered from the documented inverter
  endpoint, with per-string AC/DC power, DC voltage and inverter temperature
  diagnostic sensors
- Live power, energy, temperature, percentage and diagnostic sensors where the
  portal exposes suitable points
- Battery state of charge detected from the structured smart1 `SOC` signal
- Exact PV production from the documented cumulative photovoltaic endpoint
- Energy Dashboard statistics for grid import/export, battery charge/discharge,
  wallbox, heat pump and auxiliary-heater consumption
- Automatic background import of up to 365 days of history
- Redacted Home Assistant diagnostics without API keys, plant IDs, linear IDs or
  measurement values

All portal access is read-only. The integration does not expose controls,
switches or commands.

## Screenshots

**Masked API-key setup**

<p align="center">
  <img src="docs/images/setup-api-key.jpg" width="720" alt="smart1 EMS setup with a masked API-key field">
</p>

**Automatically discovered devices**

<p align="center">
  <img src="docs/images/device-overview.jpg" width="900" alt="Home Assistant device overview for smart1 EMS">
</p>

<details>
  <summary><strong>Battery Energy Dashboard configuration</strong></summary>
  <p align="center">
    <img src="docs/images/energy-battery.jpg" width="500" alt="Battery charge, discharge, power and state-of-charge configuration">
  </p>
</details>

## Compatibility

The integration uses the common, officially documented smart1 CSV portal API
and should therefore work with all smart1 portals that expose the documented
endpoints and compatible measurement metadata. Real-world validation has so far
been limited to this installation:

- M-TEC Energy Hero EMS
- M-TEC Energy Heater heating element
- M-TEC Energy Butler inverter and battery storage
- KEBA wallbox

Other smart1 installations and hardware combinations may use different point
names or interface metadata. Diagnostic reports from those systems are welcome
and help extend the discovery rules.

## Installation with HACS

Until the integration is included in the default HACS catalogue, add it as a
custom repository:

[![Open your Home Assistant instance and open this repository inside HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=philipp-reede&repository=ha-smart1&category=integration)

Alternatively, add the repository manually:

1. Open HACS in Home Assistant.
2. Open the three-dot menu and select **Custom repositories**.
3. Add `https://github.com/philipp-reede/ha-smart1` with category
   **Integration**.
4. Search for **smart1 EMS** and download it.
5. Restart Home Assistant.

## Manual installation

Copy `custom_components/smart1_ems` from this repository into the
`custom_components` directory of your Home Assistant configuration, then
restart Home Assistant.

## Configuration

1. In Home Assistant, open **Settings → Devices & services**.
2. Select **Add integration** and search for **smart1 EMS**.
3. Enter your personal API key from the smart1 portal. The field is masked and
   the key is stored in the Home Assistant config entry.
4. If the key has access to multiple installations, select the desired plant.

Live entities are available after setup. The historical import starts in the
background and also runs automatically after future Home Assistant restarts.
Depending on portal response times, the first import can take several minutes.

## Energy Dashboard

Open **Settings → Devices & services → smart1 EMS → Configure** and select the
power point for every desired energy role. The integration suggests detected
points, but the selection is explicit because a smart1 installation can expose
multiple measurement paths for the same physical device.

After saving the roles, add these statistics in **Settings → Dashboards →
Energy**:

| Energy Dashboard section | smart1 EMS statistic |
| --- | --- |
| Solar production | smart1 EMS PV production |
| Grid consumption | smart1 EMS grid import |
| Return to grid | smart1 EMS grid export |
| Energy going into the battery | smart1 EMS battery charge |
| Energy coming out of the battery | smart1 EMS battery discharge |
| Individual devices | Wallbox, heat pump and auxiliary-heater consumption |

The PV total is taken from the documented cumulative endpoint. To keep the
Energy Dashboard's hourly flow calculation consistent, that exact daily total
is distributed across the measured five-minute PV power profile.

Other energy totals are estimates calculated from five-minute power samples
using guarded trapezoidal integration. Gaps longer than 15 minutes are not
bridged, so incomplete portal data can make these totals lower than actual
consumption. Changing a selected source creates a new statistic rather than
combining measurements from different sources.

## Troubleshooting

- Confirm that the Home Assistant host can reach `portal.smart1.eu`.
- Check **Settings → System → Logs** for messages mentioning `smart1_ems`.
- Download integration diagnostics from the smart1 EMS entry under
  **Settings → Devices & services** when reporting a discovery, mapping or
  history problem.
- Diagnostics generated by this integration are designed to omit credentials,
  identifiers and measurements, but review any diagnostic file before sharing
  it publicly.

Please report reproducible problems through the
[GitHub issue tracker](https://github.com/philipp-reede/ha-smart1/issues).
Include the Home Assistant version, integration version, relevant logs and the
redacted diagnostics file when possible.

## Known limitations

- The smart1 CSV portal is a cloud dependency and controls data availability.
- Inverter diagnostics depend on the optional inverter metadata and detailed
  photovoltaic endpoints. If a portal does not expose them, all other devices
  and sensors continue to work.
- Non-PV historical energy is derived rather than read from native cumulative
  meter totals because the tested installation does not expose usable linear
  cumulative data.
- Device classification is based on structured interface metadata and known
  smart1 naming conventions; unusual installations may require additional
  mapping rules.
- The integration is read-only and cannot control the EMS or connected devices.

## Roadmap

- Validate discovery and device mapping with additional smart1 portals and
  hardware combinations.
- Validate the new inverter and PV-string diagnostics with additional inverter
  models and multi-inverter installations.
- Make the historical import range configurable if longer or shorter imports
  prove useful across installations.
- Apply for inclusion in the default HACS catalogue after broader real-world
  validation.

Technical API findings and implementation details are documented in
[`API_NOTES.md`](API_NOTES.md).

## Support the project

If this integration is useful to you, you can support its continued development
here:

[![Buy me a beer](docs/images/buy-me-a-beer.svg)](https://www.buymeacoffee.com/philipp_reede)

## Releases

The integration version is stored in
`custom_components/smart1_ems/manifest.json`. Release notes are maintained in
[`CHANGELOG.md`](CHANGELOG.md). GitHub releases use matching `vX.Y.Z` tags so
HACS can offer stable, selectable versions.

## License

This project is available under the [MIT License](LICENSE).
