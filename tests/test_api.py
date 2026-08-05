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

smart1_ems = sys.modules.setdefault(
    "custom_components.smart1_ems",
    types.ModuleType("custom_components.smart1_ems"),
)
smart1_ems.__path__ = [str(ROOT / "custom_components" / "smart1_ems")]

api_module = importlib.import_module("custom_components.smart1_ems.api")
Smart1Api = api_module.Smart1Api
Smart1ApiError = api_module.Smart1ApiError


class CsvResponse:
    def __init__(self, text: str, status: int) -> None:
        self._text = text
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def text(self) -> str:
        return self._text

class CsvSession:
    def __init__(self, text: str, status: int = 200) -> None:
        self._text = text
        self._status = status

    def get(self, *args, **kwargs) -> CsvResponse:
        return CsvResponse(self._text, self._status)


class RecordingSmart1Api(Smart1Api):
    def __init__(self, rows: list[dict[str, str]]) -> None:
        super().__init__(None, "redacted", "42")
        self.rows = rows
        self.requested_path: str | None = None
        self.missing_ok = False

    async def _get_csv(
        self,
        path: str,
        *,
        missing_ok: bool = False,
    ) -> list[dict[str, str]]:
        self.requested_path = path
        self.missing_ok = missing_ok
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

    def test_historical_request_allows_missing_days(self) -> None:
        api = RecordingSmart1Api([])

        value = asyncio.run(
            api.get_pv_cumulative_energy(
                target_date=date(2026, 8, 3),
                missing_ok=True,
            )
        )

        self.assertIsNone(value)
        self.assertTrue(api.missing_ok)

    def test_inverter_metadata_endpoint_is_optional(self) -> None:
        api = RecordingSmart1Api(
            [
                {
                    "Inverter Id": "Inverter_B2_A1",
                    "Name": "Energy Butler",
                    "Strings": "2",
                }
            ]
        )

        inverters = asyncio.run(api.get_inverters(missing_ok=True))

        self.assertEqual(inverters[0].key, (2, 1))
        self.assertEqual(inverters[0].string_ids, (1, 2))
        self.assertEqual(api.requested_path, "/inverters/42")
        self.assertTrue(api.missing_ok)

    def test_module_field_endpoint_is_optional(self) -> None:
        api = RecordingSmart1Api(
            [
                {
                    "ModulfieldId": "Modulfield_1",
                    "Name": "West",
                    "Bias": "23",
                    "Direction": "65",
                }
            ]
        )

        module_fields = asyncio.run(api.get_module_fields(missing_ok=True))

        self.assertEqual(module_fields[0].reference, "1")
        self.assertEqual(module_fields[0].tilt_degrees, 23.0)
        self.assertEqual(module_fields[0].azimuth_degrees, 65.0)
        self.assertEqual(api.requested_path, "/modulfields/42")
        self.assertTrue(api.missing_ok)

    def test_pv_detailed_endpoint_accepts_string_filter(self) -> None:
        api = RecordingSmart1Api([])

        rows = asyncio.run(
            api.get_pv_detailed_rows(
                target_date=date(2026, 8, 5),
                bus=2,
                address=1,
                string_id=3,
                missing_ok=True,
            )
        )

        self.assertEqual(rows, [])
        self.assertEqual(
            api.requested_path,
            "/data/csv/42/photovoltaics/day/detailed/20260805/2/1/3",
        )
        self.assertTrue(api.missing_ok)

    def test_year_pv_detailed_request_requires_inverter(self) -> None:
        api = RecordingSmart1Api([])

        with self.assertRaisesRegex(ValueError, "require bus and address"):
            asyncio.run(api.get_pv_detailed_rows(period="year"))

    def test_linear_live_endpoint_uses_requested_date(self) -> None:
        api = RecordingSmart1Api([])

        values = asyncio.run(
            api.get_latest_linear_values(
                ["counter_1"],
                target_date=date(2026, 8, 4),
            )
        )

        self.assertEqual(values, {"counter_1": None})
        self.assertEqual(
            api.requested_path,
            "/data/csv/42/linear/day/detailed/20260804/counter_1",
        )

    def test_rejects_unsupported_period(self) -> None:
        api = RecordingSmart1Api([])

        with self.assertRaises(ValueError):
            asyncio.run(
                api.get_pv_cumulative_energy("week", date(2026, 8, 4))
            )

    def test_linear_cumulative_probe_uses_requested_points(self) -> None:
        api = RecordingSmart1Api([])

        rows = asyncio.run(
            api.get_linear_cumulative_rows(
                ["counter_1", "counter_2"],
                target_date=date(2026, 8, 3),
                missing_ok=True,
            )
        )

        self.assertEqual(rows, [])
        self.assertEqual(
            api.requested_path,
            "/data/csv/42/linear/day/cumulative/20260803/"
            "counter_1,counter_2",
        )
        self.assertTrue(api.missing_ok)

    def test_linear_detailed_endpoint_uses_requested_date(self) -> None:
        api = RecordingSmart1Api([])

        rows = asyncio.run(
            api.get_linear_detailed_rows(
                ["pv_1"],
                target_date=date(2026, 8, 3),
                missing_ok=True,
            )
        )

        self.assertEqual(rows, [])
        self.assertEqual(
            api.requested_path,
            "/data/csv/42/linear/day/detailed/20260803/pv_1",
        )
        self.assertTrue(api.missing_ok)

    def test_embedded_not_found_is_missing_data(self) -> None:
        api = Smart1Api(
            CsvSession("Errorcode;Errormessage\n404;No entries or data found\n"),
            "redacted",
            "42",
        )

        rows = asyncio.run(api._get_csv("/test", missing_ok=True))

        self.assertEqual(rows, [])

    def test_embedded_api_error_is_raised_without_message(self) -> None:
        api = Smart1Api(
            CsvSession("Errorcode;Errormessage\n500;private portal details\n"),
            "redacted",
            "42",
        )

        with self.assertRaisesRegex(Smart1ApiError, "smart1 API error 500"):
            asyncio.run(api._get_csv("/test"))

    def test_http_error_does_not_expose_api_key(self) -> None:
        api_key = "private-api-key"
        api = Smart1Api(
            CsvSession(f"Portal rejected {api_key}", status=401),
            api_key,
            "42",
        )

        with self.assertLogs(api_module._LOGGER, level="ERROR") as captured:
            with self.assertRaises(Smart1ApiError) as raised:
                asyncio.run(api._get_csv("/test"))

        self.assertEqual(str(raised.exception), "smart1 API error 401")
        self.assertNotIn(api_key, str(raised.exception))
        self.assertNotIn(api_key, "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
