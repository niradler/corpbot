"""Per-message sandbox routing for the boxy MCP connection (corpbot plugin).

corpbot routes every boxy MCP call to a *per-user* nsjail sandbox via the ``X-Sandbox-Id``
HTTP header. The id is derived from the **trusted Slack user id** carried in the per-message
``RequestContext`` (``chat_id``) — never from the model or from tool arguments
(confused-deputy boundary).

The id is stored in a context var, set once per message from the trusted context (see
``corpbot.tools`` `set_context`, which nanobot's ``AgentLoop._set_tool_context`` invokes on
every ``ContextAware`` tool). nanobot dispatches each inbound message as its own asyncio task,
so a context var is naturally isolated per message even under concurrent users. The boxy tool
invoker reads it in the calling task at tool-call time and opens a connection whose header
carries exactly that id.
"""
from __future__ import annotations

import re
from contextvars import ContextVar

SANDBOX_HEADER = "X-Sandbox-Id"
# boxy derives "<id>-session" (a k8s label-backed name, max 63 chars) and does NOT validate
# the MCP X-Sandbox-Id header itself — so producing a valid id is our responsibility. Cap at
# 55 so "<id>-session" stays <= 63, and match boxy's contract:
# ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ (lowercase alphanumeric, with '-'/'.' only in the middle).
_MAX_LEN = 55

# Trusted sandbox id for the current async context (message task). Set per message.
_current_sandbox_id: ContextVar[str | None] = ContextVar("current_sandbox_id", default=None)


def sanitize_sandbox_id(raw: str | None) -> str | None:
    """Map a Slack user id to a boxy/k8s-safe sandbox id.

    Rules: lowercase, keep only ``[a-z0-9-]``, prefix ``u-``, cap at 55 chars, and trim so the
    result always matches boxy's contract ``^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$`` (no leading or
    trailing separator). Slack ids are uppercase, so lowercasing matters.

    Returns ``None`` when there is no usable id, so callers **fail closed** — we never route a
    request to a wrong or empty sandbox. (boxy does not reject a malformed id; it would just
    silently fail to match a sandbox, so emitting a clean id is on us.)
    """
    if not raw:
        return None
    core = re.sub(r"[^a-z0-9-]", "", raw.lower()).strip("-")
    if not core:
        return None
    return f"u-{core}"[:_MAX_LEN].rstrip("-")


def set_current_sandbox_id(chat_id: str | None) -> str | None:
    """Set the active sandbox id for this message from a trusted chat id. Returns the id."""
    sandbox_id = sanitize_sandbox_id(chat_id)
    _current_sandbox_id.set(sandbox_id)
    return sandbox_id


def current_sandbox_id() -> str | None:
    """Return the trusted sandbox id for the current async context (or ``None``)."""
    return _current_sandbox_id.get()


def _reset_for_tests() -> None:
    """Clear the context var (test isolation only)."""
    _current_sandbox_id.set(None)
