"""Diagnostics support for the smart1 EMS integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import ClientError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import Smart1Api, Smart1ApiError
from .bus import Smart1BusSystem
from .classifier import classify_point
from .const import DOMAIN
from .energy_roles import ENERGY_ROLES_BY_KEY
from .inverter import Smart1Inverter, Smart1PvStringSample
from .module_field import Smart1ModuleField
from .point import Smart1Point
from .power_integration import integrate_power_rows


def _point_diagnostics(number: int, point: Smart1Point) -> dict[str, Any]:
    """Return non-value metadata used to classify one point."""
    parsed = point.parsed_interface

    return {
        "point_number": number,
        "name": point.name,
        "type": point.type,
        "source": point.source,
        "hardware": point.hardware,
        "interface": point.interface,
        "max": point.max,
        "index": point.index,
        "parsed_interface": {
            "protocol": parsed.protocol if parsed else None,
            "service": parsed.service if parsed else None,
            "service_id": parsed.service_id if parsed else None,
            "object_type": parsed.object_type if parsed else None,
            "object_id": parsed.object_id if parsed else None,
            "signal": parsed.signal if parsed else None,
        },
        "current_category": classify_point(point).value,
    }


def _configured_energy_roles(
    entry: ConfigEntry,
    numbered_points: list[tuple[int, Smart1Point]],
) -> dict[str, int | None]:
    """Map configured roles to redacted point numbers instead of linear IDs."""
    point_numbers_by_id = {
        point.id: number for number, point in numbered_points
    }
    selected_roles = entry.options.get("energy_roles", {})
    return {
        role_key: point_numbers_by_id.get(point_id)
        for role_key, point_id in selected_roles.items()
        if role_key in ENERGY_ROLES_BY_KEY
    }


def _inverter_diagnostics(
    inverters: list[Smart1Inverter],
    samples: dict[tuple[int, int, int], Smart1PvStringSample],
) -> dict[str, Any]:
    """Describe inverter support without IDs, serial numbers, or values."""
    inverter_rows = []
    for number, inverter in enumerate(inverters, start=1):
        inverter_samples = [
            sample
            for (bus, address, _string_id), sample in samples.items()
            if (bus, address) == inverter.key
        ]
        available_metrics = set()
        for sample in inverter_samples:
            for metric in (
                "ac_power_w",
                "dc_power_w",
                "dc_voltage_v",
                "inverter_temperature_c",
            ):
                if getattr(sample, metric) is not None:
                    available_metrics.add(metric)

        inverter_rows.append(
            {
                "inverter_number": number,
                "name": inverter.name,
                "manufacturer": inverter.manufacturer,
                "model": inverter.model,
                "configured_strings": inverter.string_count,
                "strings_with_data": len(
                    {sample.string_id for sample in inverter_samples}
                ),
                "available_metrics": sorted(available_metrics),
                "monitoring": inverter.monitoring,
                "configured": inverter.configured,
                "serial_number_present": bool(inverter.serial_number),
            }
        )

    return {
        "inverter_count": len(inverters),
        "strings_with_data": len(samples),
        "inverters": inverter_rows,
    }


def _module_field_diagnostics(
    module_fields: list[Smart1ModuleField],
    inverters: list[Smart1Inverter],
) -> dict[str, int]:
    """Describe module-field support without names or configuration values."""
    return {
        "module_field_count": len(module_fields),
        "with_installed_capacity": sum(
            module_field.installed_capacity_w(inverters) is not None
            for module_field in module_fields
        ),
        "with_azimuth": sum(
            module_field.azimuth_degrees is not None
            for module_field in module_fields
        ),
        "with_tilt": sum(
            module_field.tilt_degrees is not None
            for module_field in module_fields
        ),
        "with_shadow_interval": sum(
            bool(module_field.shadow_from and module_field.shadow_until)
            for module_field in module_fields
        ),
    }


def _bus_diagnostics(
    buses: list[Smart1BusSystem],
    endpoint_probe: object = None,
) -> dict[str, Any]:
    """Describe buses and the endpoint shape without values or identifiers."""
    result: dict[str, Any] = {
        "configured_bus_count": len(buses),
        "with_manufacturer_information": sum(
            bool(bus.manufacturers) for bus in buses
        ),
        "manufacturer_protocol_counts": sorted(
            len(bus.manufacturers) for bus in buses
        ),
    }

    if not isinstance(endpoint_probe, dict):
        return result

    for key in (
        "endpoint_result",
        "response_status",
        "response_rows",
        "error_code",
        "error_type",
        "rows_with_documented_bus_id",
        "rows_with_numeric_bus_id",
        "rows_with_other_bus_id",
        "rows_without_bus_id",
        "rows_with_configuration_status",
        "rows_with_manufacturer_count",
        "rows_with_manufacturer_protocols",
    ):
        value = endpoint_probe.get(key)
        if isinstance(value, (str, int)):
            result[key] = value

    columns = endpoint_probe.get("response_columns")
    if isinstance(columns, (list, tuple)) and all(
        isinstance(column, str) for column in columns
    ):
        result["response_columns"] = sorted(set(columns))

    return result


async def _linear_cumulative_probe(
    api: Smart1Api,
    numbered_points: list[tuple[int, Smart1Point]],
) -> dict[str, Any]:
    """Probe yesterday's cumulative endpoint without exposing IDs or values."""
    energy_points = [
        (number, point)
        for number, point in numbered_points
        if point.source == "counter" and point.type.lower() == "energy"
    ]
    point_numbers_by_id = {
        point.id: number for number, point in energy_points
    }
    target_date = dt_util.now().date() - timedelta(days=1)

    try:
        rows = await api.get_linear_cumulative_rows(
            [point.id for _, point in energy_points],
            target_date=target_date,
            missing_ok=True,
        )
    except Smart1ApiError as err:
        return {
            "period": "previous_complete_day",
            "requested_points": len(energy_points),
            "result": "api_error",
            "error_code": err.code,
        }
    except (ClientError, TimeoutError) as err:
        return {
            "period": "previous_complete_day",
            "requested_points": len(energy_points),
            "result": "request_failed",
            "error_type": type(err).__name__,
        }

    points_with_rows = sorted(
        {
            point_numbers_by_id[linear_id]
            for row in rows
            if (linear_id := row.get("LinearId")) in point_numbers_by_id
        }
    )
    columns = sorted({column for row in rows for column in row})

    return {
        "period": "previous_complete_day",
        "requested_points": len(energy_points),
        "result": "data_returned" if rows else "no_data",
        "response_rows": len(rows),
        "response_columns": columns,
        "point_numbers_with_rows": points_with_rows,
        "unmatched_response_rows": len(rows) - sum(
            1 for row in rows if row.get("LinearId") in point_numbers_by_id
        ),
    }


async def _pv_power_integration_probe(
    hass: HomeAssistant,
    api: Smart1Api,
    numbered_points: list[tuple[int, Smart1Point]],
) -> dict[str, Any]:
    """Compare derived PV energy with the exact daily PV total."""
    pv_point = next(
        (
            (number, point)
            for number, point in numbered_points
            if point.source == "counter"
            and point.type.lower() == "energy"
            and point.hardware.lower() == "pv_global"
        ),
        None,
    )
    if pv_point is None:
        return {
            "period": "previous_complete_day",
            "result": "no_pv_power_point",
        }

    point_number, point = pv_point
    target_date = dt_util.now().date() - timedelta(days=1)

    try:
        rows = await api.get_linear_detailed_rows(
            [point.id],
            target_date=target_date,
            missing_ok=True,
        )
        exact_energy_kwh = await api.get_pv_cumulative_energy(
            target_date=target_date,
            missing_ok=True,
        )
    except Smart1ApiError as err:
        return {
            "period": "previous_complete_day",
            "point_number": point_number,
            "result": "api_error",
            "error_code": err.code,
        }
    except (ClientError, TimeoutError) as err:
        return {
            "period": "previous_complete_day",
            "point_number": point_number,
            "result": "request_failed",
            "error_type": type(err).__name__,
        }

    integration = integrate_power_rows(
        rows,
        point.id,
        ZoneInfo(hass.config.time_zone),
    )
    base_result = {
        "period": "previous_complete_day",
        "point_number": point_number,
        "method": "trapezoidal_max_15_minute_gap",
        "sample_count": integration.sample_count,
        "integrated_intervals": integration.integrated_intervals,
        "skipped_gaps": integration.skipped_gaps,
        "coverage_minutes": round(integration.covered_seconds / 60),
    }

    if exact_energy_kwh is None or integration.integrated_intervals == 0:
        return {**base_result, "result": "insufficient_data"}
    if exact_energy_kwh <= 0:
        return {**base_result, "result": "zero_reference_energy"}

    relative_difference = (
        abs(integration.energy_kwh - exact_energy_kwh)
        / exact_energy_kwh
        * 100
    )
    if relative_difference <= 5:
        assessment = "good"
    elif relative_difference <= 10:
        assessment = "marginal"
    else:
        assessment = "poor"

    return {
        **base_result,
        "result": "calibrated",
        "relative_difference_percent": round(relative_difference, 2),
        "assessment": assessment,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics without credentials, device IDs, or live values."""
    runtime_data = hass.data[DOMAIN][entry.entry_id]
    points = runtime_data["devices"]
    numbered_points = list(enumerate(points, start=1))
    coordinator_data = runtime_data["coordinator"].data or {}

    return {
        "integration": DOMAIN,
        "history_imports": [
            history_importer.diagnostic_status
            for history_importer in runtime_data.get("history_importers", [])
        ],
        "configured_energy_roles": _configured_energy_roles(
            entry,
            numbered_points,
        ),
        "discovery": runtime_data["discovery"].to_dict(),
        "inverter_diagnostics": _inverter_diagnostics(
            runtime_data.get("inverters", []),
            coordinator_data.get("pv_strings", {}),
        ),
        "module_field_diagnostics": _module_field_diagnostics(
            runtime_data.get("module_fields", []),
            runtime_data.get("inverters", []),
        ),
        "bus_diagnostics": _bus_diagnostics(
            runtime_data.get("buses", []),
            runtime_data.get("bus_probe"),
        ),
        "linear_cumulative_probe": await _linear_cumulative_probe(
            runtime_data["api"],
            numbered_points,
        ),
        "pv_power_integration_probe": await _pv_power_integration_probe(
            hass,
            runtime_data["api"],
            numbered_points,
        ),
        "points": [
            _point_diagnostics(number, point)
            for number, point in numbered_points
        ],
    }
