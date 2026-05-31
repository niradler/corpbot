#!/usr/bin/env bash
# boxy teardownScript — sync the per-user /workspace back to S3 on sandbox expiry/reap.
#
# SCAFFOLD: review before use. Runs on the boxy CONTROLLER (has network + S3 creds via
# IRSA/instance role), NOT inside the jail.
#
# Env provided by boxy:
#   $BOXY_SANDBOX_ID  — e.g. u-<sanitized-slack-user-id>
#   $BOXY_WORKSPACE   — path to the sandbox /workspace on the controller
#   $S3_BUCKET        — from the template's scriptEnv
set -euo pipefail

# TODO: confirm IRSA grants s3:PutObject. Consider --delete semantics carefully (a buggy
# wipe of /workspace would propagate to S3) before adding it.
aws s3 sync "${BOXY_WORKSPACE}/" "s3://${S3_BUCKET}/${BOXY_SANDBOX_ID}/"
