from __future__ import annotations

import asyncio
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).parents[1]
SPEC = spec_from_file_location(
    "smart1_recorder_helpers_test_module",
    ROOT / "custom_components" / "smart1_ems" / "recorder_helpers.py",
)
assert SPEC is not None and SPEC.loader is not None
recorder_helpers = module_from_spec(SPEC)
SPEC.loader.exec_module(recorder_helpers)


class Smart1RecorderHelpersTest(unittest.TestCase):
    def test_clear_statistics_waits_for_recorder_callback(self) -> None:
        def clear_statistics(statistic_ids, *, on_done) -> None:
            self.assertEqual(statistic_ids, ["smart1_ems:a", "smart1_ems:b"])
            on_done()

        recorder = Mock(
            async_clear_statistics=Mock(side_effect=clear_statistics)
        )

        result = asyncio.run(
            recorder_helpers.async_clear_statistics(
                recorder,
                ["smart1_ems:b", "smart1_ems:a", "smart1_ems:b"],
            )
        )

        self.assertTrue(result)
        recorder.async_clear_statistics.assert_called_once()

    def test_clear_statistics_reports_callback_timeout(self) -> None:
        recorder = Mock(async_clear_statistics=Mock())

        with patch.object(
            recorder_helpers,
            "RECORDER_OPERATION_TIMEOUT",
            0.001,
        ):
            result = asyncio.run(
                recorder_helpers.async_clear_statistics(
                    recorder,
                    ["smart1_ems:a"],
                )
            )

        self.assertFalse(result)

    def test_clear_statistics_skips_empty_request(self) -> None:
        recorder = Mock(async_clear_statistics=Mock())

        result = asyncio.run(
            recorder_helpers.async_clear_statistics(recorder, [])
        )

        self.assertTrue(result)
        recorder.async_clear_statistics.assert_not_called()


if __name__ == "__main__":
    unittest.main()
