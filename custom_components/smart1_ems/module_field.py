"""Models and parsers for documented smart1 PV module-field metadata."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .inverter import Smart1Inverter


_MODULE_FIELD_ID_PATTERN = re.compile(
    r"^Modulfield_(?P<reference>.+)$",
    re.IGNORECASE,
)
_MISSING_VALUES = {"", "no value", '"no value"'}


def _clean_text(value: object) -> str:
    """Return a normalized module-field metadata string."""
    text = str(value or "").strip().strip('"')
    return "" if text.casefold() in _MISSING_VALUES else text


def _optional_float(value: object) -> float | None:
    """Parse one optional smart1 numeric value."""
    text = _clean_text(value)
    if not text:
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def normalize_module_field_reference(value: object) -> str:
    """Return the reference used between inverter and module-field rows."""
    text = _clean_text(value)
    match = _MODULE_FIELD_ID_PATTERN.match(text)
    if match is not None:
        text = match.group("reference")
    return str(int(text)) if text.isdigit() else text.casefold()


@dataclass(frozen=True, slots=True)
class Smart1ModuleField:
    """Static metadata returned by `/modulfields/{deviceId}`."""

    id: str
    reference: str
    name: str = ""
    tilt_degrees: float | None = None
    azimuth_degrees: float | None = None
    shadow_from: str = ""
    shadow_until: str = ""
    reward: float | None = None
    variation: float | None = None
    monitoring: str = ""
    configured: str = ""

    def installed_capacity_w(
        self,
        inverters: list[Smart1Inverter],
    ) -> float | None:
        """Sum configured string capacities assigned to this module field."""
        capacities = []
        for inverter in inverters:
            for index, module_field in enumerate(
                inverter.string_module_fields
            ):
                if (
                    normalize_module_field_reference(module_field)
                    != self.reference
                ):
                    continue
                capacity = (
                    inverter.string_capacities_w[index]
                    if index < len(inverter.string_capacities_w)
                    else None
                )
                if capacity is not None:
                    capacities.append(capacity)
        return sum(capacities) if capacities else None


def parse_module_fields(rows: list[dict[str, str]]) -> list[Smart1ModuleField]:
    """Parse documented module-field metadata and ignore invalid rows."""
    module_fields: dict[str, Smart1ModuleField] = {}

    for row in rows:
        module_field_id = _clean_text(
            row.get("ModulfieldId")
            or row.get("Modulfield Id")
            or row.get('"ModulfieldId"')
        )
        match = _MODULE_FIELD_ID_PATTERN.match(module_field_id)
        if match is None:
            continue

        reference = normalize_module_field_reference(
            match.group("reference")
        )
        module_fields[module_field_id] = Smart1ModuleField(
            id=module_field_id,
            reference=reference,
            name=_clean_text(row.get("Name")),
            tilt_degrees=_optional_float(row.get("Bias")),
            azimuth_degrees=_optional_float(row.get("Direction")),
            shadow_from=_clean_text(row.get("ShadowFrom")),
            shadow_until=_clean_text(
                row.get("ShadowTill") or row.get("ShadowUntil")
            ),
            reward=_optional_float(row.get("Reward")),
            variation=_optional_float(row.get("Variation")),
            monitoring=_clean_text(row.get("Monitoring")),
            configured=_clean_text(row.get("Configured")),
        )

    return list(module_fields.values())
