"""Unit tests for the corpbot plugin's lazy boxy auth/timeout resolution.

Covers per-call token resolution: the static ``BOXY_ROUTER_TOKEN`` env var, the rotating
``BOXY_ROUTER_TOKEN_FILE`` (read fresh on each call so rotation is picked up), file precedence
over the static var, and the fail-closed empty-header case when no token is available. Also
covers the configurable ``BOXY_MCP_TIMEOUT_SECONDS``.
"""
from __future__ import annotations

import pytest

from corpbot.tools import (
    DEFAULT_TIMEOUT_SECONDS,
    _auth_headers,
    _resolve_token,
    _timeout_seconds,
)

_TOKEN_VARS = ("BOXY_ROUTER_TOKEN", "BOXY_ROUTER_TOKEN_FILE")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (*_TOKEN_VARS, "BOXY_MCP_TIMEOUT_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    yield


def test_static_token_from_env(monkeypatch):
    monkeypatch.setenv("BOXY_ROUTER_TOKEN", "static-abc")
    assert _resolve_token() == "static-abc"
    assert _auth_headers() == {"Authorization": "Bearer static-abc"}


def test_no_token_yields_empty_auth_header():
    # Fail closed: empty header (boxy rejects via TokenReview) rather than guessing.
    assert _resolve_token() is None
    assert _auth_headers() == {}


def test_token_file_is_read_and_stripped(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("  file-token-1\n", encoding="utf-8")
    monkeypatch.setenv("BOXY_ROUTER_TOKEN_FILE", str(token_file))
    assert _resolve_token() == "file-token-1"
    assert _auth_headers() == {"Authorization": "Bearer file-token-1"}


def test_token_file_is_reread_each_call_for_rotation(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("v1", encoding="utf-8")
    monkeypatch.setenv("BOXY_ROUTER_TOKEN_FILE", str(token_file))
    assert _resolve_token() == "v1"
    # Simulate kubelet rotating the projected token on disk.
    token_file.write_text("v2", encoding="utf-8")
    assert _resolve_token() == "v2"


def test_token_file_takes_precedence_over_static(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("from-file", encoding="utf-8")
    monkeypatch.setenv("BOXY_ROUTER_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("BOXY_ROUTER_TOKEN", "from-env")
    assert _resolve_token() == "from-file"


def test_missing_token_file_fails_closed(tmp_path, monkeypatch):
    # An unreadable file path yields no token (empty header), not a crash.
    monkeypatch.setenv("BOXY_ROUTER_TOKEN_FILE", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("BOXY_ROUTER_TOKEN", "should-be-ignored")
    assert _resolve_token() is None
    assert _auth_headers() == {}


def test_empty_token_file_fails_closed(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("   \n", encoding="utf-8")
    monkeypatch.setenv("BOXY_ROUTER_TOKEN_FILE", str(token_file))
    assert _resolve_token() is None
    assert _auth_headers() == {}


def test_timeout_default_and_override(monkeypatch):
    assert _timeout_seconds() == DEFAULT_TIMEOUT_SECONDS
    monkeypatch.setenv("BOXY_MCP_TIMEOUT_SECONDS", "30")
    assert _timeout_seconds() == 30.0
    # Invalid values fall back to the default rather than crashing a tool call.
    monkeypatch.setenv("BOXY_MCP_TIMEOUT_SECONDS", "not-a-number")
    assert _timeout_seconds() == DEFAULT_TIMEOUT_SECONDS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
