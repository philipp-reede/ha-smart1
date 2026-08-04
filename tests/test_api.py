from __future__ import annotations

import asyncio
from datetime import date
import importlib
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).parents[1]

if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = types.ModuleType("aiohttp")

custom_components = sys.modules.setdefault(
    "custom_components",
    types.ModuleType("custom_components"),
)
custom_components.__path__ = [str(ROOT / "custom_components")]

smart1_csv = sys.modules.setdefault(
    "custom_components.smart1_csv",
    types.ModuleType("custom_components.smart1_csv"),
)
smart1_csv.__path__ = [str(ROOT / "custom_components" / "smart1_csv")]

Smart1Api = importlib.import_module("custom_components.smart1_csv.api").Smart1Api


class RecordingSmart1Api(Smart1Api):
    def __init__(self, rows: list[dict[str, str]]) -> None:
        super().__init__(None, "redacted", "42")
        self.rows = rows
        self.requested_path: str | None = None

    async def _get_csv(self, path: str) -> list[dict[str, str]]:
        self.requested_path = path
        return self.rows


class Smart1ApiTest(unittest.TestCase):
    def test_pv_cumulative_endpoint_and_conversion(self) -> None:
        api = RecordingSmart1Api(
            [
                {
                    "Bus": "1",
                    "Address": "2",
                    "StringId": "1",
                    "Value1": "3456",
                }
            ]
        )

        value = asyncio.run(
            api.get_pv_cumulative_energy("day", date(2026, 8, 4))
        )

        self.assertEqual(value, 3.456)
        self.assertEqual(
            api.requested_path,
            "/data/csv/42/photovoltaics/day/cumulative/20260804",
        )

    def test_rejects_unsupported_period(self) -> None:
        api = RecordingSmart1Api([])

        with self.assertRaises(ValueError):
            asyncio.run(
                api.get_pv_cumulative_energy("week", date(2026, 8, 4))
            )


if __name__ == "__main__":
    unittest.main()
