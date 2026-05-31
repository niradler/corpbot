# corpbot

A self-hosted, **single-tenant** AI agent for one company. Many employees talk to it
in Slack; **each user gets their own isolated execution sandbox**. The model has no local
tools — every action runs inside a per-user [nsjail](https://github.com/google/nsjail)
sandbox on Kubernetes, with a sliding 1-hour TTL and S3-backed `/workspace` persistence.

> **Status — verified live.** The per-user routing has been proven end-to-end against **real
> boxy on a local kind cluster**: two Slack users each ran `bash` in their **own** nsjail
> sandbox with isolated, persistent `/workspace` (no cross-user leak), plus fail-closed when no
> trusted id is present. The nanobot side ships as patches + tests + demos (this repo does not
> vendor the HKUDS source); the one required boxy change ships as a diff.
>
> ```text
> [alice]  root | NSJAIL | alice-secret      # ran in alice's sandbox, wrote+read her marker
> [bob]    NSJAIL | NO_MARKER | bob-secret   # bob never saw alice's /workspace
> [alice2] NSJAIL | alice-secret             # alice's data persisted, not overwritten by bob
> RESULT: PASS — real boxy on kind, per-user sandbox isolation proven
> ```

## Repository layout

| Path | What |
|------|------|
| [`docs/architecture.md`](./docs/architecture.md) | Flow, sandbox lifecycle, identity/security model, limits |
| [`nanobot/`](./nanobot) | Fork guidance + **`overlay/`** (new files), **`patches/upstream.diff`** (edits), **`tests/`**, **`scripts/`** (demo + live driver) |
| [`boxy/`](./boxy) | Extension plan + **`patches/b2-router-session-routing.diff`** + S3 hook scripts |
| [`agent-deploy/`](./agent-deploy) | Helm values, nanobot config, k8s manifests, secrets template |

See [`nanobot/README.md`](./nanobot/README.md) to apply the patches to your HKUDS fork and run
the test/demo suite (mock transport, no cluster needed) or the live driver (against boxy).

## One-liner

```text
Slack message
  → one nanobot process (asserts the Slack user id)
    → boxy MCP  (X-Sandbox-Id = that user)
      → per-user nsjail sandbox on k8s
         (sliding 1h TTL, S3-backed /workspace persistence)
```

## Architecture

```text
                ┌─────────────────────────────────────────────────────────┐
                │                      Kubernetes                           │
                │                                                           │
  Slack  ─────► │  ┌────────────────┐        ┌──────────────────────────┐  │
  (events)      │  │   nanobot      │  MCP    │   boxy-router            │  │
                │  │  (agent brain  │ ──────► │  (auth + X-Sandbox-Id    │  │
                │  │   + Slack      │  HTTP   │   routing)               │  │
                │  │   ingress)     │         └────────────┬─────────────┘  │
                │  └───────┬────────┘                      │                │
                │          │ LLM API (egress)              ▼                │
                │          │                  ┌──────────────────────────┐  │
                │          ▼                  │  boxy-operator/controller │  │
                │     LLM provider            │  per-user nsjail sandbox  │  │
                │     (Anthropic/...)         │  /workspace (no internet) │  │
                │                             └────────────┬─────────────┘  │
                │                                          │ setup/teardown  │
                └──────────────────────────────────────────┼────────────────┘
                                                            ▼
                                                   S3: s3://<bucket>/u-<user>/
```

The model acts **only** through boxy. nanobot's own shell/web/file tools are disabled.
The LLM API calls leave from the nanobot process (which has egress); the sandbox jail
itself has **no internet**.

## Components (three repos)

| Dir | What it is | Language | How it ships |
|-----|-----------|----------|--------------|
| [`nanobot/`](./nanobot) | Fork of [HKUDS/nanobot](https://github.com/HKUDS/nanobot) — the agent brain + Slack ingress | Python | Container image, config at `~/.nanobot/config.json` |
| [`boxy/`](./boxy) | Fork/branch of [niradler/boxy](https://github.com/niradler/boxy) — per-user sandbox runtime | Go (k8s) | Helm chart `oci://ghcr.io/niradler/charts/boxy`, pinned by version |
| [`agent-deploy/`](./agent-deploy) | Thin deploy repo that wires the two together | YAML / Helm | Helm + k8s manifests |

> **These are three separate repos**, not one. They live here as sibling directories for
> convenience during bootstrap. `agent-deploy/` references boxy as a **deployed service**
> (Helm chart + HTTP/MCP endpoint) — **never** as a git submodule of boxy source, and boxy
> is **never** imported as a library into nanobot.

## Deploy order (Helm)

1. **boxy first** — `helm install` the pinned boxy chart (`oci://ghcr.io/niradler/charts/boxy`)
   with `agent-deploy/helm/boxy-values.example.yaml`, plus the sandbox-template ConfigMap.
2. **nanobot second** — deploy nanobot with its templated `config.json` pointing at
   `http://boxy-router.boxy.svc.cluster.local:8080/mcp`.

Full deploy steps live in [`agent-deploy/README.md`](./agent-deploy/README.md).

## Guardrails (read before changing anything)

- **Single tenant, one company, mutually trusted users** → one boxy cluster, per-user
  sandboxes. Do **not** build multi-tenant node pools.
- **Routing key = Slack user id, derived from channel context** — NEVER from the model or
  tool arguments (confused-deputy risk).
- **boxy is the only execution surface** — do not enable nanobot's built-in shell/web/file
  tools.
- **Sandbox has no internet** (`allowInternetAccess: false`). LLM API calls come from the
  nanobot process, outside the jail.
- **boxy exec output caps at 6 MB** (`BOXY_MAX_OUTPUT_BYTES`) and truncates with a `200 OK` —
  use the stream endpoint or file tools for large output.
- **Pin boxy** by chart/image version; keep the nanobot fork's upstream remote to HKUDS for
  updates.

## Out of scope (v1)

- **No MCP gateway (Obot).** Only needed later for onboarding third-party MCP servers that
  require per-user OAuth. Not in v1.
- **No multi-tenancy.** This is for one company with trusted employees.

## Docs

- [`docs/architecture.md`](./docs/architecture.md) — full flow, sandbox lifecycle, identity
  and security model, persistence, limits.
