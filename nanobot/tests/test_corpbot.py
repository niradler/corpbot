"""corpbot fork tests: A1 (built-in file tools off) and A4 (per-message X-Sandbox-Id routing)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.config.schema import Config, MCPServerConfig, ToolsConfig
from nanobot.agent.tools.filesystem import FileToolsConfig, ReadFileTool
from nanobot.security.sandbox_routing import (
    apply_sandbox_id,
    current_sandbox_id,
    sanitize_sandbox_id,
    _reset_for_tests,
)


# --- A4: sanitize ---------------------------------------------------------

def test_sanitize_lowercases_and_prefixes():
    assert sanitize_sandbox_id("ABC") == "u-abc"


def test_sanitize_strips_disallowed_chars():
    assert sanitize_sandbox_id("ab_c.d!") == "u-abcd"


def test_sanitize_keeps_hyphens_and_digits():
    assert sanitize_sandbox_id("W012-AB") == "u-w012-ab"


def test_sanitize_fails_closed_on_empty():
    assert sanitize_sandbox_id(None) is None
    assert sanitize_sandbox_id("") is None
    assert sanitize_sandbox_id("___...") is None
    assert sanitize_sandbox_id("---") is None


def test_sanitize_caps_at_55_so_session_name_fits_63():
    out = sanitize_sandbox_id("a" * 100)
    assert out.startswith("u-")
    assert len(out) == 55
    assert len(f"{out}-session") <= 63


def test_sanitize_never_emits_leading_or_trailing_separator():
    # Slack ids are alphanumeric, but be robust: no result may start/end with '-' or '.'.
    assert sanitize_sandbox_id("abc-") == "u-abc"
    assert sanitize_sandbox_id("-abc-") == "u-abc"
    out = sanitize_sandbox_id("z" * 54)  # 'u-' + 53 z -> exactly 55, no trailing trim needed
    assert not out.endswith(("-", "."))


def test_sanitize_matches_boxy_contract():
    import re as _re

    pattern = _re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
    for raw in ["U07ALICE", "abc-", "-x-", "A.B_C!", "u" * 80, "U123-456"]:
        out = sanitize_sandbox_id(raw)
        if out is not None:
            assert pattern.match(out), out
            assert len(out) <= 55


# --- A4: per-message application of the trusted id ------------------------

def test_apply_sandbox_id_sets_contextvar():
    _reset_for_tests()
    applied = apply_sandbox_id("U12AB")
    assert applied == "u-u12ab"
    assert current_sandbox_id() == "u-u12ab"
    # Fail-closed: an empty/None id leaves no current sandbox.
    assert apply_sandbox_id(None) is None
    assert current_sandbox_id() is None
    _reset_for_tests()


def test_invoker_fails_closed_without_sandbox_id():
    import asyncio

    from nanobot.agent.tools.mcp_sandbox import SandboxRoutingError, SandboxToolInvoker

    _reset_for_tests()
    invoker = SandboxToolInvoker("http://boxy/mcp", {}, tool_timeout=5)
    with pytest.raises(SandboxRoutingError):
        asyncio.run(invoker.call_tool("bash", {"command": "echo hi"}))


# --- A1: built-in file tools off by default -------------------------------

def test_file_tools_disabled_by_default():
    assert FileToolsConfig().enable is False
    assert Config().tools.file.enable is False


def test_fs_tool_gate_follows_config_flag():
    ctx_off = SimpleNamespace(config=ToolsConfig())
    assert ReadFileTool.enabled(ctx_off) is False

    cfg_on = ToolsConfig()
    cfg_on.file.enable = True
    ctx_on = SimpleNamespace(config=cfg_on)
    assert ReadFileTool.enabled(ctx_on) is True


# --- A4: config flag plumbing ---------------------------------------------

def test_mcp_server_inject_sandbox_id_default_false():
    assert MCPServerConfig().inject_sandbox_id is False


def test_mcp_server_accepts_camelcase_inject_flag():
    cfg = MCPServerConfig.model_validate({"url": "http://boxy/mcp", "injectSandboxId": True})
    assert cfg.inject_sandbox_id is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
