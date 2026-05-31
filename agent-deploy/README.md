# agent-deploy/ (corpbot)

The **thin deploy repo** that wires nanobot + boxy together for one company. Helm values for
boxy, the nanobot `config.json`, secrets, the sandbox template, and k8s manifests.

> This repo references boxy as a **deployed service** (its published Helm chart + the
> in-cluster MCP endpoint). It does **not** vendor or submodule boxy/nanobot source.

> **Status: scaffold.** Files here are placeholders/examples (`*.example.*`). Fill in real
> values, render to live manifests, and keep secrets out of git.

## Contents

```
agent-deploy/
├── helm/
│   └── boxy-values.example.yaml         # values for the pinned boxy chart
├── nanobot/
│   └── config.example.json              # ~/.nanobot/config.json (env-templated)
└── k8s/
    ├── namespace.yaml                   # boxy + nanobot namespaces
    ├── sandbox-template.configmap.yaml  # the per-user sandbox template (B2)
    └── secrets.example.yaml             # secret keys (DO NOT commit real values)
```

## Deploy order

> **boxy first, then nanobot.** nanobot's first tool call expects boxy's `/mcp` to be live.

```bash
# 0) namespaces + sandbox template
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/sandbox-template.configmap.yaml

# 1) secrets (use your real secret manager / SOPS / sealed-secrets — example only)
#    keys: BOXY_ROUTER_TOKEN, SLACK_BOT_TOKEN, <LLM_PROVIDER_KEY>, S3 creds (or IRSA)
kubectl apply -f k8s/secrets.example.yaml   # after filling in real values out-of-band

# 2) boxy — PINNED chart version
helm install boxy oci://ghcr.io/niradler/charts/boxy \
  --version <PINNED_CHART_VERSION> \
  --namespace boxy --create-namespace \
  -f helm/boxy-values.example.yaml

# 3) nanobot — mount nanobot/config.example.json (env-substituted) at ~/.nanobot/config.json
#    deploy the nanobot image/manifest (TODO: add nanobot Deployment manifest here)
```

## Secrets (provide out-of-band)

| Secret | Used by | Notes |
|--------|---------|-------|
| `BOXY_ROUTER_TOKEN` | nanobot → boxy `Authorization: Bearer` | Must match boxy-router's configured token. |
| `SLACK_BOT_TOKEN` | nanobot Slack channel | Slack app bot token. |
| `<LLM_PROVIDER_KEY>` | nanobot LLM calls | e.g. `ANTHROPIC_API_KEY`. |
| S3 access | boxy controller | **Prefer IRSA / instance role** over static keys; bucket = `S3_BUCKET`. |

## Pinning

- **boxy chart + image**: pin `--version` and image tags in `helm/boxy-values.example.yaml`.
- **nanobot**: pin the fork image tag; keep the fork's `upstream` remote for HKUDS updates
  (see [`../nanobot/README.md`](../nanobot/README.md)).

## Security guardrail: disable boxy's default sandbox

boxy does **not** validate the MCP `X-Sandbox-Id` header, and if a tool call arrives without
an id while a **default sandbox is enabled**, boxy silently routes to that **shared** sandbox
(cross-user risk). nanobot already refuses to call boxy without a trusted id (fails closed),
but set boxy's default sandbox **off** too — then a missing id returns a tool-error instead of
touching a shared sandbox. See `helm/boxy-values.example.yaml` (`defaultSandbox.enabled: false`).

## Checklist

- [ ] Namespaces + sandbox-template ConfigMap applied
- [ ] Secrets created via real secret manager (not committed)
- [ ] **boxy default sandbox disabled** (`defaultSandbox.enabled: false`) — fail-closed guardrail
- [ ] boxy installed from pinned chart version with values
- [ ] nanobot deployed with templated `config.json` pointing at boxy `/mcp`
- [ ] Smoke test: Slack message → tool call → sandbox provisioned → S3 restore/sync verified
