"""Per-run hardened Postgres container, copied from the quiescent template volume.

Docker control lives here (the runner side) only; no DB container ever gets the Docker socket.
"""
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

IMAGE = "postgres@sha256:3b0cda93be7c8dba2ed484d19393d93845dc3dafb88973e248ba8e6d99d784e1"  # postgres:17 (17.x) pinned by digest: hashint4 values in seed/golden.json depend on it
TEMPLATE_VOLUME = "drydock_template"
PGDATA = "/var/lib/postgresql/data"
SOCK_DIR = "/var/run/postgresql"
DB = "drydock"
# T: also set per connection (options=) so an executor ALTER ROLE ... SET cannot persist an override.
STATEMENT_TIMEOUT = "5s"

# Every flag is explained in sandbox/harden.md.
MEMORY = "512m"
HARDEN = [
    "--network", "none",
    "--memory", MEMORY, "--memory-swap", MEMORY,
    "--cpus", "1",
    "--pids-limit", "128",
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--read-only",
    "--tmpfs", f"{SOCK_DIR}:rw,nosuid,nodev,noexec,size=1m,uid=999,gid=999,mode=0711",
    "--user", "postgres",
]


@dataclass(frozen=True)
class RunBox:
    id: str            # container name
    sock_admin: str    # libpq conninfo, valid only inside the container (socket dir + admin role)
    sock_exec: str     # same socket, executor role
    created_at: datetime
    volume: str


def docker(*args, input=None, timeout=60):
    return subprocess.run(["docker", *args], input=input, capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout)


def _ok(p):
    if p.returncode != 0:
        raise RuntimeError(f"docker {p.args[1:]} failed: {p.stderr.strip()}")
    return p


def start_run_container(kind: str = "run") -> RunBox:
    """kind "run" = per-run copy; "live" = the live demo DB (same image, same HARDEN flags, same template copy)."""
    name = f"drydock-{kind}-{uuid.uuid4().hex[:12]}"
    box = RunBox(name, f"host={SOCK_DIR} dbname={DB} user=postgres",
                 f"host={SOCK_DIR} dbname={DB} user=executor options='-c statement_timeout={STATEMENT_TIMEOUT}'",
                 datetime.now(timezone.utc), name)
    try:
        _ok(docker("volume", "create", "--label", f"drydock={kind}", name))
        # Copy the stopped template into the run volume (short-lived, no network, template read-only).
        _ok(docker("run", "--rm", "--network", "none", "--user", "postgres", "--entrypoint", "sh",
                   "-v", f"{TEMPLATE_VOLUME}:/template:ro", "-v", f"{name}:{PGDATA}", IMAGE,
                   "-c", f"cp -a /template/. {PGDATA}/ && chmod 700 {PGDATA}"))
        _ok(docker("run", "-d", "--name", name, "--label", f"drydock={kind}", *HARDEN,
                   "-v", f"{name}:{PGDATA}", "--entrypoint", "postgres", IMAGE))
        deadline = time.monotonic() + 30
        while docker("exec", name, "pg_isready", "-q", "-h", SOCK_DIR).returncode != 0:
            if time.monotonic() > deadline:
                raise RuntimeError(f"{name} not ready: {docker('logs', name).stderr[-500:]}")
            time.sleep(0.05)
        return box
    except Exception:
        destroy(box)
        raise


def destroy(box: RunBox) -> datetime:
    """Remove container + volume; returns destroyed_at only once both are verifiably gone."""
    rm = docker("rm", "-f", "-v", box.id)
    vol = docker("volume", "rm", "-f", box.volume)
    still = [what for what, p in (("container", docker("inspect", box.id)),
                                  ("volume", docker("volume", "inspect", box.volume))) if p.returncode == 0]
    if still:
        raise RuntimeError(f"destroy {box.id}: {still} still present ({rm.stderr.strip()} {vol.stderr.strip()})")
    return datetime.now(timezone.utc)


def sweep_orphans() -> list:
    """Run at process start: a kill mid-run leaves drydock-run-* containers and volumes that nothing else removes."""
    names = docker("ps", "-a", "--filter", "label=drydock=run", "--format", "{{.Names}}").stdout.split()
    names += docker("volume", "ls", "--filter", "label=drydock=run", "--format", "{{.Name}}").stdout.split()
    orphans = sorted(set(names))
    for name in orphans:
        destroy(RunBox(name, "", "", datetime.now(timezone.utc), name))
    return orphans


def recreate_live() -> RunBox:
    """LC1 task-end reset: destroy every live-db container + volume, start a new one from the seed template, same flags.
    Template = seed forever; nothing is ever copied back into it.
    No -q here: Docker ignores --format with -q and prints ids, and the volume is named after the container name."""
    for name in docker("ps", "-a", "--filter", "label=drydock=live", "--format", "{{.Names}}").stdout.split():
        destroy(RunBox(name, "", "", datetime.now(timezone.utc), name))
    return start_run_container("live")
