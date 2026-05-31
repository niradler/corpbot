# boxy/ (corpbot fork/branch)

The **per-user sandbox runtime** for corpbot. A fork/branch of
[niradler/boxy](https://github.com/niradler/boxy) (Go, Kubernetes, nsjail). boxy is a
**deployed service** that nanobot calls over HTTP/MCP — never a library imported into nanobot.

> **Status: scaffold.** This directory documents the boxy extensions and contains the S3
> hook script stubs. The actual fork source is not committed here; deploy via the Helm chart
> (see [`../agent-deploy`](../agent-deploy)).

## Ships as

Helm chart `oci://ghcr.io/niradler/charts/boxy`, **pinned by version**. Components:
`boxy-router` (auth + MCP/REST frontend) · `boxy-operator` (k8s controller) ·
`boxy-controller` (per-node nsjail daemon).

## Build tasks

### B1 — File tools on the `/mcp` server

Extend boxy's `/mcp` server (built with the official Go MCP SDK) with file tools alongside
the existing `bash` tool:

| Tool | Signature |
|------|-----------|
| `read_file`  | `read_file(path)` |
| `write_file` | `write_file(path, content)` |
| `edit_file`  | `edit_file(path, old, new)` |
| `list_dir`   | `list_dir(path)` |
| `make_dir`   | `make_dir(path)` |

- Implement as **native controller file ops** on the per-sandbox `/workspace`.
  Preferred over exec wrappers: **no binary deps, no 6 MB output cap.**
- **Confine every path to `/workspace`.** Reject `..`, absolute escapes, and symlinks that
  point out of the workspace.

> **Findings from the boxy MCP handler (confirmed):**
>
> - `X-Sandbox-Id` is consumed **only by tool closures** (`bash`/`read_file`/`write_file`/
>   `edit_file`). `initialize` and `tools/list` ignore it — so MCP discovery works with just
>   the Bearer token and provisions nothing. The tool list is **global/static**.
> - The MCP handler does **not** run `X-Sandbox-Id` through `ValidateSandboxID` (unlike the
>   REST API). The raw header flows into the k8s lookup. Contract to honor on the client:
>   `^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$`, and the id must be **≤55** chars (boxy derives
>   `<id>-session`, a ≤63-char label). corpbot's nanobot side sanitizes to this.
> - **Fail-open risk:** a `tools/call` with no id, when the **default sandbox is enabled**,
>   routes to a shared default sandbox. **Deploy with the default sandbox disabled** so a
>   missing id returns a tool-error instead. See `agent-deploy`.

### B2 — Lazy provisioning

When `/mcp` receives an `X-Sandbox-Id` for a sandbox that **doesn't exist**, auto-create it
from a template (env / ConfigMap), then proceed. `sandboxId` = the incoming `X-Sandbox-Id`.
TTL is **sliding** (refreshed per exec).

Template (authored in `agent-deploy` as a ConfigMap):

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

### B3 — Controller image (`Dockerfile.controller.dev`)

- **Pre-bake the AWS CLI** and **every `allowedBinaries` entry** (`bash`, `python3`, `node`,
  `git`) into the controller image.
- Attach **S3 read/write** to the controller's **service account** (IRSA / instance role).
- Setup/teardown scripts run **on the controller** (which has network), **not in the jail**
  (which stays internet-isolated).

### C — S3 hook scripts

Live in [`scripts/`](./scripts) and are baked into the controller image at `/opt/boxy/scripts/`.
boxy passes `$BOXY_SANDBOX_ID`, `$BOXY_WORKSPACE`, and `scriptEnv` (e.g. `$S3_BUCKET`).

- [`scripts/s3-restore.sh`](./scripts/s3-restore.sh) — `setupScript`, restores `/workspace`
  from S3 (tolerates a brand-new user).
- [`scripts/s3-sync.sh`](./scripts/s3-sync.sh) — `teardownScript`, syncs `/workspace` to S3.

## Checklist

- [ ] B1 — `read_file` / `write_file` / `edit_file` / `list_dir` / `make_dir` on `/mcp`, path-confined to `/workspace`
- [~] B2 — **partial, shipped as [`patches/b2-router-session-routing.diff`](./patches/b2-router-session-routing.diff)**: an explicit `X-Sandbox-Id` now resolves/creates that sandbox's session **without** requiring the global default sandbox (decoupled from `DefaultSandboxEnabled`), so per-user routing works with the default sandbox **off**. Verified live on kind. Remaining: auto-create the *sandbox* itself from a template on first unknown id (currently the sandbox is pre-created via REST)
- [ ] B3 — controller image pre-bakes AWS CLI + allowed binaries; S3 perms via IRSA
- [ ] C — S3 restore/sync scripts baked at `/opt/boxy/scripts/`
- [ ] Pin the fork; publish/pin the Helm chart + image versions consumed by agent-deploy
