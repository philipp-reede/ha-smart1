from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).parents[1]
SPEC = spec_from_file_location(
    "smart1_recorder_helpers_test_module",
    ROOT / "custom_components" / "smart1_ems" / "recorder_helpers.py",
)
assert SPEC is not None and SPEC.loader is not None
recorder_helpers = module_from_spec(SPEC)
SPEC.loader.exec_module(recorder_helpers)


class Smart1RecorderHelpersTest(unittest.TestCase):
    def test_statistics_persistence_requires_every_expected_value(self) -> None:
        first = datetime(2026, 8, 4, tzinfo=timezone.utc)
        second = datetime(2026, 8, 4, 1, tzinfo=timezone.utc)
        expected = [
            {"start": first, "state": 1.0, "sum": 1.0},
            {"start": second, "state": 2.0, "sum": 3.0},
        ]
        observed = [
            {"start": first.timestamp(), "state": 1.0, "sum": 1.0},
            {"start": second.timestamp(), "state": 2.0, "sum": 3.0},
        ]

        self.assertTrue(
            recorder_helpers.statistics_are_persisted(expected, observed)
        )
        observed[-1]["sum"] = 2.5
        self.assertFalse(
            recorder_helpers.statistics_are_persisted(expected, observed)
        )

    def test_wait_for_recorder_commit_uses_queue_barrier(self) -> None:
        recorder = Mock(async_block_till_done=AsyncMock())

        result = asyncio.run(
            recorder_helpers.async_wait_for_recorder_commit(recorder)
        )

        self.assertTrue(result)
        recorder.async_block_till_done.assert_awaited_once_with()

    def test_wait_for_recorder_commit_timeout_is_not_completion(self) -> None:
        async def never_finishes() -> None:
            await asyncio.Event().wait()

        recorder = Mock(
            async_block_till_done=AsyncMock(side_effect=never_finishes)
        )

        with patch.object(
            recorder_helpers,
            "RECORDER_OPERATION_TIMEOUT",
            0.001,
        ):
            result = asyncio.run(
                recorder_helpers.async_wait_for_recorder_commit(recorder)
            )

        self.assertFalse(result)

    def test_wait_for_recorder_commit_propagates_cancellation(self) -> None:
        recorder = Mock(
            async_block_till_done=AsyncMock(
                side_effect=asyncio.CancelledError
            )
        )

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(
                recorder_helpers.async_wait_for_recorder_commit(recorder)
            )

    def test_statistics_readback_retries_stale_result(self) -> None:
        start = datetime(2026, 8, 4, tzinfo=timezone.utc)
        expected = [{"start": start, "state": 1.0, "sum": 1.0}]
        readback = AsyncMock(
            side_effect=(
                [{"start": start.timestamp(), "state": 9.0, "sum": 9.0}],
                [{"start": start.timestamp(), "state": 1.0, "sum": 1.0}],
            )
        )

        result = asyncio.run(
            recorder_helpers.async_wait_for_statistics_readback(
                expected,
                readback,
            )
        )

        self.assertTrue(result)
        self.assertEqual(readback.await_count, 2)

    def test_statistics_readback_propagates_cancellation(self) -> None:
        start = datetime(2026, 8, 4, tzinfo=timezone.utc)

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(
                recorder_helpers.async_wait_for_statistics_readback(
                    [{"start": start, "state": 1.0, "sum": 1.0}],
                    AsyncMock(side_effect=asyncio.CancelledError),
                )
            )

    def test_statistics_readback_bounds_retries_and_hanging_query(self) -> None:
        start = datetime(2026, 8, 4, tzinfo=timezone.utc)
        expected = [{"start": start, "state": 1.0, "sum": 1.0}]
        stale_readback = AsyncMock(
            return_value=[
                {"start": start.timestamp(), "state": 9.0, "sum": 9.0}
            ]
        )

        with patch.object(
            recorder_helpers,
            "RECORDER_OPERATION_TIMEOUT",
            0.001,
        ):
            self.assertFalse(
                asyncio.run(
                    recorder_helpers.async_wait_for_statistics_readback(
                        expected,
                        stale_readback,
                    )
                )
            )
        self.assertLessEqual(stale_readback.await_count, 3)

        async def never_returns():
            await asyncio.Event().wait()

        with patch.object(
            recorder_helpers,
            "RECORDER_OPERATION_TIMEOUT",
            0.001,
        ):
            self.assertFalse(
                asyncio.run(
                    recorder_helpers.async_wait_for_statistics_readback(
                        expected,
                        never_returns,
                    )
                )
            )

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

    def test_late_clear_runs_late_finalizer_only_after_timeout(self) -> None:
        callbacks = []
        normal_completion = Mock()
        late_completion = Mock()

        def clear_statistics(_statistic_ids, *, on_done) -> None:
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
                    on_done=normal_completion,
                    on_late_done=late_completion,
                )
            callbacks[0]()
            await asyncio.sleep(0)
            return result

        self.assertFalse(asyncio.run(run_scenario()))
        normal_completion.assert_called_once_with()
        late_completion.assert_called_once_with()

    def test_on_time_clear_does_not_run_late_finalizer(self) -> None:
        late_completion = Mock()

        def clear_statistics(_statistic_ids, *, on_done) -> None:
            on_done()

        recorder = Mock(
            async_clear_statistics=Mock(side_effect=clear_statistics)
        )

        result = asyncio.run(
            recorder_helpers.async_clear_statistics(
                recorder,
                ["smart1_ems:a"],
                on_late_done=late_completion,
            )
        )

        self.assertTrue(result)
        late_completion.assert_not_called()

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

    def test_statistics_validation_uses_utc_hour_boundary(self) -> None:
        metadata = {
            "mean_type": "none",
            "source": "smart1_ems",
            "statistic_id": "smart1_ems:pv_production",
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        }
        kolkata = timezone(timedelta(hours=5, minutes=30))

        with self.assertRaisesRegex(ValueError, "UTC hour boundary"):
            recorder_helpers.validate_energy_statistics_imports(
                [
                    (
                        metadata,
                        [
                            {
                                "start": datetime(2026, 8, 4, tzinfo=kolkata),
                                "state": 1.0,
                                "sum": 1.0,
                            }
                        ],
                    )
                ]
            )

        with self.assertRaisesRegex(ValueError, "on the hour"):
            recorder_helpers.validate_energy_statistics_imports(
                [
                    (
                        metadata,
                        [
                            {
                                # This converts to 19:00Z, but Home Assistant
                                # rejects it before that conversion because the
                                # supplied representation is not on the hour.
                                "start": datetime(
                                    2026,
                                    8,
                                    4,
                                    0,
                                    30,
                                    tzinfo=kolkata,
                                ),
                                "state": 1.0,
                                "sum": 1.0,
                            }
                        ],
                    )
                ]
            )

        recorder_helpers.validate_energy_statistics_imports(
            [
                (
                    metadata,
                    [
                        {
                            "start": datetime(
                                2026,
                                8,
                                4,
                                tzinfo=timezone.utc,
                            ),
                            "state": 1.0,
                            "sum": 1.0,
                        }
                    ],
                )
            ]
        )

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
