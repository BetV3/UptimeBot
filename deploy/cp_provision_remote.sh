#!/usr/bin/env bash
# Runs ON a fresh worker VPS as root (copied there by provision_worker.sh, deleted after).
# Usage: bash cp_provision_remote.sh <region> <worker_id> <hostname> <api_url>
# Line 1 of stdin = WORKER_SECRET (never echoed, written only into worker.env 0600).
set -euo pipefail
REGION="$1" WID="$2" HN="$3" API="$4"
IFS= read -r SECRET
[ "${#SECRET}" -ge 16 ] || { echo "FAIL: no worker secret on stdin"; exit 2; }
export DEBIAN_FRONTEND=noninteractive
APT="apt-get -o DPkg::Lock::Timeout=600 -q"

echo "## 1 hostname"
hostnamectl set-hostname "$HN"
if grep -q '^127\.0\.1\.1' /etc/hosts; then sed -i "s/^127\.0\.1\.1.*/127.0.1.1 $HN/" /etc/hosts
else echo "127.0.1.1 $HN" >> /etc/hosts; fi
mkdir -p /etc/cloud/cloud.cfg.d && echo 'preserve_hostname: true' > /etc/cloud/cloud.cfg.d/99-checkpulse-hostname.cfg
echo "hostname=$(hostname)"

echo "## 2 swap"
SW=$(free -m | awk '/Swap:/{print $2}')
if [ "$SW" -lt 400 ] && [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
echo "swap=$(free -m | awk '/Swap:/{print $2}') MB"

echo "## 3 sshd key-only (00- sorts before cloud-init's 50-; sshd keeps the first value)"
cat > /etc/ssh/sshd_config.d/00-checkpulse.conf <<'EOF'
# CheckPulse worker: key-only root login (provision_worker.sh)
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
mkdir -p /run/sshd
sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
echo "sshd: $(sshd -T | grep -iE '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) ' | tr '\n' ' ')"

echo "## 4 firewall: inbound 22 only"
command -v ufw >/dev/null || $APT install -y ufw >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
ufw --force enable >/dev/null
echo "ufw: $(ufw status | head -1) rules: $(ufw status | grep -c ALLOW)"

echo "## 5 security updates on, no auto reboot"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
echo 'Unattended-Upgrade::Automatic-Reboot "false";' > /etc/apt/apt.conf.d/52checkpulse-no-reboot
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
echo "unattended-upgrades: $(systemctl is-active unattended-upgrades)"

echo "## 6 docker (Ubuntu archive packages)"
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  $APT update >/dev/null
  $APT install -y docker.io docker-compose-v2 >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1
echo "$(docker --version) / $(docker compose version --short)"

echo "## 7 worker files"
install -d -m 755 /opt/checkpulse
install -m 644 /root/cp_compose.yml /opt/checkpulse/docker-compose.yml
cat > /opt/checkpulse/docker-compose.override.yml <<EOF
# heartbeat hostname = this box, not a container id (provision_worker.sh)
services:
  worker:
    hostname: $HN
EOF
( umask 077
  printf 'REGION=%s\nAPI_URL=%s\nWORKER_SECRET=%s\nWORKER_ID=%s\nPOLL_INTERVAL=10\n' \
    "$REGION" "$API" "$SECRET" "$WID" > /opt/checkpulse/worker.env )
chown root:root /opt/checkpulse/worker.env && chmod 600 /opt/checkpulse/worker.env
echo "worker.env: $(stat -c '%a %U' /opt/checkpulse/worker.env), keys: $(cut -d= -f1 /opt/checkpulse/worker.env | tr '\n' ' ')"

echo "## 8 preflight: API reachable with the secret"
HF=$(mktemp); chmod 600 "$HF"; printf 'X-Worker-Secret: %s\n' "$SECRET" > "$HF"
echo "GET /internal/workers: $(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -H @"$HF" "$API/internal/workers")"
echo "GET /internal/jobs?region=$REGION&limit=0: $(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -H @"$HF" "$API/internal/jobs?region=$REGION&limit=0")"
rm -f "$HF"; unset SECRET

echo "## 9 start worker"
cd /opt/checkpulse
docker compose pull -q
docker compose up -d 2>&1 | tail -2
sleep 30
docker ps --filter name=checkpulse-worker --format '{{.Names}} {{.Image}} {{.Status}}'
echo "--- worker log"
docker logs --tail 12 checkpulse-worker 2>&1
