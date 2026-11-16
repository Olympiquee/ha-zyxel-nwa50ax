"""SSH queue: a caller that gave up must not break the queue worker."""
import asyncio
import logging

from ha_zyxel.zyxel_ssh_api import ZyxelSSHAPI


def test_cancelled_caller_does_not_log_invalid_state(caplog):
    async def scenario():
        api = ZyxelSSHAPI("192.0.2.10", "admin", "dummy-password")
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_operation():
            started.set()
            await release.wait()
            return "late result"

        async def fast_operation():
            return "next result"

        # The caller gives up while the operation is still running
        # (coordinator timeout, entry unload, HA shutdown).
        caller = asyncio.create_task(api._queue_ssh_operation(1, "refresh:fast", slow_operation))
        await started.wait()
        caller.cancel()
        await asyncio.sleep(0)
        release.set()

        # The worker must still serve the next operation.
        result = await asyncio.wait_for(api._queue_ssh_operation(1, "refresh:slow", fast_operation), 2)
        await api.async_shutdown()
        return result

    with caplog.at_level(logging.ERROR):
        assert asyncio.run(scenario()) == "next result"
    assert "invalid state" not in caplog.text.lower()
    assert "SSH queue operation" not in caplog.text
