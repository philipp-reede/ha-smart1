from __future__ import annotations

from datetime import date, datetime, timezone
import importlib
from pathlib import Path
import sys
import types
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).parents[1]

aiohttp = sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
aiohttp.ClientError = type("ClientError", (Exception,), {})

homeassistant = sys.modules.setdefault(
    "homeassistant",
    types.ModuleType("homeassistant"),
)
homeassistant.__path__ = []

components = sys.modules.setdefault(
    "homeassistant.components",
    types.ModuleType("homeassistant.components"),
)
components.__path__ = []

recorder = types.ModuleType("homeassistant.components.recorder")
recorder.get_instance = lambda hass: None
recorder.__path__ = []
sys.modules["homeassistant.components.recorder"] = recorder


class StatisticData(dict):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)


class StatisticMetaData(dict):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)


models = types.ModuleType("homeassistant.components.recorder.models")
models.StatisticData = StatisticData
models.StatisticMetaData = StatisticMetaData
models.StatisticMeanType = types.SimpleNamespace(NONE="none")
sys.modules["homeassistant.components.recorder.models"] = models

statistics = types.ModuleType("homeassistant.components.recorder.statistics")
statistics.async_add_external_statistics = lambda *args, **kwargs: None
statistics.get_last_statistics = lambda *args, **kwargs: {}
sys.modules["homeassistant.components.recorder.statistics"] = statistics

const = types.ModuleType("homeassistant.const")
const.UnitOfEnergy = types.SimpleNamespace(KILO_WATT_HOUR="kWh")
sys.modules["homeassistant.const"] = const

core = sys.modules.setdefault(
    "homeassistant.core",
    types.ModuleType("homeassistant.core"),
)
core.HomeAssistant = object

util = sys.modules.setdefault(
    "homeassistant.util",
    types.ModuleType("homeassistant.util"),
)
util.__path__ = []
unit_conversion = types.ModuleType("homeassistant.util.unit_conversion")
unit_conversion.EnergyConverter = types.SimpleNamespace(UNIT_CLASS="energy")
sys.modules["homeassistant.util.unit_conversion"] = unit_conversion

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

history = importlib.import_module("custom_components.smart1_ems.history")


class Smart1HistoryTest(unittest.TestCase):
    def test_initial_window_contains_365_days(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")

        start, baseline = history.determine_import_window(
            [],
            date(2026, 8, 4),
            local_tz,
        )

        self.assertEqual(start, date(2025, 8, 5))
        self.assertEqual(baseline, 0.0)

    def test_refresh_uses_sum_before_recent_window(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        records = [
            {
                "start": datetime(
                    2026,
                    8,
                    1,
                    tzinfo=local_tz,
                ).timestamp(),
                "sum": 42.5,
            },
            {
                "start": datetime(
                    2026,
                    8,
                    2,
                    tzinfo=local_tz,
                ).timestamp(),
                "sum": 47.0,
            },
        ]

        start, baseline = history.determine_import_window(
            records,
            date(2026, 8, 4),
            local_tz,
        )

        self.assertEqual(start, date(2026, 8, 2))
        self.assertEqual(baseline, 42.5)

    def test_statistics_are_cumulative_and_hour_aligned(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")

        result = history.build_pv_statistics(
            [
                (date(2026, 3, 29), 4.25),
                (date(2026, 3, 30), 5.75),
            ],
            10.0,
            local_tz,
        )

        self.assertEqual([record["state"] for record in result], [4.25, 5.75])
        self.assertEqual([record["sum"] for record in result], [14.25, 20.0])
        self.assertEqual(result[0]["start"].tzinfo, local_tz)
        self.assertEqual(result[0]["start"].minute, 0)
        self.assertEqual(
            result[0]["start"].astimezone(timezone.utc).minute,
            0,
        )
        self.assertEqual(
            (
                result[1]["start"].astimezone(timezone.utc)
                - result[0]["start"].astimezone(timezone.utc)
            ).total_seconds(),
            23 * 60 * 60,
        )

    def test_refresh_preserves_existing_value_when_portal_has_no_data(self) -> None:
        local_tz = ZoneInfo("Europe/Berlin")
        records = [
            {
                "start": datetime(2026, 8, 2, tzinfo=local_tz).timestamp(),
                "state": 3.0,
                "sum": 20.0,
            },
            {
                "start": datetime(2026, 8, 3, tzinfo=local_tz).timestamp(),
                "state": 4.0,
                "sum": 24.0,
            },
        ]

        result = history.merge_daily_energy(
            records,
            [(date(2026, 8, 3), 4.5), (date(2026, 8, 4), 2.0)],
            date(2026, 8, 2),
            date(2026, 8, 4),
            local_tz,
        )

        self.assertEqual(
            result,
            [
                (date(2026, 8, 2), 3.0),
                (date(2026, 8, 3), 4.5),
                (date(2026, 8, 4), 2.0),
            ],
        )


if __name__ == "__main__":
    unittest.main()
