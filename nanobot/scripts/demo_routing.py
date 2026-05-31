"""corpbot MVP demo: per-user sandbox routing, end to end, on your machine.

Starts a local stand-in for boxy's /mcp server, connects nanobot's real MCP client stack to
it with `injectSandboxId` enabled, then simulates several Slack users sending a message. Each
user's tool call is routed to *their own* sandbox id (X-Sandbox-Id) — proving the load-bearing
corpbot mechanism without Kubernetes, real boxy, or an LLM key.

Run:  .venv/Scripts/python.exe scripts/demo_routing.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

# Quiet the MCP/httpx/uvicorn request chatter so the routing result is readable.
for _name in ("mcp", "httpx", "uvicorn", "uvicorn.error", "uvicorn.access"):
    logging.getLogger(_name).setLevel(logging.WARNING)
# asyncio logs a benign ConnectionResetError on Windows when the mock server's sockets close.
logging.getLogger("asyncio").setLevel(logging.CRITICAL)

# nanobot logs caught tool errors via loguru; the fail-closed step deliberately triggers one.
from loguru import logger as _loguru  # noqa: E402

_loguru.remove()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))

import boxy_mock  # noqa: E402

from nanobot.agent.tools.mcp import connect_mcp_servers  # noqa: E402
from nanobot.agent.tools.registry import ToolRegistry  # noqa: E402
from nanobot.config.schema import MCPServerConfig  # noqa: E402
from nanobot.security.sandbox_routing import apply_sandbox_id, _reset_for_tests  # noqa: E402

# Simulated Slack user ids (uppercase, as Slack returns them).
SLACK_USERS = ["U07ALICE", "U07BOB", "U07CAROL"]


async def main() -> int:
    server = boxy_mock.MockBoxyServer().start()
    print(f"mock boxy /mcp listening at {server.mcp_url}\n")
    registry = ToolRegistry()
    cfg = MCPServerConfig(url=server.mcp_url, type="streamableHttp", inject_sandbox_id=True)
    stacks = await connect_mcp_servers({"boxy": cfg}, registry)
    ok = True
    try:
        tool = registry.get("mcp_boxy_whoami")

        print("Sequential — one message per user:")
        for uid in SLACK_USERS:
            applied = apply_sandbox_id(uid)            # what AgentLoop._set_tool_context does
            seen = await tool.execute()                # what the model's tool call does
            status = "ok" if seen == applied else "MISMATCH"
            ok = ok and seen == applied
            print(f"  slack {uid:<10} -> routed {applied:<12} server saw {seen:<12} [{status}]")

        print("\nConcurrent — all users at once (must not leak across each other):")

        async def call(uid: str) -> tuple[str, str]:
            applied = apply_sandbox_id(uid)
            return applied, await tool.execute()

        for applied, seen in await asyncio.gather(*(call(u) for u in SLACK_USERS)):
            status = "ok" if seen == applied else "LEAK"
            ok = ok and seen == applied
            print(f"  routed {applied:<12} server saw {seen:<12} [{status}]")

        print("\nFail-closed — no trusted id in context:")
        _reset_for_tests()
        refused = await tool.execute()
        refused_ok = "SandboxRoutingError" in refused or "failed" in refused.lower()
        ok = ok and refused_ok
        print(f"  call refused: {refused!r} [{'ok' if refused_ok else 'NOT REFUSED'}]")
    finally:
        for stack in stacks.values():
            if stack is not None:
                await stack.aclose()
        server.stop()

    print("\nRESULT:", "PASS — every user reached only their own sandbox" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
