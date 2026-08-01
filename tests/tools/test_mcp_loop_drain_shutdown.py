"""Regression tests for clean MCP event-loop shutdown (JAC-4192 / Flash lane).

The Flash lane (``ollama-cloud``) surfaced a post-run breadcrumb::

    Exception ignored in: <coroutine object ...>
    RuntimeError: Event loop is closed

Root cause: ``_stop_mcp_loop`` closed the background MCP event loop while
tasks were still suspended on it (e.g. a parked reconnect waiter). Their
coroutines were left for the garbage collector, whose finalizer then resumed
them to run cleanup against an already-closed loop.

The fix drains — cancels and reaps — every pending task on the loop's own
thread *before* closing it. These tests prove the drain happens and that no
coroutine is abandoned for the GC to finalize.
"""

import asyncio
import gc
import threading
import warnings

import tools.mcp_tool as mcp


class TestMcpLoopDrainShutdown:
    def test_drain_cancels_and_reaps_pending_task(self):
        """`_drain_mcp_loop_tasks` cancels a suspended task and closes its coroutine."""

        async def _run():
            async def _forever():
                await asyncio.Event().wait()

            task = asyncio.ensure_future(_forever())
            await asyncio.sleep(0)  # let it start and suspend on the Event
            assert not task.done()
            await mcp._drain_mcp_loop_tasks(timeout=1.0)
            return task

        task = asyncio.run(_run())
        # Drained: the task was cancelled+reaped, not left running.
        assert task.done()
        assert task.cancelled()

    def test_drain_no_pending_is_noop(self):
        """Draining an idle loop returns immediately without error."""

        async def _run():
            await mcp._drain_mcp_loop_tasks(timeout=1.0)

        asyncio.run(_run())  # must not raise

    def test_stop_mcp_loop_drains_parked_task_no_coroutine_leak(self):
        """`_stop_mcp_loop` drains a parked task before closing — no GC breadcrumb.

        Starts the real background MCP loop, parks a coroutine suspended on an
        Event forever (the shape of a reconnect waiter), then stops the loop.
        The parked task must end up cancelled (drained on its owning thread)
        and must NOT trigger a "coroutine was never awaited" warning, which is
        the observable proxy for the "Event loop is closed" finalizer crash.
        """
        mcp._ensure_mcp_loop()
        loop = mcp._mcp_loop
        assert loop is not None and loop.is_running()

        started = threading.Event()
        holder: dict = {}

        async def _parked():
            holder["task"] = asyncio.current_task()
            started.set()
            await asyncio.Event().wait()

        # Schedule onto the running background loop from this (sync) thread.
        asyncio.run_coroutine_threadsafe(_parked(), loop)
        assert started.wait(timeout=5), "parked coroutine never started"

        parked_task = holder["task"]
        assert not parked_task.done()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = mcp._stop_mcp_loop()
            gc.collect()

        assert result is True
        assert loop.is_closed()
        # Drained on the loop thread rather than abandoned for the GC.
        assert parked_task.done()
        assert parked_task.cancelled()
        # Global refs cleared so a later start re-creates a fresh loop.
        assert mcp._mcp_loop is None
        assert mcp._mcp_thread is None

        leaked = [
            w for w in caught
            if issubclass(w.category, RuntimeWarning)
            and "was never awaited" in str(w.message)
        ]
        assert leaked == [], (
            "coroutine(s) abandoned to the GC during shutdown: "
            f"{[str(w.message) for w in leaked]}"
        )
