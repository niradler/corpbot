# corpbot — Architecture

Single-tenant AI agent. Slack is the front door; every action the model takes runs inside a
per-user nsjail sandbox managed by boxy on Kubernetes. nanobot is the brain and the only
process with outbound network access (for the LLM API). The sandbox is isolated.

## Implementation (how the pieces are sourced)

- **nanobot is stock** (`nanobot-ai`, no fork). Per-user sandbox routing — historically "A4" —
  is implemented as a **thin nanobot plugin**, the published `corpbot` package. It registers
  boxy's tools (`bash`/`read_file`/`write_file`/`edit_file`) via the `nanobot.tools` entry
  point group; each tool is `ContextAware`, so nanobot sets the trusted per-message
  `RequestContext` on it before the turn, and the plugin derives the per-user session id from
  `chat_id`. **The plugin IS the boxy MCP client** — boxy is not configured as a nanobot
  `mcpServers` entry, so no nanobot MCP wrapper is involved. Configure via env `BOXY_MCP_URL`
  and `BOXY_ROUTER_TOKEN`. See `src/corpbot/` (`routing.py`, `tools.py`).
- **Built-in tools off** — historically "A1" — is config only: set `tools.exec.enable=false`,
  `tools.web.enable=false`, `tools.file.enable=false`. The `file.enable` flag lands via
  upstream nanobot PR [HKUDS/nanobot#4138](https://github.com/HKUDS/nanobot/pull/4138).
- **boxy** is consumed as a **pinned published Helm chart** (release with per-user session
  provisioning, [niradler/boxy#6](https://github.com/niradler/boxy/pull/6)) — not forked or vendored.
- **S3 `/workspace` persistence is deferred** to a later milestone (see Persistence below).

## Flow

1. A Slack message arrives. nanobot's Slack channel carries the **trusted Slack user id**
   (`event.user`) and conversation type (`channel_type == "im"` for DMs) in the per-message
   context metadata. Note `RequestContext.chat_id` is the **conversation** id (the DM channel for
   a DM, the channel id for a channel message) — not the user id.
2. nanobot sets the trusted per-message `RequestContext` on every `ContextAware` tool (the
   `corpbot` plugin's boxy tools) in that message's asyncio task, before the turn runs.
3. When the model calls a boxy tool, the plugin opens a fresh boxy `/mcp` connection for that
   call carrying two headers: `X-Session-Id: u-<sanitized-key>` (the routing key, derived from
   **trusted identity** per the scope rules below — never from tool arguments) and
   `X-Sandbox-Id: <config id>` (the shared `Sandbox` config, a deploy-time value).
   - **DMs** key on the Slack user id (always per-user — a DM is 1:1).
   - **Channels** follow `sandbox.sessionScope` (env `BOXY_SESSION_SCOPE`): `per-user` (default)
     keys on the user id so every member is isolated even in a shared channel; `per-channel`
     keys on the channel id so the whole channel shares one sandbox.
4. boxy-router finds no session for that key yet → provisions one bound to the named config
   (the shared `Sandbox` CR) → `setupScript` restores `/workspace` from `s3://<bucket>/u-<user>/`.
   Later calls reuse the running session.
5. Tool calls run in the jail. **Each exec refreshes the sliding 1h TTL.**
6. After **1h idle** the sandbox expires → `teardownScript` syncs `/workspace` back to S3 →
   the sandbox is reaped.

## Sequence

```
Slack          nanobot                         boxy-router            sandbox (nsjail)        S3
  │  message      │                                  │                       │                 │
  ├──────────────►│                                  │                       │                 │
  │   chat_id =   │                                  │                       │                 │
  │   slack uid   │  agent loop → first tool call    │                       │                 │
  │               ├─ MCP connect /mcp ──────────────►│                       │                 │
  │               │  X-Session-Id: u-<uid>           │                       │                 │
  │               │  X-Sandbox-Id: <config>          │  no session yet?      │                 │
  │               │                                  ├─ provision from config CR              │
  │               │                                  ├─ setupScript: s3 restore ──────────────►│
  │               │                                  │                       │◄── /workspace ──┤
  │               │  tool result                     │  exec in jail ───────►│                 │
  │               │◄─────────────────────────────────┤  (refresh TTL)        │                 │
  │◄──────────────┤  reply                           │                       │                 │
  │               │                                  │                       │                 │
  │            (idle 1h)                             │  TTL expires          │                 │
  │               │                                  ├─ teardownScript: s3 sync ───────────────►│
  │               │                                  └─ reap sandbox         X                 │
```

## Identity & security model

- **Routing key** is `u-<sanitized-key>`, carried as the `X-Session-Id` header. **DMs** key on the
  Slack user id (always per-user). **Channels** follow `sandbox.sessionScope` (`BOXY_SESSION_SCOPE`):
  `per-user` (default) keys on the user id, `per-channel` keys on the channel id. The `X-Sandbox-Id`
  header carries the shared `Sandbox` **config** id — a deploy-time value, never derived from the user.
- **Derivation:** the plugin's `set_context` (called by nanobot per message) pulls the trusted
  user id and conversation type from `RequestContext.metadata["slack"]` (`event.user`,
  `channel_type`); `RequestContext.chat_id` is the conversation id used for `per-channel` scope.
  It resolves the key per the scope rules, stores the sanitized session id in a `contextvar`, and
  injects it on the `X-Session-Id` header of every boxy MCP call. Each inbound message is its own
  asyncio task, so the `contextvar` is concurrency-safe across users.
- **It must never come from the LLM or from tool arguments.** Letting the model choose the
  session id would be a confused-deputy vulnerability (one user reading another's workspace).
- **Sanitization** (`sanitize_session_id`): lowercase, keep only `[a-z0-9-]`, prefix
  `u-`, cap at 63 chars (boxy uses `X-Session-Id` verbatim as the k8s RFC1123 label-backed
  Session name). Slack ids are uppercase, so lowercasing matters.
- **Path confinement:** boxy's file tools confine every path to `/workspace` and reject `..`,
  absolute escapes, and symlinks that point outside the workspace.
- **Fail closed, both sides:** nanobot refuses to call boxy without a trusted session id
  (`SandboxRoutingError`), so it never routes an unidentified user. The deploy also **disables
  boxy's default sandbox** (`defaultSandbox.enabled: false`), so a request with no headers at all
  yields a tool-error rather than touching a shared sandbox. Config selection is explicit: the
  named `Sandbox` config CR must exist (boxy never auto-creates one).
- **Id hygiene is the client's job:** boxy validates the `X-Session-Id` format on session create
  but does not sanitize it, so nanobot pre-sanitizes to boxy's contract
  (`^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$`, ≤63 chars).

## Sandbox lifecycle

| Phase | Trigger | What happens |
|-------|---------|--------------|
| Provision | `/mcp` request with a new `X-Session-Id` (bound to the `X-Sandbox-Id` config) | Create the per-user session from the shared config CR with a fresh `/workspace`. _(Deferred: `setupScript` S3 restore.)_ |
| Active | Each exec / tool call | Sliding TTL refreshed to 1h. |
| Expire | 1h idle | Sandbox reaped. _(Deferred: `teardownScript` syncs `/workspace` → S3 first.)_ |

Sandbox config (rendered as a boxy `Sandbox` CR — `deploy/templates/sandbox.yaml`; one shared
config, every per-user session is built from it):

```json
{
  "ttlSeconds": 3600,
  "network": { "enabled": true, "allowInternetAccess": false },
  "allowedBinaries": [],
  "vm": {
    "memoryMb": 512,
    "rlimits": [
      { "resource": "nproc",  "soft": 256 },
      { "resource": "nofile", "soft": 1024 }
    ]
  },
  "setupScript": "/opt/boxy/scripts/s3-restore.sh",
  "teardownScript": "/opt/boxy/scripts/s3-sync.sh",
  "scriptEnv": { "S3_BUCKET": "<bucket>" }
}
```

`sandboxId` = the shared config id (`X-Sandbox-Id`); the per-user `Session` is keyed by
`X-Session-Id`. `allowedBinaries` lists **only** extra binaries baked into the controller image
at `/usr/local/bin` (the rootfs already provides bash/coreutils) — empty by default; see
`deploy/values.yaml` `sandbox.allowedBinaries`. TTL is **sliding** (refreshed per exec).

## Persistence

> **Deferred for v1.** v1 runs with boxy's **ephemeral, per-sandbox `/workspace`** (each
> sandbox gets a fresh workspace; data does not survive a reap). The S3 backup/restore flow
> below — including the `setupScript`/`teardownScript` hooks, the sequence diagram's S3 lane,
> and the lifecycle table's S3 steps — is a **later milestone**. When implemented it would run
> on a **derived** boxy-controller image (AWS CLI + hook scripts), **not** a boxy source fork.
> boxy itself stays a deployed, pinned published dependency.

- `/workspace` is the per-user workspace directory inside the jail.
- _(Deferred)_ On provision, `setupScript` runs `aws s3 sync s3://$S3_BUCKET/$BOXY_SANDBOX_ID/ $BOXY_WORKSPACE/ || true`
  (the `|| true` keeps a brand-new user from failing provisioning).
- _(Deferred)_ On teardown, `teardownScript` runs `aws s3 sync $BOXY_WORKSPACE/ s3://$S3_BUCKET/$BOXY_SANDBOX_ID/`.
- _(Deferred)_ **Scripts run on the controller** (which has network + S3 credentials via
  IRSA/instance role), **not in the jail** (which stays internet-isolated). AWS CLI would be
  pre-baked into the derived controller image.

## Network model

- The **jail has no internet** (`allowInternetAccess: false`).
- The **LLM API calls originate from the nanobot process**, which runs outside the jail and
  has egress.
- boxy's S3 sync runs on the **controller**, also outside the jail.

## Limits

- boxy exec output caps at **6 MB** (`BOXY_MAX_OUTPUT_BYTES`) and truncates with a `200 OK`.
  For large output, use the **stream endpoint** or the **file tools** (which read directly
  from `/workspace` and avoid the exec output cap).

## Out of scope (v1)

- **MCP gateway (Obot)** — only for later, when onboarding third-party MCP servers that need
  per-user OAuth.
- **Multi-tenant node pools** — single company, trusted users only.
