from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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

    def test_timeout_keeps_replacement_behind_late_clear(self) -> None:
        operations: list[str] = []
        callbacks = []
        completion = Mock()

        def clear_statistics(_statistic_ids, *, on_done) -> None:
            operations.append("clear")
            callbacks.append(on_done)

        recorder = Mock(
            async_clear_statistics=Mock(side_effect=clear_statistics)
        )

        async def run_scenario() -> bool:
            with patch.object(
                recorder_helpers,
                "RECORDER_OPERATION_TIMEOUT",
                0.001,
            ):
                result = await recorder_helpers.async_clear_statistics(
                    recorder,
                    ["smart1_ems:a"],
                    enqueue_followup=lambda: operations.append("import"),
                    on_done=completion,
                )
            self.assertEqual(operations, ["clear", "import"])
            completion.assert_not_called()
            callbacks[0]()
            await asyncio.sleep(0)
            completion.assert_called_once_with()
            return result

        self.assertFalse(asyncio.run(run_scenario()))

    def test_cancellation_happens_after_replacement_is_queued(self) -> None:
        operations: list[str] = []
        callbacks = []
        completion = Mock()

        def clear_statistics(_statistic_ids, *, on_done) -> None:
            operations.append("clear")
            callbacks.append(on_done)

        recorder = Mock(
            async_clear_statistics=Mock(side_effect=clear_statistics)
        )

        async def run_scenario() -> None:
            task = asyncio.create_task(
                recorder_helpers.async_clear_statistics(
                    recorder,
                    ["smart1_ems:a"],
                    enqueue_followup=lambda: operations.append("import"),
                    on_done=completion,
                )
            )
            await asyncio.sleep(0)
            self.assertEqual(operations, ["clear", "import"])
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            callbacks[0]()
            await asyncio.sleep(0)
            completion.assert_called_once_with()

        asyncio.run(run_scenario())

    def test_clear_statistics_skips_empty_request(self) -> None:
        recorder = Mock(async_clear_statistics=Mock())

        result = asyncio.run(
            recorder_helpers.async_clear_statistics(recorder, [])
        )

        self.assertTrue(result)
        recorder.async_clear_statistics.assert_not_called()

    def test_failed_prevalidation_does_not_queue_clear(self) -> None:
        recorder = Mock(async_clear_statistics=Mock())
        enqueue_followup = Mock()
        invalid_metadata = {
            "mean_type": "none",
            "source": "smart1_ems",
            "statistic_id": "invalid statistic id",
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        }
        statistics = [
            {
                "start": datetime(2026, 8, 4, tzinfo=timezone.utc),
                "state": 1.0,
                "sum": 1.0,
            }
        ]

        with self.assertRaisesRegex(ValueError, "statistic_id"):
            asyncio.run(
                recorder_helpers.async_clear_statistics(
                    recorder,
                    ["smart1_ems:a"],
                    validate_followup=lambda: (
                        recorder_helpers.validate_energy_statistics_imports(
                            [(invalid_metadata, statistics)]
                        )
                    ),
                    enqueue_followup=enqueue_followup,
                )
            )

        recorder.async_clear_statistics.assert_not_called()
        enqueue_followup.assert_not_called()

    def test_prevalidation_checks_all_batches_before_clear(self) -> None:
        recorder = Mock(async_clear_statistics=Mock())
        valid_metadata = {
            "mean_type": "none",
            "source": "smart1_ems",
            "statistic_id": "smart1_ems:grid_import_deadbeef",
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        }
        invalid_metadata = {
            **valid_metadata,
            "statistic_id": "smart1_ems:wallbox-consumption",
        }
        statistics = [
            {
                "start": datetime(2026, 8, 4, tzinfo=timezone.utc),
                "state": 1.0,
                "sum": 1.0,
            }
        ]

        with self.assertRaisesRegex(ValueError, "statistic_id"):
            asyncio.run(
                recorder_helpers.async_clear_statistics(
                    recorder,
                    ["smart1_ems:a", "smart1_ems:b"],
                    validate_followup=lambda: (
                        recorder_helpers.validate_energy_statistics_imports(
                            [
                                (valid_metadata, statistics),
                                (invalid_metadata, statistics),
                            ]
                        )
                    ),
                    enqueue_followup=Mock(),
                )
            )

        recorder.async_clear_statistics.assert_not_called()


if __name__ == "__main__":
    unittest.main()
