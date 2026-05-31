"""Per-user sandbox routing for the boxy MCP server (corpbot).

boxy routes every ``/mcp`` request to a per-user nsjail sandbox by the ``X-Sandbox-Id``
header. nanobot keeps ONE shared MCP session per server and sends client->server POSTs from a
connection-scoped background task, and it dispatches each inbound message as its own asyncio
task. A single shared session with a mutated header would therefore race across concurrent
users and could leak one user's sandbox to another.

:class:`SandboxToolInvoker` avoids that by opening a **fresh, short-lived MCP connection per
tool call**, in the caller's own task, whose header carries the current message's sandbox id.
This is:

* concurrency-safe — no shared session or mutable header between users;
* anyio-safe — each connection's task group is entered and exited in the same task;
* fail-closed — a call with no sandbox id in context raises instead of routing to a wrong or
  default sandbox.

boxy state persists server-side (keyed by ``X-Sandbox-Id``), so reconnecting per call reuses
the same sandbox and refreshes its sliding TTL. The per-call MCP handshake trades a little
latency for correctness; a pooled per-user session is a later optimization.
"""
from __future__ import annotations

from typing import Any

import httpx

from nanobot.security.sandbox_routing import SANDBOX_HEADER, current_sandbox_id


class SandboxRoutingError(RuntimeError):
    """Raised when a sandbox-routed tool is called with no sandbox id in context (fail closed)."""


class SandboxToolInvoker:
    """Opens a per-call boxy MCP connection scoped to the current user's sandbox id."""

    def __init__(self, url: str, base_headers: dict[str, str], tool_timeout: int):
        self._url = url
        self._base_headers = dict(base_headers or {})
        self._tool_timeout = tool_timeout

    def _headers_for(self, sandbox_id: str) -> dict[str, str]:
        return {**self._base_headers, SANDBOX_HEADER: sandbox_id}

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Connect with the current sandbox id, run the tool, and return the MCP result."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        sandbox_id = current_sandbox_id()
        if not sandbox_id:
            raise SandboxRoutingError(
                "no sandbox id in context — refusing to route a boxy tool call (fail closed)"
            )

        async with httpx.AsyncClient(
            headers=self._headers_for(sandbox_id),
            follow_redirects=True,
            timeout=None,
        ) as client:
            async with streamable_http_client(self._url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.call_tool(tool_name, arguments=arguments)
