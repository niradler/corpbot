"""Real live proof against boxy running on the local kind cluster.

Creates two per-user sandboxes via boxy's REST API, then drives per-user `bash` over MCP
through nanobot's real routing code (`SandboxToolInvoker`) and asserts each Slack user runs in
their OWN nsjail sandbox with an isolated, persistent `/workspace` — plus fail-closed when no
trusted id is set.

Prereq: a port-forward to boxy-router, e.g.
    kubectl port-forward -n boxy svc/boxy-router 18080:8080
The router token is read from the `boxy-tokens` secret via kubectl (kept in memory only).

Run:  .venv/Scripts/python.exe scripts/live_boxy_routing.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess

for _n in ("mcp", "httpx", "uvicorn", "asyncio"):
    logging.getLogger(_n).setLevel(logging.CRITICAL)
from loguru import logger as _lg  # noqa: E402

_lg.remove()

import httpx  # noqa: E402

from nanobot.agent.tools.mcp import connect_mcp_servers  # noqa: E402
from nanobot.agent.tools.registry import ToolRegistry  # noqa: E402
from nanobot.config.schema import MCPServerConfig  # noqa: E402
from nanobot.security.sandbox_routing import (  # noqa: E402
    apply_sandbox_id,
    sanitize_sandbox_id,
    _reset_for_tests,
)

BASE = os.environ.get("BOXY_URL", "http://127.0.0.1:18080")
NS = os.environ.get("BOXY_NS", "boxy")
SA = os.environ.get("BOXY_SA", "boxy-e2e-client")
SLACK_USERS = ["U07ALICE", "U07BOB"]


def router_token() -> str:
    # This boxy deploy authenticates via K8s SA bound tokens (TokenReview), so mint one.
    return subprocess.check_output(
        ["kubectl", "create", "token", SA, "-n", NS, "--duration=3600s"]
    ).decode().strip()


def flat(text: str) -> str:
    return " | ".join(line for line in text.splitlines() if line.strip())


async def main() -> int:
    auth = {"Authorization": f"Bearer {router_token()}"}
    sandboxes = {u: sanitize_sandbox_id(u) for u in SLACK_USERS}

    print(f"boxy router: {BASE}")
    async with httpx.AsyncClient(headers=auth, timeout=60) as client:
        for uid, sid in sandboxes.items():
            # No allowedBinaries: bash/coreutils are already in boxy's Ubuntu rootfs;
            # allowedBinaries is only for extra tools bind-mounted from the controller.
            body = {"sandboxId": sid, "ttlSeconds": 3600}
            resp = await client.post(f"{BASE}/v1/sandboxes", json=body)
            created = resp.status_code in (200, 201, 409)
            note = "(already exists)" if resp.status_code == 409 else ""
            print(f"  create {sid:<12} HTTP {resp.status_code} {note} "
                  f"[{'ok' if created else 'FAIL ' + resp.text}]")
            if not created:
                return 1

    registry = ToolRegistry()
    cfg = MCPServerConfig(
        url=f"{BASE}/mcp", type="streamableHttp", headers=auth,
        inject_sandbox_id=True, tool_timeout=120,
    )
    stacks = await connect_mcp_servers({"boxy": cfg}, registry)
    ok = True
    try:
        print("\ndiscovered tools (no id sent):", sorted(registry.tool_names))
        bash_tool = registry.get("mcp_boxy_bash")
        assert bash_tool is not None, "boxy bash tool not registered"

        apply_sandbox_id("U07ALICE")
        alice = await bash_tool.execute(
            command="whoami; hostname; echo alice-secret > /workspace/marker.txt; cat /workspace/marker.txt"
        )
        print("\n[alice] ", flat(alice))

        apply_sandbox_id("U07BOB")
        bob = await bash_tool.execute(
            command="hostname; cat /workspace/marker.txt 2>/dev/null || echo NO_MARKER; "
                    "echo bob-secret > /workspace/marker.txt; cat /workspace/marker.txt"
        )
        print("[bob]   ", flat(bob))

        apply_sandbox_id("U07ALICE")
        alice2 = await bash_tool.execute(command="hostname; cat /workspace/marker.txt")
        print("[alice2]", flat(alice2))

        isolation = ("alice-secret" not in bob) and ("alice-secret" in alice2) and ("bob-secret" not in alice2)
        print("\nIsolation:", "PASS — bob never saw alice's /workspace; alice's data persisted"
              if isolation else "FAIL — cross-user leak or lost data")
        ok = ok and isolation

        _reset_for_tests()
        refused = await bash_tool.execute(command="echo should-not-run")
        fail_closed = "SandboxRoutingError" in refused or "failed" in refused.lower()
        print("Fail-closed (no id):", repr(refused), "[ok]" if fail_closed else "[FAIL]")
        ok = ok and fail_closed
    finally:
        for stack in stacks.values():
            if stack is not None:
                await stack.aclose()

    print("\nRESULT:", "PASS — real boxy on kind, per-user sandbox isolation proven" if ok else "FAIL")
    print("cleanup (optional; sandboxes also expire via TTL):")
    for sid in sandboxes.values():
        print(f"  curl -XDELETE -H 'Authorization: Bearer <token>' {BASE}/v1/sandboxes/{sid}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
