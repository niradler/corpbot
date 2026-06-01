"""nanobot Tool subclasses that route boxy's tools to a per-user sandbox (corpbot plugin).

boxy routes every ``/mcp`` request to a per-user nsjail sandbox by the ``X-Sandbox-Id`` header.
A single shared MCP session with a mutated header would race across concurrent users and could
leak one user's sandbox to another. :class:`SandboxToolInvoker` avoids that by opening a
**fresh, short-lived MCP connection per tool call**, in the caller's own asyncio task, whose
header carries the current message's sandbox id. This is:

* concurrency-safe — no shared session or mutable header between users;
* anyio-safe — each connection's task group is entered and exited in the same task;
* fail-closed — a call with no sandbox id in context raises instead of routing to a wrong or
  default sandbox.

boxy state persists server-side (keyed by ``X-Sandbox-Id``), so reconnecting per call reuses
the same sandbox and refreshes its sliding TTL.

The plugin **is** the boxy client: nanobot does not configure boxy as an ``mcpServers`` entry,
so no nanobot MCP wrapper is involved. ``BOXY_MCP_URL`` and ``BOXY_ROUTER_TOKEN`` are read from
the environment.
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import RequestContext

from corpbot.routing import SANDBOX_HEADER, current_sandbox_id, set_current_sandbox_id

# Defaults match the in-cluster boxy-router service; override via env at deploy time.
DEFAULT_BOXY_MCP_URL = "http://boxy-router.boxy.svc.cluster.local:8080/mcp"


def _boxy_url() -> str:
    return os.environ.get("BOXY_MCP_URL", DEFAULT_BOXY_MCP_URL)


def _base_headers() -> dict[str, str]:
    token = os.environ.get("BOXY_ROUTER_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


class SandboxRoutingError(RuntimeError):
    """Raised when a sandbox-routed tool is called with no sandbox id in context (fail closed)."""


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
    """Opens a per-call boxy MCP connection scoped to the current user's sandbox id."""

    def __init__(self, url: str | None = None, base_headers: dict[str, str] | None = None):
        self._url = url or _boxy_url()
        self._base_headers = dict(base_headers) if base_headers is not None else _base_headers()

    def _headers_for(self, sandbox_id: str) -> dict[str, str]:
        return {**self._base_headers, SANDBOX_HEADER: sandbox_id}

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Connect with the current sandbox id, run the tool, and return result text.

        Fails closed: if there is no sandbox id in the current context, raises
        :class:`SandboxRoutingError` rather than routing an unrouted (or default) request.
        """
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
                    result = await session.call_tool(tool_name, arguments=arguments)
        return _result_text(result)


# One shared invoker per process. It is stateless apart from the URL/base headers; the
# per-user sandbox id is read from the contextvar at call time, so sharing is safe.
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
        """nanobot calls this once per message; derive the sandbox id from the trusted chat id."""
        set_current_sandbox_id(ctx.chat_id)

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
