"""End-to-end proof of per-user X-Sandbox-Id routing over the real MCP transport.

Drives nanobot's actual MCP client stack (connect_mcp_servers + MCPToolWrapper +
SandboxToolInvoker) against a live local MCP-over-HTTP server, and asserts each user's tool
call carries only their own sandbox id — sequentially AND concurrently (no cross-user leak),
plus fail-closed when no id is in context.
"""
from __future__ import annotations

import asyncio

import pytest

from nanobot.agent.tools.mcp import connect_mcp_servers
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import MCPServerConfig
from nanobot.security.sandbox_routing import apply_sandbox_id, _reset_for_tests

import boxy_mock


@pytest.fixture(scope="module")
def mock_boxy():
    server = boxy_mock.MockBoxyServer().start()
    yield server
    server.stop()


def test_per_user_routing_over_real_transport(mock_boxy):
    async def scenario():
        registry = ToolRegistry()
        cfg = MCPServerConfig(
            url=mock_boxy.mcp_url, type="streamableHttp", inject_sandbox_id=True
        )
        stacks = await connect_mcp_servers({"boxy": cfg}, registry)
        try:
            tool = registry.get("mcp_boxy_whoami")
            assert tool is not None, "boxy whoami tool was not registered"

            # 1) Sequential: each message routes to its own sandbox.
            apply_sandbox_id("UALICE")
            assert await tool.execute() == "u-ualice"
            apply_sandbox_id("UBOB")
            assert await tool.execute() == "u-ubob"

            # 2) Concurrent: four users dispatched as separate tasks (mirrors nanobot's
            #    asyncio.create_task per message). No id may leak across users.
            async def call(uid: str) -> str:
                apply_sandbox_id(uid)
                return await tool.execute()

            results = await asyncio.gather(
                call("Ualice"), call("Ubob"), call("Ucarol"), call("Udave")
            )
            assert results == ["u-ualice", "u-ubob", "u-ucarol", "u-udave"], results

            # 3) Fail closed: no sandbox id in context -> the call is refused.
            _reset_for_tests()
            refused = await tool.execute()
            assert "SandboxRoutingError" in refused or "failed" in refused.lower(), refused
        finally:
            for stack in stacks.values():
                if stack is not None:
                    await stack.aclose()

    asyncio.run(scenario())
