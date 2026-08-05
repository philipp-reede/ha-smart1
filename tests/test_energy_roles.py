from __future__ import annotations

import importlib
from pathlib import Path
import re
import sys
import types
import unittest


ROOT = Path(__file__).parents[1]

custom_components = sys.modules.setdefault(
    "custom_components",
    types.ModuleType("custom_components"),
)
custom_components.__path__ = [str(ROOT / "custom_components")]
smart1_ems = sys.modules.setdefault(
    "custom_components.smart1_ems",
    types.ModuleType("custom_components.smart1_ems"),
)
smart1_ems.__path__ = [str(ROOT / "custom_components" / "smart1_ems")]

energy_roles = importlib.import_module("custom_components.smart1_ems.energy_roles")
interface = importlib.import_module("custom_components.smart1_ems.interface")
point_module = importlib.import_module("custom_components.smart1_ems.point")

ENERGY_ROLES_BY_KEY = energy_roles.ENERGY_ROLES_BY_KEY
Smart1Point = point_module.Smart1Point


def make_point(
    point_id: str,
    name: str,
    interface_value: str = "",
    hardware: str = "Counter",
    *,
    point_type: str = "Energy",
    source: str = "counter",
) -> Smart1Point:
    return Smart1Point(
        id=point_id,
        name=name,
        type=point_type,
        source=source,
        hardware=hardware,
        interface=interface_value,
        parsed_interface=interface.parse_interface(interface_value),
    )


class EnergyRolesTest(unittest.TestCase):
    def test_recommends_one_source_for_each_known_role(self) -> None:
        points = [
            make_point("grid-import", "Bezug", "rio:riometer_1:USAGE"),
            make_point(
                "grid-export",
                "Überschuss",
                "rio:riometer_1:OVERPRODUCTION",
            ),
            make_point(
                "battery-charge",
                "BATT Laden",
                "rio:battery_2:CHARGE_POWER",
            ),
            make_point(
                "battery-discharge",
                "BATT Entladen",
                "rio:battery_2:DISCHARGE_POWER",
            ),
            make_point(
                "wallbox-monitoring",
                "ECar monitoring Laden",
                "rio:ecar_3:METER_POWER",
            ),
            make_point(
                "wallbox",
                "ECar Laden",
                "rio:ecar_3:METER_POWER",
            ),
            make_point("heat-pump", "WP Bezug Gesamt"),
            make_point(
                "heater-remote",
                "Heizstab Bezug",
                "rio:remoteio_4:POWER_METER",
                hardware="RemoteCounter",
            ),
            make_point(
                "heater-bus",
                "Heizstab Bezug",
                hardware="BusCounter",
            ),
        ]

        self.assertEqual(
            energy_roles.recommend_energy_roles(points),
            {
                "grid_import": "grid-import",
                "grid_export": "grid-export",
                "battery_charge": "battery-charge",
                "battery_discharge": "battery-discharge",
                "wallbox_consumption": "wallbox",
                "heat_pump_consumption": "heat-pump",
                "auxiliary_heater_consumption": "heater-remote",
            },
        )

    def test_heater_bus_counter_is_fallback_without_remote_data(self) -> None:
        bus_counter = make_point(
            "heater-bus",
            "Heizstab Bezug",
            hardware="BusCounter",
        )

        self.assertEqual(
            energy_roles.recommend_energy_roles([bus_counter]),
            {"auxiliary_heater_consumption": "heater-bus"},
        )

    def test_candidates_require_energy_counter_and_matching_category(self) -> None:
        matching = make_point("matching", "WP Bezug Gesamt")
        power = make_point(
            "power",
            "WP Leistung",
            point_type="Power",
        )
        sensor = make_point(
            "sensor",
            "WP Energie",
            source="sensor",
        )
        grid = make_point("grid", "Bezug", "rio:riometer_1:USAGE")

        self.assertEqual(
            energy_roles.energy_candidates(
                [matching, power, sensor, grid],
                ENERGY_ROLES_BY_KEY["heat_pump_consumption"],
            ),
            [matching],
        )

    def test_directional_roles_exclude_opposite_measurements(self) -> None:
        grid_import = make_point(
            "grid-import",
            "Bezug",
            "rio:riometer_1:USAGE",
        )
        grid_export = make_point(
            "grid-export",
            "Überschuss",
            "rio:riometer_1:OVERPRODUCTION",
        )
        battery_charge = make_point(
            "battery-charge",
            "BATT Laden",
            "rio:battery_2:CHARGE_POWER",
        )
        battery_discharge = make_point(
            "battery-discharge",
            "BATT Entladen",
            "rio:battery_2:DISCHARGE_POWER",
        )
        points = [
            grid_import,
            grid_export,
            battery_charge,
            battery_discharge,
        ]

        self.assertEqual(
            energy_roles.energy_candidates(
                points,
                ENERGY_ROLES_BY_KEY["grid_import"],
            ),
            [grid_import],
        )
        self.assertEqual(
            energy_roles.energy_candidates(
                points,
                ENERGY_ROLES_BY_KEY["grid_export"],
            ),
            [grid_export],
        )
        self.assertEqual(
            energy_roles.energy_candidates(
                points,
                ENERGY_ROLES_BY_KEY["battery_charge"],
            ),
            [battery_charge],
        )
        self.assertEqual(
            energy_roles.energy_candidates(
                points,
                ENERGY_ROLES_BY_KEY["battery_discharge"],
            ),
            [battery_discharge],
        )

    def test_statistic_id_is_stable_and_source_specific(self) -> None:
        statistic_id = energy_roles.statistic_id_for_role(
            "grid_import",
            "counter-one",
        )

        self.assertEqual(
            statistic_id,
            energy_roles.statistic_id_for_role("grid_import", "counter-one"),
        )
        self.assertNotEqual(
            statistic_id,
            energy_roles.statistic_id_for_role("grid_import", "counter-two"),
        )
        self.assertRegex(
            statistic_id,
            re.compile(r"^smart1_ems:grid_import_[0-9a-f]{8}$"),
        )


if __name__ == "__main__":
    unittest.main()
