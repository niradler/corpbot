"""nanobot Tool subclasses that route boxy's tools to a per-user sandbox (corpbot plugin).

boxy routes every ``/mcp`` request to a per-user nsjail sandbox by the ``X-Session-Id`` header
(the user) plus the shared ``X-Sandbox-Id`` config id. A single shared MCP session with a mutated
header would race across concurrent users and could leak one user's sandbox to another.
:class:`SandboxToolInvoker` avoids that by opening a **fresh, short-lived MCP connection per tool
call**, in the caller's own asyncio task, whose headers carry the current message's session id.
This is:

* concurrency-safe — no shared session or mutable header between users;
* anyio-safe — each connection's task group is entered and exited in the same task;
* fail-closed — a call with no sandbox id in context raises instead of routing to a wrong or
  default sandbox.

boxy state persists server-side (keyed by ``X-Session-Id``), so reconnecting per call reuses
the same per-user sandbox and refreshes its sliding TTL.

The plugin **is** the boxy client: nanobot does not configure boxy as an ``mcpServers`` entry,
so no nanobot MCP wrapper is involved.

Configuration (all read from the environment, resolved **lazily per call** so rotation works):

* ``BOXY_MCP_URL`` — boxy-router ``/mcp`` endpoint (defaults to the in-cluster service).
* ``BOXY_ROUTER_TOKEN`` — static bearer token, OR
* ``BOXY_ROUTER_TOKEN_FILE`` — path to a file holding the bearer (e.g. a projected Kubernetes
  ServiceAccount token that rotates on disk). When set it takes precedence and is re-read on
  every call so token rotation is picked up without a restart.
* ``BOXY_MCP_TIMEOUT_SECONDS`` — per-call HTTP timeout (default 120).

A missing/unreadable token yields an **empty** auth header — the request still fails closed at
boxy (which validates via TokenReview), so we never silently route with stale or absent auth.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import RequestContext

from corpbot.routing import (
    SANDBOX_HEADER,
    SESSION_HEADER,
    current_session_id,
    sandbox_config_id,
    set_current_session_id,
)

# Defaults match the in-cluster boxy-router service; override via env at deploy time.
DEFAULT_BOXY_MCP_URL = "http://boxy-router.boxy.svc.cluster.local:8080/mcp"
DEFAULT_TIMEOUT_SECONDS = 120.0

_log = logging.getLogger("corpbot.tools")


def _boxy_url() -> str:
    return os.environ.get("BOXY_MCP_URL", DEFAULT_BOXY_MCP_URL)


def _resolve_token() -> str | None:
    """Resolve the boxy bearer token at call time (so rotation is picked up).

    Precedence: ``BOXY_ROUTER_TOKEN_FILE`` (re-read fresh each call, supports rotating
    projected SA tokens) over the static ``BOXY_ROUTER_TOKEN``. Returns ``None`` if neither
    yields a non-empty value or the file cannot be read.
    """
    token_file = os.environ.get("BOXY_ROUTER_TOKEN_FILE")
    if token_file:
        try:
            with open(token_file, encoding="utf-8") as f:
                token = f.read().strip()
        except OSError as exc:
            _log.warning("boxy router token file %r unreadable: %s", token_file, exc)
            return None
        if not token:
            _log.warning("boxy router token file %r is empty", token_file)
        return token or None
    token = os.environ.get("BOXY_ROUTER_TOKEN")
    return token or None


def _auth_headers() -> dict[str, str]:
    """Build the auth header lazily. Empty when no token — still fails closed at boxy."""
    token = _resolve_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _timeout_seconds() -> float:
    """Per-call HTTP timeout (seconds) from env, falling back to the default."""
    raw = os.environ.get("BOXY_MCP_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        _log.warning("invalid BOXY_MCP_TIMEOUT_SECONDS %r; using default %s", raw, DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0:
        _log.warning("non-positive/invalid BOXY_MCP_TIMEOUT_SECONDS %r; using default %s", raw, DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS
    return value


class SandboxRoutingError(RuntimeError):
    """Raised when a sandbox-routed tool is called with no sandbox id in context (fail closed)."""


class BoxyToolError(RuntimeError):
    """Raised when boxy returns a tool-level error result (``isError``)."""


def _result_text(result: Any) -> str:
    """Flatten an MCP ``CallToolResult`` to text (joining any text content blocks)."""
    content = getattr(result, "content", None)
    if content is None:
        return str(result)
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    return "\n".join(parts)


class SandboxToolInvoker:
    """Opens a per-call boxy MCP connection scoped to the current user's sandbox id.

    URL, auth token, and timeout are resolved **lazily per call** (not at init) so that a
    rotating projected SA token (``BOXY_ROUTER_TOKEN_FILE``) is re-read fresh on every request.
    Tests may pin ``url`` and ``base_headers`` to bypass env-based auth.
    """

    def __init__(self, url: str | None = None, base_headers: dict[str, str] | None = None):
        self._url = url
        # Explicit override (e.g. tests pass {}); ``None`` means "resolve auth per call".
        self._base_headers = dict(base_headers) if base_headers is not None else None

    def _headers_for(self, session_id: str) -> dict[str, str]:
        base = self._base_headers if self._base_headers is not None else _auth_headers()
        return {**base, SESSION_HEADER: session_id, SANDBOX_HEADER: sandbox_config_id()}

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Connect with the current user's session id + the shared config id, run the tool, and
        return result text.

        Fails closed: if there is no per-user session id in the current context, raises
        :class:`SandboxRoutingError` rather than routing an unrouted (or default) request.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        session_id = current_session_id()
        if not session_id:
            raise SandboxRoutingError(
                "no session id in context — refusing to route a boxy tool call (fail closed)"
            )

        async with httpx.AsyncClient(
            headers=self._headers_for(session_id),
            follow_redirects=True,
            timeout=_timeout_seconds(),
        ) as client:
            url = self._url or _boxy_url()
            async with streamable_http_client(url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments=arguments)
        text = _result_text(result)
        if getattr(result, "isError", False):
            raise BoxyToolError(text or f"boxy tool {tool_name!r} returned an error")
        return text


# One shared invoker per process. It is stateless apart from the URL/base headers; the
# per-user session id is read from the contextvar at call time, so sharing is safe.
_invoker = SandboxToolInvoker()


class _BoxyTool(Tool):
    """Base for boxy-backed tools: ContextAware routing + delegate to the shared invoker.

    Subclasses set ``boxy_tool_name`` (the tool name on boxy's MCP server), ``name``,
    ``description``, and (via :func:`tool_parameters`) ``parameters``.
    """

    boxy_tool_name: str = ""

    #: Optional per-instance invoker (tests point this at a mock boxy server); ``None`` uses the
    #: process-wide :data:`_invoker`.
    _override_invoker: SandboxToolInvoker | None = None

    def set_context(self, ctx: RequestContext) -> None:
        """nanobot calls this once per message; derive the per-user session id from the trusted chat id."""
        set_current_session_id(ctx.chat_id)

    def _invoker_override(self, invoker: SandboxToolInvoker) -> None:
        """Point this tool instance at a specific invoker (used by tests)."""
        self._override_invoker = invoker

    @classmethod
    def enabled(cls, ctx: Any) -> bool:  # noqa: ARG003 - ctx is the nanobot ToolContext
        return True

    async def execute(self, **kwargs: Any) -> str:
        invoker = self._override_invoker or _invoker
        return await invoker.call_tool(self.boxy_tool_name, kwargs)


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to run in the user's sandbox via bash.",
            },
        },
        "required": ["command"],
    }
)
class BashTool(_BoxyTool):
    boxy_tool_name = "bash"
    name = "bash"
    description = "Run a bash command inside the user's isolated sandbox."


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path of the file to read (confined to /workspace).",
            },
        },
        "required": ["path"],
    }
)
class ReadFileTool(_BoxyTool):
    boxy_tool_name = "read_file"
    name = "read_file"
    description = "Read a file from the user's sandbox workspace."


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path of the file to write (confined to /workspace).",
            },
            "content": {
                "type": "string",
                "description": "Full contents to write to the file.",
            },
        },
        "required": ["path", "content"],
    }
)
class WriteFileTool(_BoxyTool):
    boxy_tool_name = "write_file"
    name = "write_file"
    description = "Write (create or overwrite) a file in the user's sandbox workspace."


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path of the file to edit (confined to /workspace).",
            },
            "old_string": {
                "type": "string",
                "description": "Exact text to find and replace.",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text.",
            },
        },
        "required": ["path", "old_string", "new_string"],
    }
)
class EditFileTool(_BoxyTool):
    boxy_tool_name = "edit_file"
    name = "edit_file"
    description = "Edit a file in the user's sandbox workspace by replacing a string."
