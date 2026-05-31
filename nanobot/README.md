# nanobot/ (corpbot fork)

The **agent brain + Slack ingress** for corpbot. A fork of
[HKUDS/nanobot](https://github.com/HKUDS/nanobot) (Python), patched so the model acts
**only** through boxy.

> **Status: implemented, tested, and proven live** against real boxy on a local kind
> cluster. A1 (boxy-only surface) + A4 (per-user `X-Sandbox-Id` routing) are shipped here as:
>
> - [`overlay/`](./overlay) — new files to drop into your fork (`security/sandbox_routing.py`, `agent/tools/mcp_sandbox.py`)
> - [`patches/upstream.diff`](./patches/upstream.diff) — edits to `loop.py`, `tools/mcp.py`, `tools/filesystem.py`, `config/schema.py`
> - [`tests/`](./tests) — unit + mock-transport e2e; [`scripts/`](./scripts) — `demo_routing.py` (mock) + `live_boxy_routing.py` (real boxy)
>
> A2/A3 are config (see [`../agent-deploy/nanobot/config.example.json`](../agent-deploy/nanobot/config.example.json)).

## Apply to your HKUDS fork

```bash
# 1. fork + clone HKUDS/nanobot, add upstream for updates
git clone <your-nanobot-fork-url> nanobot && cd nanobot
git remote add upstream https://github.com/HKUDS/nanobot.git && git fetch upstream
# pin to a known-good commit before patching (HKUDS may move files)

# 2. drop in the new files and apply the edits (run from corpbot/nanobot)
cp -r overlay/nanobot/* <fork>/nanobot/
git -C <fork> apply ../patches/upstream.diff

# 3. set up an env and run the tests/demo (no cluster needed — mock transport)
cd <fork> && uv venv && uv pip install -e . pytest
cp ../corpbot/nanobot/tests/* tests/ && cp ../corpbot/nanobot/scripts/* scripts/
python -m pytest tests/test_corpbot.py tests/test_corpbot_routing_e2e.py -q
python scripts/demo_routing.py        # → RESULT: PASS
```

> **Verify the patched file paths against your pinned commit** — `loop.py`,
> `tools/mcp.py`, `tools/filesystem.py`, `config/schema.py` are from the corpbot spec and were
> confirmed against the clone used here; HKUDS may move things.

## Config

Lives at `~/.nanobot/config.json`. The deploy-templated version is in
[`../agent-deploy/nanobot/config.example.json`](../agent-deploy/nanobot/config.example.json).

## Build tasks

### A1 — Disable built-in tools (boxy is the only execution surface) ✅ implemented

The model must not have any local tools.

- `tools.exec.enable: false` — no local shell (also disables `write_stdin` / `list_exec_sessions`).
- `tools.web.enable: false` — no `web_search` / `web_fetch`.
- **Built-in FILE tools have no flag — added one.** Tools self-register through a
  `tool_cls.enabled(ctx)` gate (`tools/loader.py`), so rather than patching
  `_register_default_tools()`, the fork adds a `FileToolsConfig(enable=False)` and gates the
  shared filesystem base class:

```python
# nanobot/agent/tools/filesystem.py
class FileToolsConfig(Base):
    enable: bool = False          # off by default — boxy is the only file surface

class _FsTool(Tool):
    config_key = "file"
    @classmethod
    def config_cls(cls): return FileToolsConfig
    @classmethod
    def enabled(cls, ctx): return ctx.config.file.enable
```

Because every local-FS tool extends `_FsTool`, this one gate disables **read / write / edit /
list / apply_patch** *and* **find_files / grep** (`search.py`). Wired into `ToolsConfig.file`
in `config/schema.py`.

> **Residual always-on tools (your call):** with exec/web/file off, nanobot still registers
> agent-internal tools — `message` (reply to channel), `spawn` (subagents), `cron`,
> `long_task`. None give shell/FS/web/corp-data access, so they don't breach "boxy is the
> only execution surface." `cli_apps` / `image_generation` / `my` are separately
> config-gated (default off). Lock any of these down too if you want a stricter surface.

### A2 — Register boxy as the only MCP server

```json
{
  "tools": {
    "exec": { "enable": false },
    "web":  { "enable": false },
    "mcpServers": {
      "boxy": {
        "url": "http://boxy-router.boxy.svc.cluster.local:8080/mcp",
        "headers": { "Authorization": "Bearer ${BOXY_ROUTER_TOKEN}" },
        "injectSandboxId": true,
        "toolTimeout": 120
      }
    }
  }
}
```

### A3 — Slack channel + allowlist

```json
{
  "channels": {
    "slack": {
      "enabled": true,
      "token": "${SLACK_BOT_TOKEN}",
      "allowFrom": ["<COMPANY_SLACK_USER_IDS>"]
    }
  }
}
```

> **Note:** in current builds an **empty `allowFrom` denies all**. List the company's Slack
> user ids explicitly.

### A4 — The routing patch (load-bearing) ✅ implemented & proven

boxy routes per `X-Sandbox-Id`. Opt in per MCP server with `"injectSandboxId": true` (default
false, so other MCP servers are untouched).

**Why the naive version is unsafe (verified against upstream):** nanobot keeps **one** shared
boxy `ClientSession`, sends client→server POSTs from a **connection-scoped background task**
(`mcp/client/streamable_http.py` `post_writer`), and dispatches **each inbound message as its
own `asyncio` task** (`loop.py:893`). So mutating a shared header per message — or relying on a
`contextvar` the background send task can't see — **races across concurrent users and can leak
one user's sandbox to another.** Unacceptable for the routing key.

**Design — per-call connection scoped to the current user** (`agent/tools/mcp_sandbox.py`):

- `sanitize_sandbox_id(raw)` (`security/sandbox_routing.py`) — lowercase → `[a-z0-9-]` →
  `u-` prefix → **≤55 chars**, no leading/trailing separator. Returns `None` on empty →
  routing **fails closed**. (boxy confirmed it does **not** validate the MCP `X-Sandbox-Id`
  header — a malformed id silently fails to match a sandbox — so emitting a clean id matching
  boxy's `^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$` is on us; 55 keeps the derived `<id>-session` ≤63.)
- `apply_sandbox_id(chat_id)` — called once per message from `_set_tool_context()`
  (`loop.py:500`) with the trusted `chat_id`; stores it in a per-task `contextvar`.
- `SandboxToolInvoker` — for boxy tools, `MCPToolWrapper.execute()` delegates to the invoker,
  which **reads the id in the caller's own task** and opens a **fresh short-lived MCP
  connection whose `X-Sandbox-Id` header is that user's id**, runs the tool, and closes.

This is concurrency-safe (no shared session/header between users), anyio-safe (each
connection's task group opens/closes in the same task), and fails closed (no id → refuse).
boxy state persists server-side by id, so reconnecting per call reuses the sandbox and
refreshes its sliding TTL — a small latency cost; a pooled per-user session is a later
optimization.

> **Security invariant (enforced):** the id comes only from `chat_id` in `_set_tool_context`
> — never from the model or tool arguments (those flow as MCP call params, not headers).
> Confused-deputy boundary — see
> [`../docs/architecture.md`](../docs/architecture.md#identity--security-model).

**Proven two ways:**

- **Mock transport** ([`tests/test_corpbot_routing_e2e.py`](./tests/test_corpbot_routing_e2e.py)) — drives nanobot's actual MCP client against a local MCP server; asserts per-user routing **sequentially and with 4 concurrent users (no leak)**, plus fail-closed. No cluster needed.
- **Real boxy on kind** ([`scripts/live_boxy_routing.py`](./scripts/live_boxy_routing.py)) — creates per-user sandboxes, drives `bash` through the real router; alice/bob get isolated, persistent `/workspace`; fail-closed holds. Requires boxy deployed + a port-forward + the B2 patch ([`../boxy/patches`](../boxy/patches)).

## Checklist

- [x] Patches authored as `overlay/` + `patches/upstream.diff` (apply to a pinned HKUDS fork)
- [x] **A1** — `exec`/`web` disabled via config; `FileToolsConfig(enable=false)` added + `_FsTool` gated (read/write/edit/list/apply_patch/find/grep)
- [ ] A2 — boxy registered as the only MCP server (config; `injectSandboxId: true`)
- [ ] A3 — Slack channel enabled with explicit `allowFrom` (config)
- [x] **A4** — per-user `X-Sandbox-Id` via `SandboxToolInvoker` (per-call connection); opt-in `injectSandboxId`; fed from trusted `chat_id`; **proven concurrency-safe + fail-closed on real transport** (e2e test)
- [x] File paths confirmed against clone: `loop.py:467/500/893`, `tools/mcp.py:177`
- [x] **Concurrency model decided** — per-call connection (no shared mutable header), safe under nanobot's concurrent per-message dispatch. No serialization required.
- [x] **Residual tools decided (v1)** — leave `message`/`spawn`/`cron`/`long_task` on (agent-internal, no shell/fs/web/corp-data); `cli_apps`/`image_generation`/`my` default off. Revisit if a stricter surface is wanted.
- [x] **Discovery path confirmed with boxy** — `initialize`/`tools/list` succeed with only the Bearer token (no `X-Sandbox-Id`), provision nothing, and the tool list is global/static. No discovery id needed.
- [ ] **Deploy guardrail: disable boxy's default sandbox** — boxy fails *open* (routes to a shared default sandbox) on a missing id when `DefaultSandboxEnabled=true`. nanobot fails closed, but set boxy's default sandbox **off** for defense in depth (see `agent-deploy`).
- [x] **Live against real boxy on kind** — per-user `bash` isolation + persistence + fail-closed proven (needs boxy [B2 patch](../boxy/patches))
- [ ] Open a PR to HKUDS/nanobot if you want `tools.file.enable` + MCP per-call header routing upstreamed
