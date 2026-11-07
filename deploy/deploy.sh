#!/usr/bin/env bash
# Run against the Vultr VM since 2026-11-07, re-run on every redeploy. Read every failure; do not assume a step worked.
#
# From the laptop, repo root, after committing:   deploy/deploy.sh root@<vm-ip>
# Target: a fresh Vultr VM on Ubuntu 24.04. Re-running it is safe.
#
# Laptop part: build web/ with the live flag, copy the committed tree, dist and .env to /opt/drydock.
# VM part (--on-vm): firewall, Docker, pinned postgres, seed template, start, wait for health.
set -euo pipefail
APP=/opt/drydock

if [[ "${1:-}" != "--on-vm" ]]; then
  target="${1:?usage: deploy/deploy.sh root@<vm-ip>}"
  ip="${target#*@}"
  [[ -f .env ]] || echo "WARNING: no .env here. The site will come up, and every run will show Model unavailable." >&2
  (cd web && { [[ -d node_modules ]] || npm ci; } && VITE_DRYDOCK_LIVE=1 npx vite build)
  ssh -o StrictHostKeyChecking=accept-new "$target" "mkdir -p $APP && rm -rf $APP/web/dist"
  git archive HEAD | ssh "$target" "tar -x -C $APP"
  tar -C web -cf - dist | ssh "$target" "tar -x -C $APP/web"
  if [[ -f .env ]]; then
    tr -d '\r' < .env | ssh "$target" "umask 077 && cat > $APP/.env"
  else
    ssh "$target" "umask 077 && touch $APP/.env"
  fi
  ssh "$target" "cd $APP && bash deploy/deploy.sh --on-vm $ip"
  deploy/verify.sh "$target"
  exit 0
fi

ip="${2:?--on-vm needs the public ip}"
host="${ip//./-}.sslip.io"
cd "$APP/deploy"

# ---- packages (a fresh VM may still hold the apt lock) ----
export DEBIAN_FRONTEND=noninteractive
apt_opts=(-o DPkg::Lock::Timeout=300)
apt-get "${apt_opts[@]}" update -q
apt-get "${apt_opts[@]}" install -y -q docker.io docker-compose-v2 docker-buildx curl ufw
systemctl enable --now docker

# ---- host firewall: 22 for ssh, 80/443 for Caddy, nothing else. Port 8000 is on loopback anyway ----
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

# ---- DOCKER-USER (sandbox/harden.md): a container that ever gets a network still cannot reach metadata or this host ----
# Sandbox containers run --network none, so this is the second layer. It filters bridge traffic only: orch and caddy use
# the host network and are not covered, and neither runs model output as code.
cat > /usr/local/sbin/drydock-firewall.sh <<EOF
#!/bin/sh
for dst in 169.254.0.0/16 $ip; do
  iptables -C DOCKER-USER -d "\$dst" -j DROP 2>/dev/null || iptables -I DOCKER-USER -d "\$dst" -j DROP
done
EOF
chmod 755 /usr/local/sbin/drydock-firewall.sh
cat > /etc/systemd/system/drydock-firewall.service <<EOF
[Unit]
Description=DryDock DOCKER-USER rules
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/drydock-firewall.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable drydock-firewall
systemctl restart drydock-firewall

# ---- compose settings. The VM id switches on the "Vultr Cloud Compute VM" ledger row; if unknown, nothing claims a VM ----
# ponytail: metadata path from memory, unverified. Empty just leaves the row off; check it in the verify output.
vm_id="$(curl -fsS -m 3 http://169.254.169.254/v1/instanceid || true)"
[[ -n "$vm_id" ]] || echo "WARNING: no VM id from metadata. The ledger will not name the VM until DRYDOCK_VM_ID is set in deploy/.env." >&2
umask 077
cat > .env <<EOF
DRYDOCK_HOST=$host
DOCKER_GID=$(stat -c %g /var/run/docker.sock)
DRYDOCK_VM_ID=$vm_id
EOF
umask 022

# ---- images and seed template. The template must exist before orch starts: a missing one makes every container fail ----
image="$(grep -oP '^IMAGE = "\K[^"]+' "$APP/sandbox/container.py")"
docker pull "$image"
docker compose build orch
docker compose stop orch caddy 2>/dev/null || true
docker compose run --rm --no-deps orch python -c "from seed.size_event_log import seed_template; seed_template(None)"

# ---- start. restart: unless-stopped brings both back after a crash or reboot ----
docker compose up -d orch caddy || true  # an unhealthy orch is reported below, with its log
for _ in $(seq 1 30); do
  curl -fsS -m 3 http://127.0.0.1:8000/api/health >/dev/null && { echo "orch healthy"; docker compose ps; echo "https://$host"; exit 0; }
  sleep 3
done
echo "orch never became healthy. Last log lines:" >&2
docker compose logs --tail 40 orch >&2
exit 1
