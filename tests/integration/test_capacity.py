"""Bounded-output performance checks use actual server RSS, not Python allocations."""

import asyncio
import time

import anyio
import pytest

from terminal_mcp.procinfo import process_rss
from test_mcp_stdio import python_command
from test_restart import connection

pytestmark = pytest.mark.anyio


async def test_wait_latency_under_200ms(tmp_path):
    async with connection(tmp_path) as server:
        release = tmp_path / "produce-output"
        code = (f"import time; from pathlib import Path; gate=Path({str(release)!r})\n"
                "while not gate.exists(): time.sleep(.001)\n"
                "print(time.monotonic_ns(),flush=True); time.sleep(60)")
        result = await server.call("terminal_start", {
            "command": python_command(code), "mode": "pipe",
        })
        waiter = asyncio.create_task(server.call("terminal_read", {"session_id": result["session_id"], "cursor": 0, "wait_ms": 3000}))
        try:
            await anyio.sleep(.05)
            # A later request completes while the earlier read is still pending.
            summary = await server.call("terminal_list", {"session_id": result["session_id"]})
            assert summary["sessions"][0]["output_total_bytes"] == 0
            assert not waiter.done() and not release.exists()
            release.touch()
            page = await waiter
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        latency_ms = (time.monotonic_ns() - int(page["stdout"].strip())) / 1_000_000
        assert latency_ms < 200, f"Observed output-to-client latency: {latency_ms:.3f} ms"
        print(f"wait_latency_ms={latency_ms:.3f}")


def rss(pid):
    value = process_rss(pid)
    assert value is not None
    return value


async def test_50mb_output_is_bounded(tmp_path):
    async with connection(tmp_path) as server:
        # The official transport harness exposes only the process it created.
        path, = (server.state / "sessions").glob("*.json")
        assert int(path.stem.split("-", 1)[0]) == server.process.pid
        await server.call("terminal_list")
        baseline = rss(server.process.pid)
        peak = baseline
        samples = 0
        latencies = []
        code = "import os; os.read(0,1); block=b'x'*8192; remaining=50000000\nwhile remaining:\n chunk=block[:min(remaining,len(block))]; os.write(1,chunk); remaining-=len(chunk)"
        started = await server.call("terminal_start", {"command": python_command(code), "mode": "pipe"})
        sid = started["session_id"]
        await server.call("terminal_write", {"session_id": sid, "text": "!"})
        with anyio.fail_after(8):
            while True:
                peak = max(peak, rss(server.process.pid))
                samples += 1
                before = time.monotonic()
                summary = (await server.call("terminal_list", {"session_id": sid}))["sessions"][0]
                latencies.append(time.monotonic() - before)
                peak = max(peak, rss(server.process.pid))
                assert summary["output_end_cursor"] - summary["output_start_cursor"] <= 1048576
                if summary["status"] != "running":
                    break
                await anyio.sleep(.02)
        assert summary["output_total_bytes"] == 50000000
        assert samples > 1
        assert max(latencies) < .2, f"Server call latency under burst: {max(latencies):.3f} s"
        growth = peak - baseline
        print(f"rss_baseline={baseline} rss_peak={peak} rss_growth={growth} samples={samples} max_call_ms={max(latencies)*1000:.3f}")
        assert growth <= 3 * 1048576, f"Server RSS growth exceeded 3 MiB: {growth} bytes"
        page = await server.call("terminal_read", {"session_id": sid, "cursor": 0, "max_bytes": 4})
        assert page["dropped_bytes"] == 50000000 - 1048576
