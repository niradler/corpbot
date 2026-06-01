# deploy/ — corpbot umbrella Helm chart

One `helm install` brings up the whole corpbot platform:

- **nanobot** — the agent brain + Slack ingress, running the **corpbot plugin** (the boxy MCP
  client; injects per-user `X-Sandbox-Id`). Built-in tools (`exec`/`web`/`file`) are disabled,
  so the model can **only** act through boxy.
- **boxy** — the per-user nsjail sandbox runtime, consumed as a **subchart** from its published
  chart `oci://ghcr.io/niradler/charts/boxy` (pinned). Default sandbox is **off** (fail-closed);
  nanobot authenticates with a **projected ServiceAccount token** that boxy validates via
  TokenReview + SubjectAccessReview against the RBAC this chart grants.

```
deploy/
├── Chart.yaml                  # name: corpbot; dependency: boxy (oci, pinned, alias boxy)
├── values.yaml                 # all config, secure defaults, fully commented
├── values.schema.json          # validates key types / required fields
├── docker/Dockerfile.nanobot   # nanobot-ai + corpbot plugin image (multi-stage, non-root)
└── templates/
    ├── _helpers.tpl
    ├── serviceaccount.yaml          # nanobot SA (projected token)
    ├── rbac.yaml                    # Role+RoleBinding: boxy.dev sandboxes/sessions [get,list,create,update]
    ├── configmap-nanobot.yaml       # ~/.nanobot/config.json (tools off; slack socket mode; allowFrom)
    ├── configmap-sandbox-template.yaml  # per-user sandbox template (ephemeral /workspace; no S3 in v1)
    ├── secret.yaml                  # only when secrets.create=true (dev); else existingSecret
    ├── deployment-nanobot.yaml      # nanobot+plugin Deployment (hardened)
    ├── networkpolicy.yaml           # default-deny + egress to boxy/DNS/LLM/Slack
    └── NOTES.txt
```

## Prerequisites

- Kubernetes cluster + `kubectl`, Helm v3.8+ (OCI support; tested with v4).
- The **boxy chart must be published** at `oci://ghcr.io/niradler/charts/boxy` at the version
  pinned in `Chart.yaml` (currently `0.1.1`, which includes per-user routing, niradler/boxy #5).
  It **is** published, so `helm dependency build deploy/` works. If you ever need to install
  boxy separately, render corpbot alone with `--set boxy.enabled=false`.
- The **nanobot image** (`nanobot-ai` + corpbot plugin) built and pushed to a registry — see
  [Building the image](#building-the-nanobot-image). Until you push it, set
  `nanobot.image.repository`/`tag` to your image.

## One-command install

```bash
# 1) Pull the pinned boxy subchart into deploy/charts/
helm dependency build deploy/

# 2) Create the secret (NOT committed — see below), then install
helm install corpbot deploy/ -n corpbot --create-namespace \
  --set secrets.existingSecret=corpbot-secrets
```

boxy and nanobot come up together in the `corpbot` namespace. Watch:

```bash
kubectl rollout status deploy/corpbot-nanobot -n corpbot
kubectl logs -f deploy/corpbot-nanobot -n corpbot
```

## Creating the secret (never commit real secrets)

The chart reads Slack + LLM credentials from a Kubernetes Secret. In production, manage it
out-of-band (SOPS, sealed-secrets, External Secrets, or a cloud secret manager) and reference it
via `secrets.existingSecret`. A quick manual create:

```bash
kubectl create secret generic corpbot-secrets -n corpbot \
  --from-literal=SLACK_BOT_TOKEN=xoxb-... \
  --from-literal=SLACK_APP_TOKEN=xapp-... \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-...
```

Slack runs in **socket mode**, so it needs both a **bot token** (`xoxb-…`) and an **app-level
token** (`xapp-…`, with `connections:write`). No inbound ingress is required — nanobot dials out.

For **dev only**, you can have the chart render a placeholder Secret with `--set secrets.create=true`
(plus `--set secrets.values.SLACK_BOT_TOKEN=…` etc.). Do not use this in production, and never
put real values in `values.yaml`.

### boxy auth (no static secret by default)

nanobot authenticates to boxy-router with a **projected ServiceAccount token**
(`nanobot.boxyClient.auth: projected`, the default). The token is mounted at
`/var/run/secrets/boxy/token`, the plugin reads it via `BOXY_ROUTER_TOKEN_FILE` **fresh on every
call** (so kubelet rotation is picked up), and boxy validates it with TokenReview +
SubjectAccessReview against the `boxy.dev` RBAC this chart grants to nanobot's SA. No static boxy
token is stored anywhere.

Fallback: set `nanobot.boxyClient.auth: static` and `boxy.router.auth.staticToken` to share a
bearer via the secret key `BOXY_ROUTER_TOKEN`. This is weaker (a long-lived shared secret) — use
only for local dev / e2e.

## Security posture

- **Non-root, hardened pod**: `runAsNonRoot`, uid/gid/fsGroup `10001`, `readOnlyRootFilesystem`,
  `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`
  (pod- and container-level). Writable `emptyDir`s mount `~/.nanobot` (home/workspace) and `/tmp`.
- **NetworkPolicy**: default-deny; egress allowed only to DNS, the boxy-router port, and HTTPS
  (LLM API + Slack socket mode). No app ingress (Slack is outbound socket mode).
- **boxy default sandbox OFF**: a tool call with no `X-Sandbox-Id` returns a tool-error instead
  of routing to a shared sandbox (defense-in-depth alongside the plugin's fail-closed routing).
- **SA-token auth + least-privilege RBAC**: nanobot's SA gets only `boxy.dev`
  `sandboxes`/`sessions` `[get,list,create,update]` in boxy's namespace.
- **mTLS** between boxy router/operator and controller stays **on** (`boxy.mtls.disabled: false`).
- **Secrets** never live in `values.yaml`; they come from a Secret via `secretKeyRef`, and the
  boxy bearer is a rotating projected token, not a static value.

## Building the nanobot image

The image bundles stock `nanobot-ai` **and** the corpbot plugin. Build from the **repo root** so
the context includes `pyproject.toml` + `src/`:

```bash
docker build -f deploy/docker/Dockerfile.nanobot -t ghcr.io/niradler/corpbot-nanobot:0.1.0 .
docker push ghcr.io/niradler/corpbot-nanobot:0.1.0   # TODO: push to a registry you control
```

Then set `nanobot.image.repository` / `nanobot.image.tag` in `values.yaml` (or via `--set`).
The Dockerfile installs the plugin via `pip install .` of this repo; swap to
`pip install corpbot==<version>` once you pin a published release.

## Validation

```bash
helm lint deploy/
helm template corpbot deploy/ --set boxy.enabled=false -n corpbot   # corpbot-only render
helm template corpbot deploy/ -n corpbot                            # full umbrella (needs deps built)
```

## Notes / TODOs

- **Sandbox template ConfigMap** (`configmap-sandbox-template.yaml`) documents the intended
  per-user sandbox shape, but **boxy 0.1.1 does not expose a `sandboxTemplate.fromConfigMap`**
  value — the router builds the per-user sandbox from the inbound request and chart defaults. The
  ConfigMap is a no-op until boxy supports sourcing a template from a ConfigMap. _(TODO: wire it
  when boxy adds support.)_
- **Projected token audience** defaults to `https://kubernetes.default.svc` (the API server's
  default audience, which boxy's TokenReview accepts). Override `nanobot.boxyClient.tokenAudience`
  if your cluster uses a different identifier.
- **S3 `/workspace` persistence is deferred** — v1 sandboxes use an ephemeral per-sandbox
  `/workspace` (see `docs/architecture.md`).
