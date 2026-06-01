"""Proves the corpbot plugin tools route per-user, isolate concurrent users, and fail closed.

Drives the plugin's actual ``Tool`` subclasses + :class:`SandboxToolInvoker` against a live
local MCP-over-HTTP server (``boxy_mock``), asserting each user's tool call carries only their
own sandbox id — sequentially AND concurrently (no cross-user leak), plus fail-closed when no
id is in context. Also checks the sanitizer matches boxy's id contract.
"""
from __future__ import annotations

import asyncio
import re

import pytest

import boxy_mock
from corpbot.routing import _reset_for_tests, sanitize_sandbox_id
from corpbot.tools import (
    BashTool,
    EditFileTool,
    ReadFileTool,
    SandboxRoutingError,
    SandboxToolInvoker,
    WriteFileTool,
)
from nanobot.agent.tools.context import ContextAware, RequestContext


@pytest.fixture(scope="module")
def mock_boxy():
    server = boxy_mock.MockBoxyServer().start()
    yield server
    server.stop()


def _ctx(uid: str) -> RequestContext:
    return RequestContext(channel="slack", chat_id=uid)


def _invoker(mock_boxy) -> SandboxToolInvoker:
    # Point the shared invoker at the mock; no auth header needed for the mock.
    return SandboxToolInvoker(url=mock_boxy.mcp_url, base_headers={})


def test_tools_are_context_aware():
    # The plugin tools must implement nanobot's ContextAware protocol so AgentLoop sets context.
    for cls in (BashTool, ReadFileTool, WriteFileTool, EditFileTool):
        assert isinstance(cls(), ContextAware), cls.__name__


def test_bash_routes_to_per_user_sandbox(mock_boxy):
    async def scenario():
        invoker = _invoker(mock_boxy)
        tool = BashTool()
        tool._invoker_override(invoker)
        tool.set_context(_ctx("U07ALICE"))
        return await tool.execute(command="echo hi")

    _reset_for_tests()
    assert asyncio.run(scenario()) == "u-u07alice"
    _reset_for_tests()


def test_file_tools_route(mock_boxy):
    async def scenario():
        invoker = _invoker(mock_boxy)
        read, write, edit = ReadFileTool(), WriteFileTool(), EditFileTool()
        for t in (read, write, edit):
            t._invoker_override(invoker)
        read.set_context(_ctx("UBOB"))
        r = await read.execute(path="a.txt")
        write.set_context(_ctx("UBOB"))
        w = await write.execute(path="a.txt", content="x")
        edit.set_context(_ctx("UBOB"))
        e = await edit.execute(path="a.txt", old_string="x", new_string="y")
        return r, w, e

    _reset_for_tests()
    assert asyncio.run(scenario()) == ("u-ubob", "u-ubob", "u-ubob")
    _reset_for_tests()


def test_concurrent_users_do_not_leak(mock_boxy):
    invoker = _invoker(mock_boxy)

    async def scenario():
        # Each coroutine is its own task (asyncio.gather), mirroring nanobot's per-message task.
        async def call(uid: str) -> str:
            tool = BashTool()
            tool._invoker_override(invoker)
            tool.set_context(_ctx(uid))
            return await tool.execute(command="id")

        return await asyncio.gather(call("Ualice"), call("Ubob"), call("Ucarol"))

    _reset_for_tests()
    results = asyncio.run(scenario())
    assert results == ["u-ualice", "u-ubob", "u-ucarol"], results
    _reset_for_tests()


def test_fails_closed_without_context(mock_boxy):
    async def scenario():
        invoker = _invoker(mock_boxy)
        tool = BashTool()
        tool._invoker_override(invoker)
        # No set_context -> no sandbox id in context.
        await tool.execute(command="echo hi")

    _reset_for_tests()
    with pytest.raises(SandboxRoutingError):
        asyncio.run(scenario())
    _reset_for_tests()


def test_sanitize_matches_boxy_contract():
    pattern = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
    assert sanitize_sandbox_id("U07ALICE") == "u-u07alice"
    assert sanitize_sandbox_id("ab_c.d!") == "u-abcd"
    assert sanitize_sandbox_id("W012-AB") == "u-w012-ab"
    assert sanitize_sandbox_id(None) is None
    assert sanitize_sandbox_id("___...") is None
    out = sanitize_sandbox_id("a" * 100)
    assert len(out) == 55 and len(f"{out}-session") <= 63
    for raw in ["U07ALICE", "abc-", "-x-", "A.B_C!", "u" * 80, "U123-456"]:
        s = sanitize_sandbox_id(raw)
        if s is not None:
            assert pattern.match(s), s
            assert len(s) <= 55


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
