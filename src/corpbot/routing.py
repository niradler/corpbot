"""Per-message sandbox routing for the boxy MCP connection (corpbot plugin).

boxy routes each ``/mcp`` call with two headers that play distinct roles:

* ``X-Session-Id`` — the runtime key boxy uses to pick/create the sandbox. corpbot derives it from
  **trusted identity** in the per-message ``RequestContext`` (never from the model or tool
  arguments — confused-deputy boundary), scoped per the rules below. One reused sandbox per key.
* ``X-Sandbox-Id`` — the **config** to build that sandbox from: the id of a boxy ``Sandbox`` CR
  (the shape — allowed binaries, vm, network, TTL). It is the same for every user (a deploy-time
  value), so one shared config serves all users without a CR per user.

Session scoping (which trusted id becomes ``X-Session-Id``):

* **DMs are always per-user** — a direct message is inherently 1:1, so the key is the Slack user id.
* **Channels are configurable** via ``BOXY_SESSION_SCOPE`` (see :data:`DEFAULT_SESSION_SCOPE`):
  ``per-user`` (default) gives every member their own sandbox even inside a shared channel;
  ``per-channel`` shares one sandbox across the whole channel (keyed by the conversation id).

The resolved session id is stored in a context var, set once per message from the trusted context
(see ``corpbot.tools`` ``set_context``). nanobot dispatches each inbound message as its own asyncio
task, so the context var is naturally isolated per message even under concurrent users. The boxy
tool invoker reads it at tool-call time and opens a connection whose headers carry exactly that
session id plus the configured config id.
"""
from __future__ import annotations

import os
import re
from contextvars import ContextVar

# The per-user runtime key (X-Session-Id) and the shared config selector (X-Sandbox-Id).
SESSION_HEADER = "X-Session-Id"
SANDBOX_HEADER = "X-Sandbox-Id"

# boxy uses X-Session-Id verbatim as the Session name, a k8s label-backed name (max 63 chars,
# ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$). boxy does not sanitize the header, so producing a valid id is
# our responsibility.
_MAX_LEN = 63

# Config CR id used when BOXY_SANDBOX_CONFIG_ID is unset. Matches the conventional template id and
# the corpbot chart's sandbox.sandboxId default.
DEFAULT_SANDBOX_CONFIG_ID = "default"

# Session scope for CHANNEL messages (env BOXY_SESSION_SCOPE). DMs are ALWAYS per-user (a DM is
# inherently 1:1), so this knob only changes channel behaviour. Default is the documented
# "one sandbox per user" intent — the isolating, secure choice.
DEFAULT_SESSION_SCOPE = "per-user"
_SESSION_SCOPE_ENV = "BOXY_SESSION_SCOPE"
_SESSION_SCOPES = ("per-user", "per-channel")

# Trusted per-user session id for the current async context (message task). Set per message.
_current_session_id: ContextVar[str | None] = ContextVar("current_session_id", default=None)


def sanitize_session_id(raw: str | None) -> str | None:
    """Map a Slack user id to a boxy/k8s-safe per-user session id.

    Rules: lowercase, keep only ``[a-z0-9-]``, prefix ``u-``, cap at 63 chars, and trim so the
    result always matches boxy's contract ``^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$`` (no leading or
    trailing separator). Slack ids are uppercase, so lowercasing matters.

    Returns ``None`` when there is no usable id, so callers **fail closed** — we never route a
    request to a wrong or empty user session.
    """
    if not raw:
        return None
    core = re.sub(r"[^a-z0-9-]", "", raw.lower()).strip("-")
    if not core:
        return None
    return f"u-{core}"[:_MAX_LEN].rstrip("-")


def session_scope() -> str:
    """Return the channel session scope from the environment (``per-user`` | ``per-channel``).

    Resolved lazily per message so a ``helm upgrade`` is picked up without a restart. Unknown or
    empty values fall back to :data:`DEFAULT_SESSION_SCOPE`.
    """
    val = os.environ.get(_SESSION_SCOPE_ENV, "").strip().lower()
    return val if val in _SESSION_SCOPES else DEFAULT_SESSION_SCOPE


def resolve_session_id(
    user_id: str | None, conversation_id: str | None, is_dm: bool
) -> str | None:
    """Pick the trusted raw key for this message and sanitize it into a boxy session id.

    DMs are always keyed by the user (1:1). Channels follow :func:`session_scope`: ``per-user``
    keys by the user (isolated), ``per-channel`` keys by the conversation (shared). The user id is
    preferred with the conversation id as a fallback so a missing field still fails closed rather
    than cross-routing. Returns ``None`` when no usable id exists (callers fail closed).
    """
    if is_dm or session_scope() == "per-user":
        raw = user_id or conversation_id
    else:
        raw = conversation_id or user_id
    return sanitize_session_id(raw)


def set_current_session_id(chat_id: str | None) -> str | None:
    """Set the active per-user session id for this message from a trusted chat id. Returns the id."""
    session_id = sanitize_session_id(chat_id)
    _current_session_id.set(session_id)
    return session_id


def set_current_session(
    user_id: str | None, conversation_id: str | None, is_dm: bool
) -> str | None:
    """Resolve (per scope) and set the active session id for this message. Returns the id."""
    session_id = resolve_session_id(user_id, conversation_id, is_dm)
    _current_session_id.set(session_id)
    return session_id


def current_session_id() -> str | None:
    """Return the trusted per-user session id for the current async context (or ``None``)."""
    return _current_session_id.get()


def sandbox_config_id() -> str:
    """Return the shared sandbox **config** id (X-Sandbox-Id) from the environment.

    Resolved lazily per call so a ``helm upgrade`` that changes ``sandbox.sandboxId`` is picked up
    without rebuilding state. Falls back to :data:`DEFAULT_SANDBOX_CONFIG_ID`.
    """
    return os.environ.get("BOXY_SANDBOX_CONFIG_ID", "").strip() or DEFAULT_SANDBOX_CONFIG_ID


def _reset_for_tests() -> None:
    """Clear the context var (test isolation only)."""
    _current_session_id.set(None)
