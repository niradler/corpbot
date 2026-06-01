"""Minimal local stand-in for boxy's ``/mcp`` server — for plugin routing tests.

Exposes the boxy tools the corpbot plugin calls (``bash``/``read_file``/``write_file``/
``edit_file``). Each tool returns the ``X-Sandbox-Id`` header the request carried, so a caller
can prove per-user routing actually reaches the server over the real MCP streamable-HTTP
transport. This is NOT boxy — just enough MCP to exercise the wire.
"""
from __future__ import annotations

import socket
import threading
import time

import uvicorn
from mcp.server.fastmcp import Context, FastMCP


def _seen_sandbox(ctx: Context) -> str:
    request = ctx.request_context.request
    sandbox = request.headers.get("x-sandbox-id") if request is not None else None
    return sandbox or "<none>"


def build_app():
    """Build the FastMCP streamable-HTTP ASGI app exposing boxy's tools."""
    mcp = FastMCP("boxy-mock", stateless_http=True)

    @mcp.tool(description="Run a bash command; returns the X-Sandbox-Id the server received.")
    async def bash(command: str, ctx: Context) -> str:
        return _seen_sandbox(ctx)

    @mcp.tool(description="Read a file; returns the X-Sandbox-Id the server received.")
    async def read_file(path: str, ctx: Context) -> str:
        return _seen_sandbox(ctx)

    @mcp.tool(description="Write a file; returns the X-Sandbox-Id the server received.")
    async def write_file(path: str, content: str, ctx: Context) -> str:
        return _seen_sandbox(ctx)

    @mcp.tool(description="Edit a file; returns the X-Sandbox-Id the server received.")
    async def edit_file(path: str, old_string: str, new_string: str, ctx: Context) -> str:
        return _seen_sandbox(ctx)

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
