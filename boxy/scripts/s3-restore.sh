#!/usr/bin/env bash
# boxy setupScript — restore the per-user /workspace from S3 on sandbox provision.
#
# SCAFFOLD: review before use. Runs on the boxy CONTROLLER (has network + S3 creds via
# IRSA/instance role), NOT inside the jail.
#
# Env provided by boxy:
#   $BOXY_SANDBOX_ID  — e.g. u-<sanitized-slack-user-id>
#   $BOXY_WORKSPACE   — path to the sandbox /workspace on the controller
#   $S3_BUCKET        — from the template's scriptEnv
#
# `|| true` so a brand-new user (no S3 prefix yet) does not fail provisioning.
set -euo pipefail

# TODO: confirm AWS CLI is on PATH in Dockerfile.controller.dev and IRSA grants s3:GetObject.
aws s3 sync "s3://${S3_BUCKET}/${BOXY_SANDBOX_ID}/" "${BOXY_WORKSPACE}/" || true
