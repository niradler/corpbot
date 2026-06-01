"""corpbot — a thin nanobot plugin that routes boxy MCP tool calls to a per-user sandbox.

The plugin exposes boxy's tools (``bash``/``read_file``/``write_file``/``edit_file``) as
nanobot ``Tool`` subclasses. Each tool is ``ContextAware``: nanobot calls ``set_context`` once
per inbound message (in that message's asyncio task) with the trusted ``RequestContext``, and
the tool derives the per-user sandbox id from ``ctx.chat_id`` (the trusted Slack user id). The
sandbox id is carried to boxy on the ``X-Sandbox-Id`` header — never from the model or tool
arguments (confused-deputy boundary).
"""
from __future__ import annotations

from corpbot.routing import (
    SANDBOX_HEADER,
    current_sandbox_id,
    sanitize_sandbox_id,
    set_current_sandbox_id,
)
from corpbot.tools import (
    BashTool,
    EditFileTool,
    ReadFileTool,
    SandboxRoutingError,
    SandboxToolInvoker,
    WriteFileTool,
)

__all__ = [
    "SANDBOX_HEADER",
    "current_sandbox_id",
    "sanitize_sandbox_id",
    "set_current_sandbox_id",
    "BashTool",
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "SandboxRoutingError",
    "SandboxToolInvoker",
]
