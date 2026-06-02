"""corpbot — a thin nanobot plugin that routes boxy MCP tool calls to a per-user sandbox.

The plugin exposes boxy's tools (``bash``/``read_file``/``write_file``/``edit_file``) as
nanobot ``Tool`` subclasses. Each tool is ``ContextAware``: nanobot calls ``set_context`` once
per inbound message (in that message's asyncio task) with the trusted ``RequestContext``, and
the tool derives the per-user **session id** from ``ctx.chat_id`` (the trusted Slack user id).
That session id is carried to boxy on the ``X-Session-Id`` header — never from the model or tool
arguments (confused-deputy boundary) — alongside the shared ``X-Sandbox-Id`` config id. boxy
provisions one isolated sandbox per user session from that single shared config.
"""
from __future__ import annotations

from corpbot.routing import (
    SANDBOX_HEADER,
    SESSION_HEADER,
    current_session_id,
    sandbox_config_id,
    sanitize_session_id,
    set_current_session_id,
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
    "SESSION_HEADER",
    "current_session_id",
    "sandbox_config_id",
    "sanitize_session_id",
    "set_current_session_id",
    "BashTool",
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "SandboxRoutingError",
    "SandboxToolInvoker",
]
