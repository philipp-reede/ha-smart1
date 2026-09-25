"""Small async helpers for Home Assistant Recorder operations."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

RECORDER_OPERATION_TIMEOUT = 10


async def async_clear_statistics(
    recorder: Any,
    statistic_ids: Iterable[str],
) -> bool:
    """Clear statistics and wait until Recorder confirms the transaction."""
    ordered_ids = sorted(set(statistic_ids))
    if not ordered_ids:
        return True

    done_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _clear_done() -> None:
        loop.call_soon_threadsafe(done_event.set)

    recorder.async_clear_statistics(ordered_ids, on_done=_clear_done)
    try:
        await asyncio.wait_for(
            done_event.wait(),
            timeout=RECORDER_OPERATION_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return False
    return True
