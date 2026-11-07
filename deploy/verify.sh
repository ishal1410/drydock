#!/usr/bin/env bash
# Run after every deploy since 2026-11-07. A PASS line counts only for the run you saw.
#
# From the laptop, so the checks come from outside:   deploy/verify.sh root@<vm-ip>
# Spends no model tokens. A real run is a separate step: open the site and start one.
set -uo pipefail
target="${1:?usage: deploy/verify.sh root@<vm-ip>}"
ip="${target#*@}"
url="https://${ip//./-}.sslip.io"
failed=0

check() {  # name, then a command that must succeed
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "PASS  $name"; else echo "FAIL  $name"; failed=1; fi
}
status_is() {  # code, then curl arguments
  local want="$1"; shift
  [[ "$(curl -s -m 20 -o /dev/null -w '%{http_code}' "$@")" == "$want" ]]
}
body_has() {  # text, then url
  curl -fsS -m 30 "$2" | grep -q "$1"
}

# The reaper warms the seed box and live-db just after start, and the first certificate can take a moment.
for _ in $(seq 1 20); do body_has '"live_checksum":"' "$url/api/tickets" 2>/dev/null && break; sleep 3; done

check "valid TLS, health"            body_has '"ok":true'    "$url/api/health"
check "orch reaches Docker"         body_has '"docker":true' "$url/api/health"
check "tickets listed"               body_has '"tickets":\[{' "$url/api/tickets"
check "live checksum measured"       body_has '"live_checksum":"' "$url/api/tickets"
check "seed checksum measured"       body_has '"seed_checksum":"' "$url/api/tickets"
check "landing page served"          body_has 'id="root"'    "$url/"
check "live demo page served"        body_has 'id="root"'    "$url/?app"
check "unknown /api is JSON 404"     status_is 404 "$url/api/nope"
check "reset route gone"             status_is 405 -X POST "$url/api/demo/reset"
check "20 KB POST refused"           status_is 413 -X POST -H 'Content-Type: application/json' --data-binary "$(head -c 20000 /dev/zero | tr '\0' x)" "$url/api/runs"
check "port 8000 closed from outside" bash -c "! curl -s -m 5 http://$ip:8000/api/health"
check "one sandbox self-test (4 chips)" ssh "$target" "cd /opt/drydock/deploy && docker compose exec -T orch python -m sandbox.selftest"
check "metadata rule in DOCKER-USER" ssh "$target" "iptables -C DOCKER-USER -d 169.254.0.0/16 -j DROP"

[[ $failed == 0 ]] && echo "All checks passed." || { echo "Some checks FAILED."; exit 1; }
