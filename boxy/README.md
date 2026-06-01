# boxy/ (pinned upstream dependency)

The **per-user sandbox runtime** for corpbot. boxy
([niradler/boxy](https://github.com/niradler/boxy) — Go, Kubernetes, nsjail) is consumed as a
**pinned published dependency**, NOT forked or built from source by corpbot: corpbot installs
the published Helm chart `oci://ghcr.io/niradler/charts/boxy` and the GHCR images
(`ghcr.io/niradler/boxy-{router,operator,controller}`), pinned by version. boxy is a
**deployed service** that nanobot calls over HTTP/MCP — never a library imported into nanobot.

> **Status: scaffold.** This directory documents how corpbot consumes boxy and holds
> reference material (the B2 stopgap diff, the deferred S3 hook scripts). corpbot does not
> vendor or build boxy source; deploy via the published Helm chart
> (see [`../agent-deploy`](../agent-deploy)).

## Ships as

Published Helm chart `oci://ghcr.io/niradler/charts/boxy`, **pinned by version** (built and
released upstream via boxy's own `local/release.sh`). Components:
`boxy-router` (auth + MCP/REST frontend) · `boxy-operator` (k8s controller) ·
`boxy-controller` (per-node nsjail daemon).

## Build tasks

Everything corpbot needs from boxy is **already upstream** or **upstreamed via a PR**; corpbot's
job is to **pin a boxy release**, not to maintain a fork.

### B1 — File tools on the `/mcp` server — already upstream (nothing to build)

boxy's `/mcp` server (built with the official Go MCP SDK) **already ships** the file tools
alongside the existing `bash` tool, so there is **nothing for corpbot to build** here — just
pin a boxy release that includes them:

| Tool | Signature |
|------|-----------|
| `read_file`  | `read_file(path)` |
| `write_file` | `write_file(path, content)` |
| `edit_file`  | `edit_file(path, old, new)` |
| `list_dir`   | `list_dir(path)` |
| `make_dir`   | `make_dir(path)` |

- Implemented upstream as **native controller file ops** on the per-sandbox `/workspace`.
  Preferred over exec wrappers: **no binary deps, no 6 MB output cap.**
- Paths are **confined to `/workspace`** (rejects `..`, absolute escapes, and symlinks that
  point out of the workspace).

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

### B2 — Per-user session routing — upstreamed via [niradler/boxy#5](https://github.com/niradler/boxy/pull/5)

The fix that lets an explicit `X-Sandbox-Id` resolve (and lazily create) **that sandbox's own
session** without requiring the global default sandbox (decoupled from `DefaultSandboxEnabled`),
so per-user routing works with the default sandbox **off**, is **upstreamed as
[niradler/boxy#5](https://github.com/niradler/boxy/pull/5)**. corpbot's job: **pin a boxy
release that includes PR #5.**

`sandboxId` = the incoming `X-Sandbox-Id`; TTL is **sliding** (refreshed per exec). The
per-user template is authored in `agent-deploy` as a ConfigMap.

> **Stopgap reference only:** [`patches/b2-router-session-routing.diff`](./patches/b2-router-session-routing.diff)
> is the vendored copy of the PR #5 change, kept **only as a reference until a boxy release
> that includes it exists**. Once such a release is published, pin it and drop the stopgap —
> corpbot does not maintain a boxy fork.

### B3 — S3 `/workspace` persistence — **Deferred (later milestone)**

> **Deferred — not in v1.** v1 runs on boxy's ephemeral, per-sandbox `/workspace`; S3
> backup/restore is a **later milestone**.

When implemented, this would be a **thin derived controller image** —
`FROM ghcr.io/niradler/boxy-controller:<ver>` plus the AWS CLI and the setup/teardown scripts —
**NOT a fork of boxy source**:

- Pre-bake the AWS CLI (every `allowedBinaries` entry — `bash`, `python3`, `node`, `git` — is
  already in the upstream controller image).
- Attach **S3 read/write** to the controller's **service account** (IRSA / instance role).
- Setup/teardown scripts run **on the controller** (which has network), **not in the jail**
  (which stays internet-isolated).

### C — S3 hook scripts — **Deferred (reference for B3)**

Reference scripts for the deferred B3 milestone, kept in [`scripts/`](./scripts). When B3
ships they would be baked into the derived controller image at `/opt/boxy/scripts/`, with boxy
passing `$BOXY_SANDBOX_ID`, `$BOXY_WORKSPACE`, and `scriptEnv` (e.g. `$S3_BUCKET`).

- [`scripts/s3-restore.sh`](./scripts/s3-restore.sh) — `setupScript`, restores `/workspace`
  from S3 (tolerates a brand-new user). **Deferred — not wired up in v1.**
- [`scripts/s3-sync.sh`](./scripts/s3-sync.sh) — `teardownScript`, syncs `/workspace` to S3.
  **Deferred — not wired up in v1.**

## Checklist

- [x] B1 — file tools (`read_file` / `write_file` / `edit_file` / `list_dir` / `make_dir`) ship **upstream** in boxy, path-confined to `/workspace` — nothing to build
- [x] B2 — per-user session routing **upstreamed as [niradler/boxy#5](https://github.com/niradler/boxy/pull/5)** (explicit `X-Sandbox-Id` resolves/creates its own session with the default sandbox **off**). Verified live on kind. `patches/b2-router-session-routing.diff` is a stopgap reference until a release with PR #5 is pinned
- [ ] **Pin a boxy release** (chart `oci://ghcr.io/niradler/charts/boxy` + GHCR image tags) that includes PR #5, consumed by agent-deploy
- [ ] _Deferred (later milestone):_ B3 — S3 `/workspace` persistence via a **derived** controller image (AWS CLI + hook scripts, IRSA); not a fork
