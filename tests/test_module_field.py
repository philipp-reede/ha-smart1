from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).parents[1]
PACKAGE_PATH = ROOT / "custom_components" / "smart1_ems"

custom_components = sys.modules.setdefault(
    "custom_components",
    types.ModuleType("custom_components"),
)
custom_components.__path__ = [str(ROOT / "custom_components")]
smart1_ems = sys.modules.setdefault(
    "custom_components.smart1_ems",
    types.ModuleType("custom_components.smart1_ems"),
)
smart1_ems.__path__ = [str(PACKAGE_PATH)]

inverter_module = __import__(
    "custom_components.smart1_ems.inverter",
    fromlist=["Smart1Inverter"],
)

SPEC = importlib.util.spec_from_file_location(
    "custom_components.smart1_ems.module_field",
    PACKAGE_PATH / "module_field.py",
)
MODULE_FIELD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE_FIELD
SPEC.loader.exec_module(MODULE_FIELD)


class ParseModuleFieldsTest(unittest.TestCase):
    def test_parses_documented_module_field_metadata(self) -> None:
        rows = [
            {
                "ModulfieldId": "Modulfield_1",
                "Name": "West 65 Grad",
                "Bias": "23",
                "Direction": "65",
                "ShadowFrom": "11:00:00",
                "ShadowTill": "13:00:00",
                "Reward": "0.07",
                "Variation": "15",
                "Monitoring": "on",
                "Configured": "ok",
            }
        ]

        module_field = MODULE_FIELD.parse_module_fields(rows)[0]

        self.assertEqual(module_field.id, "Modulfield_1")
        self.assertEqual(module_field.reference, "1")
        self.assertEqual(module_field.name, "West 65 Grad")
        self.assertEqual(module_field.tilt_degrees, 23.0)
        self.assertEqual(module_field.azimuth_degrees, 65.0)
        self.assertEqual(module_field.shadow_from, "11:00:00")
        self.assertEqual(module_field.shadow_until, "13:00:00")
        self.assertEqual(module_field.reward, 0.07)
        self.assertEqual(module_field.variation, 15.0)

    def test_sums_capacity_of_assigned_inverter_strings(self) -> None:
        module_field = MODULE_FIELD.Smart1ModuleField(
            id="Modulfield_2",
            reference="2",
        )
        inverters = [
            inverter_module.Smart1Inverter(
                id="Inverter_B2_A1",
                bus=2,
                address=1,
                string_count=3,
                string_capacities_w=(4200.0, 3800.0, 0.0),
                string_module_fields=("2", "Modulfield_2", "3"),
            )
        ]

        self.assertEqual(module_field.installed_capacity_w(inverters), 8000.0)

    def test_capacity_is_unknown_without_assigned_capacity(self) -> None:
        module_field = MODULE_FIELD.Smart1ModuleField(
            id="Modulfield_4",
            reference="4",
        )

        self.assertIsNone(module_field.installed_capacity_w([]))

    def test_ignores_invalid_module_field_ids(self) -> None:
        self.assertEqual(
            MODULE_FIELD.parse_module_fields([{"ModulfieldId": "invalid"}]),
            [],
        )


if __name__ == "__main__":
    unittest.main()
