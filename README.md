# corpbot

> A tiny [nanobot](https://github.com/HKUDS/nanobot) plugin that gives every Slack user their **own isolated execution sandbox**.

corpbot is the thin glue for a **self-hosted, single-tenant AI agent**: employees talk to it in Slack, and every tool the model runs (`bash`, file edits, …) executes inside a **per-user [nsjail](https://github.com/google/nsjail) sandbox** managed by [boxy](https://github.com/niradler/boxy) on Kubernetes. Each user is routed to their own sandbox by their Slack user id — never the model's choice — so one person can never touch another's workspace.

It is **not a fork**. corpbot is a small Python package that plugs into stock nanobot and stock boxy:

- **nanobot** (the agent brain + Slack ingress) runs as-is; corpbot registers as a tool plugin.
- **boxy** (the sandbox runtime) runs as-is; corpbot is its per-user MCP client.

> [!NOTE]
> Per-user routing has been verified live against real boxy on a local `kind` cluster: two users each ran `bash` in their own nsjail sandbox with an isolated, persistent `/workspace`, with no cross-user leakage and fail-closed behaviour when no user id is present.

## How it works

```text
Slack message
  → nanobot agent loop (asserts the Slack user id as chat_id)
    → corpbot plugin tool (bash / read_file / write_file / edit_file)
      → boxy /mcp  with header  X-Sandbox-Id: u-<slack-user-id>
        → that user's nsjail sandbox on Kubernetes
```

The model only ever decides *which tool to call with what arguments*. corpbot takes the **trusted** Slack user id from nanobot's per-message context, sanitizes it into a Kubernetes-safe sandbox id, and stamps it on every boxy request — so the routing key can never come from the model or a tool argument.

## Features

- **Per-user isolation** — each Slack user maps to a dedicated boxy sandbox keyed by `X-Sandbox-Id`.
- **No forks** — works with the published `nanobot-ai` package and the published boxy chart; corpbot is ~200 lines of Python.
- **Concurrency-safe** — every tool call opens a fresh, short-lived MCP connection in the caller's own task, so concurrent users never share a connection or a mutable header.
- **Fail-closed** — a tool call with no trusted user id is refused, never routed to a default/shared sandbox.
- **Boxy is the only execution surface** — nanobot's built-in shell/web/file tools are disabled by config; the model can only act through the sandbox.

## Components

| Component | What it is | How it ships |
|-----------|------------|--------------|
| `corpbot` (this repo) | nanobot tool plugin that routes boxy tools per user | `pip install corpbot` |
| [nanobot](https://github.com/HKUDS/nanobot) | Agent brain + Slack ingress | `pip install nanobot-ai` (stock) |
| [boxy](https://github.com/niradler/boxy) | Per-user nsjail sandbox runtime | Helm chart `oci://ghcr.io/niradler/charts/boxy` (pinned) |

The plugin registers four tools via the `nanobot.tools` entry-point group: `boxy_bash`, `boxy_read_file`, `boxy_write_file`, `boxy_edit_file`.

## Getting started

### Prerequisites

- Python 3.11+
- A Kubernetes cluster running boxy (see [boxy](https://github.com/niradler/boxy)); locally, `kind` works.
- A Slack app bot token and an LLM provider key.

### 1. Install

```bash
pip install nanobot-ai corpbot
```

### 2. Point corpbot at boxy

corpbot reads two environment variables (defaults target the in-cluster boxy-router service):

| Variable | Default | Description |
|----------|---------|-------------|
| `BOXY_MCP_URL` | `http://boxy-router.boxy.svc.cluster.local:8080/mcp` | boxy's MCP endpoint |
| `BOXY_ROUTER_TOKEN` | *(none)* | Bearer token for boxy-router |

### 3. Configure nanobot

In `~/.nanobot/config.json`, disable the built-in tools so the model acts **only** through boxy, and enable Slack with an explicit allowlist:

```json
{
  "tools": {
    "exec": { "enable": false },
    "web":  { "enable": false },
    "file": { "enable": false }
  },
  "channels": {
    "slack": {
      "enabled": true,
      "token": "${SLACK_BOT_TOKEN}",
      "allowFrom": ["<COMPANY_SLACK_USER_IDS>"]
    }
  }
}
```

Because corpbot is installed, its boxy tools are auto-discovered — you do **not** add boxy as an `mcpServers` entry.

> [!IMPORTANT]
> `tools.file.enable` requires nanobot with [HKUDS/nanobot#4138](https://github.com/HKUDS/nanobot/pull/4138). Until that release, pin a nanobot build that includes the flag (`exec` and `web` already have theirs).

### 4. Deploy

boxy first, then nanobot. See [`agent-deploy/`](./agent-deploy) for Helm values, the templated nanobot config, the sandbox template, and k8s manifests.

```bash
helm install boxy oci://ghcr.io/niradler/charts/boxy \
  --version <PINNED_VERSION> -n boxy --create-namespace \
  -f agent-deploy/helm/boxy-values.example.yaml
```

> [!IMPORTANT]
> Pin a boxy release that includes per-user routing ([niradler/boxy#5](https://github.com/niradler/boxy/pull/5)), and deploy with the default sandbox **disabled** so a missing id fails closed instead of touching a shared sandbox.

## Security model

- **Routing key** is `u-<sanitized-slack-user-id>`, carried in the `X-Sandbox-Id` header and derived **only** from nanobot's trusted per-message `chat_id` — never the model or tool arguments (confused-deputy boundary).
- **Sanitization** matches boxy's contract: lowercase, `^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$`, prefixed `u-`, ≤55 chars (so the derived `<id>-session` stays within the 63-char Kubernetes label limit).
- **Fail closed on both sides** — corpbot refuses to call boxy without a trusted id; the deploy disables boxy's default sandbox so an id-less request returns an error rather than a shared sandbox.
- **No internet in the jail** — boxy sandboxes run with `allowInternetAccess: false`; only the nanobot process reaches the LLM API.

## Development

```bash
git clone https://github.com/niradler/corpbot.git
cd corpbot
uv venv && uv pip install -e . pytest uvicorn
uv run pytest -q
```

The test suite ([`tests/`](./tests)) runs a local mock boxy MCP server and asserts per-user routing for every tool, concurrent isolation across multiple users, fail-closed behaviour, and id sanitization — no cluster or LLM key required.

## Status and roadmap

| Item | State |
|------|-------|
| Per-user routing (A4) | Done — this plugin, verified live |
| Disable built-in tools (A1) | Config-only; file flag via [HKUDS/nanobot#4138](https://github.com/HKUDS/nanobot/pull/4138) |
| boxy per-user session routing | Upstream [niradler/boxy#5](https://github.com/niradler/boxy/pull/5) — pin a release that includes it |
| S3-backed `/workspace` persistence | Deferred — v1 sandboxes use an ephemeral per-sandbox workspace |
| MCP gateway for shared read-only corp sources | Out of scope for v1 |

## Learn more

- [`docs/architecture.md`](./docs/architecture.md) — flow, identity & security model, sandbox lifecycle, limits
- [`agent-deploy/`](./agent-deploy) — Helm values, nanobot config, k8s manifests
