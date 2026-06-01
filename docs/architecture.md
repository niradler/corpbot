# corpbot — Architecture

Single-tenant AI agent. Slack is the front door; every action the model takes runs inside a
per-user nsjail sandbox managed by boxy on Kubernetes. nanobot is the brain and the only
process with outbound network access (for the LLM API). The sandbox is isolated.

## Flow

1. A Slack message arrives. The Slack channel sets `chat_id` = the **Slack user id**
   (trusted, taken from channel context).
2. The agent loop runs. On the **first tool use**, nanobot connects to boxy `/mcp` and
   registers boxy's tools (`bash`, plus the file tools).
3. Each MCP request carries a header `X-Sandbox-Id: u-<sanitized-slack-user-id>`, injected
   from the **trusted per-message context** — never from tool arguments.
4. boxy-router sees an **unknown sandbox id** → provisions one from a template →
   `setupScript` restores `/workspace` from `s3://<bucket>/u-<user>/`.
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
  │               │  X-Sandbox-Id: u-<uid>           │                       │                 │
  │               │                                  │  unknown id?          │                 │
  │               │                                  ├─ provision from template               │
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

- **Routing key** is `u-<sanitized-slack-user-id>`, carried as the `X-Sandbox-Id` header.
- **Derivation:** the Slack user id comes from the per-message **channel context** that
  nanobot trusts. It is injected into every MCP request by the nanobot MCP wrapper.
- **It must never come from the LLM or from tool arguments.** Letting the model choose the
  sandbox id would be a confused-deputy vulnerability (one user reading another's workspace).
- **Sanitization** (`sanitize(chat_id)`): lowercase, keep only `[a-z0-9-]`, prefix `u-`,
  truncate to 63 chars (k8s RFC1123 label). Slack ids are uppercase, so lowercasing matters.
- **Path confinement:** boxy's file tools confine every path to `/workspace` and reject `..`,
  absolute escapes, and symlinks that point outside the workspace.
- **Fail closed, both sides:** nanobot refuses to call boxy without a trusted id
  (`SandboxRoutingError`). boxy does **not** validate the MCP `X-Sandbox-Id` header and would
  route a missing id to a shared **default sandbox** if one is enabled — so the deploy
  **disables boxy's default sandbox** (`defaultSandbox.enabled: false`). A missing id then
  yields a tool-error, never a shared sandbox.
- **Id hygiene is the client's job:** since boxy doesn't validate the header, nanobot
  sanitizes to boxy's contract (`^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$`, ≤55 chars so the derived
  `<id>-session` stays ≤63).

## Sandbox lifecycle

| Phase | Trigger | What happens |
|-------|---------|--------------|
| Provision | `/mcp` request with an unknown `X-Sandbox-Id` | Create sandbox from template with a fresh `/workspace`. _(Deferred: `setupScript` S3 restore.)_ |
| Active | Each exec / tool call | Sliding TTL refreshed to 1h. |
| Expire | 1h idle | Sandbox reaped. _(Deferred: `teardownScript` syncs `/workspace` → S3 first.)_ |

Sandbox template (lives in `agent-deploy` as a ConfigMap; consumed by boxy):

```json
{
  "ttlSeconds": 3600,
  "network": { "enabled": true, "allowInternetAccess": false },
  "allowedBinaries": ["bash", "python3", "node", "git"],
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

`sandboxId` = the incoming `X-Sandbox-Id`. TTL is **sliding** (refreshed per exec).

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
