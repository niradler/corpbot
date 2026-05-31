"""Minimal local stand-in for boxy's ``/mcp`` server — for routing tests and the demo.

Exposes a single ``whoami`` tool that returns the ``X-Sandbox-Id`` header the request
carried. That lets a caller prove per-user routing actually reaches the server over the real
MCP streamable-HTTP transport. This is NOT boxy — just enough MCP to exercise the wire.
"""
from __future__ import annotations

import socket
import threading
import time

import uvicorn
from mcp.server.fastmcp import Context, FastMCP


def build_app():
    """Build the FastMCP streamable-HTTP ASGI app exposing ``whoami``."""
    mcp = FastMCP("boxy-mock", stateless_http=True)

    @mcp.tool(description="Return the X-Sandbox-Id header the server received.")
    async def whoami(ctx: Context) -> str:
        request = ctx.request_context.request
        sandbox = request.headers.get("x-sandbox-id") if request is not None else None
        return sandbox or "<none>"

    return mcp.streamable_http_app()


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class MockBoxyServer:
    """Runs the mock boxy MCP server on a background thread (its own event loop)."""

    def __init__(self, port: int | None = None):
        self.port = port or free_port()
        self._server = uvicorn.Server(
            uvicorn.Config(build_app(), host="127.0.0.1", port=self.port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def start(self, timeout: float = 15.0) -> "MockBoxyServer":
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("mock boxy server did not start in time")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
