import asyncio
import os
import socket
import subprocess
import sys
import time

import httpx


def test_stdio_subprocess_initializes_and_reads_backend(tmp_path):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "BROWSERBUDDY_DATA_DIR": str(tmp_path),
        "BROWSERBUDDY_PORT": str(port),
        "BROWSERBUDDY_SEMANTIC_ENABLED": "false",
        "BROWSERBUDDY_AUTO_DIGESTS": "false",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "browserbuddy.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "error",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.poll() is None, process.stderr.read().decode()
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            raise AssertionError("test backend did not start")

        async def run():
            transport = StdioTransport(
                command=sys.executable,
                args=["-c", "from browserbuddy.cli import app; app()", "mcp"],
                env=env,
            )
            async with Client(transport) as client:
                tools = await client.list_tools()
                assert len(tools) == 7
                result = await client.call_tool("list_sessions", {})
                assert not result.is_error
                assert "items" in str(result)

        asyncio.run(run())
    finally:
        process.terminate()
        process.wait(timeout=5)
        process.stderr.close()
