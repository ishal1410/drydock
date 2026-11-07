"""self_test(box): the 4 containment chips, from real signals, every run."""
import json
import subprocess
import sys
from dataclasses import dataclass

from sandbox.container import TEMPLATE_VOLUME, RunBox, destroy, docker, start_run_container
from sandbox.runner import psql, run_sql
from sandbox.template import STATEMENT_TIMEOUT, TEMP_FILE_LIMIT


@dataclass
class Chip:
    name: str
    ok: bool
    detail: str


def _escape(box):
    r = run_sql(box, "COPY (SELECT 1) TO PROGRAM 'true'")
    return Chip("server-side COPY denied", r.denied, f"executor: {r.code} {(r.error or 'NOT DENIED').splitlines()[0]}")


def _secret_env(env: list) -> list:
    names = [e.split("=")[0] for e in env]
    return [n for n in names if any(w in n.upper() for w in ("PASS", "SECRET", "KEY", "TOKEN"))]


def _secrets(box, info):
    p = subprocess.run(["docker", "exec", box.id, "psql",
                        "host=169.254.169.254 port=80 connect_timeout=3 user=x dbname=x"],
                       capture_output=True, text=True, encoding="utf-8", timeout=30)
    unreachable = p.returncode != 0 and "unreachable" in p.stderr
    env_secrets = _secret_env(info["Config"]["Env"])
    pw = psql(box.id, box.sock_admin, "SELECT count(*) FROM pg_authid WHERE rolpassword IS NOT NULL").stdout.strip()
    ok = unreachable and not env_secrets and pw == "0"
    return Chip("metadata unreachable · no credentials", ok,
                f"169.254.169.254: {'unreachable' if unreachable else 'REACHABLE'}; "
                f"secret env vars: {len(env_secrets)}; role passwords: {pw}")


def _caps(box, info):
    hc = info["HostConfig"]
    shown = run_sql(box, "SHOW statement_timeout; SHOW temp_file_limit").stdout.split()
    # canary: the timeout mechanism really cancels a statement in this box (100 ms, not T, to stay fast)
    canary = run_sql(box, "SET statement_timeout = '100ms'; SELECT pg_sleep(2)")
    ok = (hc["Memory"] > 0 and hc["NanoCpus"] > 0 and hc["PidsLimit"] > 0 and hc["NetworkMode"] == "none"
          and shown == [STATEMENT_TIMEOUT, TEMP_FILE_LIMIT] and canary.code == "57014")
    return Chip("caps + timeout canary", ok,
                f"memory {hc['Memory'] // 2**20} MiB, cpus {hc['NanoCpus'] / 1e9:g}, pids {hc['PidsLimit']}, "
                f"network {hc['NetworkMode']}; statement_timeout {shown[0] if shown else '?'}, "
                f"temp_file_limit {shown[1] if len(shown) > 1 else '?'}; canary {canary.code or 'NOT CANCELLED'}")


def _lifecycle(box, info):
    vols = [m["Name"] for m in info["Mounts"] if m["Type"] == "volume"]
    ok = vols == [box.volume] and TEMPLATE_VOLUME not in vols and info["State"]["Running"]
    return Chip("fresh per-run copy, destroyed after run", ok,
                f"container {box.id}, own volume {vols}, created {box.created_at.isoformat(timespec='seconds')}")


def self_test(box: RunBox) -> list[Chip]:
    info = json.loads(docker("inspect", box.id).stdout)[0]
    return [_escape(box), _secrets(box, info), _caps(box, info), _lifecycle(box, info)]


def main() -> int:
    """`python -m sandbox.selftest`: one fresh box, the 4 chips, destroyed. deploy/verify.sh runs it on the VM."""
    box = start_run_container()
    try:
        chips = self_test(box)
    finally:
        destroy(box)
    for c in chips:
        print("ok  " if c.ok else "FAIL", c.name, "-", c.detail)
    return 0 if all(c.ok for c in chips) else 1


if __name__ == "__main__":
    sys.exit(main())
