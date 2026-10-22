#!/usr/bin/env bash
# Provision one CheckPulse check worker on a fresh Ubuntu LTS VPS (root key SSH).
# Usage: deploy/provision_worker.sh <ssh alias or user@ip> <region> <worker_id> [api_url]
#   e.g. deploy/provision_worker.sh cp-worker-us-west us-west do-us-west-1
# Idempotent: safe to re-run. The region should be scheduled=true first, or the worker logs
# 422 until it is (see app/services/regions.py). The worker secret is read from the prod api
# container and piped through stdin; it is never printed or passed as an argument.
set -euo pipefail
HOST="${1:?ssh alias}" REGION="${2:?region}" WID="${3:?worker id}" API="${4:-https://api.checkpulse.dev}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SSHCFG="${CP_WORKER_SSH_CONFIG:?set CP_WORKER_SSH_CONFIG to an ssh_config with the worker alias}"
# Repo is public: the prod host and key come from the environment, never from this file.
PROD="${CP_PROD_SSH:?set CP_PROD_SSH=root@<prod host>}"
PK="-i ${CP_PROD_KEY:?set CP_PROD_KEY=<path to ssh key>} -o BatchMode=yes -o ConnectTimeout=15 -o LogLevel=ERROR"
HN="cp-worker-$REGION"

SECRET=$(ssh $PK "$PROD" 'docker exec uptimebot-api-1 printenv WORKER_SECRET')
[ "${#SECRET}" -ge 16 ] || { echo "FAIL: could not read WORKER_SECRET from prod"; exit 1; }

scp -q -F "$SSHCFG" "$HERE/cp_provision_remote.sh" "$HOST:/root/cp_provision_remote.sh"
scp -q -F "$SSHCFG" "$HERE/worker.docker-compose.yml" "$HOST:/root/cp_compose.yml"
rc=0
printf '%s\n' "$SECRET" | ssh -F "$SSHCFG" "$HOST" \
  "bash /root/cp_provision_remote.sh '$REGION' '$WID' '$HN' '$API'; rc=\$?; rm -f /root/cp_provision_remote.sh /root/cp_compose.yml; exit \$rc" || rc=$?
unset SECRET

echo "## outside check: password login must be refused"
out=$(ssh -F "$SSHCFG" -o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive \
  -o BatchMode=yes "$HOST" true 2>&1 || true)
echo "$out" | grep -oE 'Permission denied \([^)]*\)' || echo "UNEXPECTED: $out"
exit $rc
