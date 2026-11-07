"""Task 1: per-run hardened Postgres container. Real Docker, no mocks."""
import json
import subprocess
import time

import pytest

from sandbox.container import IMAGE, TEMPLATE_VOLUME, destroy, docker, start_run_container
from sandbox.runner import BACKSTOP_S, run_sql
from sandbox.selftest import self_test
from sandbox.template import STATEMENT_TIMEOUT, TEMP_FILE_LIMIT, refresh_template


def inspect(box):
    return json.loads(docker("inspect", box.id).stdout)[0]


def in_box(box, *cmd):
    return subprocess.run(["docker", "exec", box.id, *cmd], capture_output=True, text=True,
                          encoding="utf-8", timeout=30)


def in_box_as(box, user, *cmd):
    return subprocess.run(["docker", "exec", "-u", user, box.id, *cmd], capture_output=True, text=True,
                          encoding="utf-8", timeout=30)


@pytest.fixture(scope="module")
def box():
    refresh_template()
    b = start_run_container()
    yield b
    destroy(b)


def test_start_under_5s(box):
    t0 = time.perf_counter()
    b = start_run_container()
    ms = (time.perf_counter() - t0) * 1000
    try:
        print(f"\nstart_run_container: {ms:.0f} ms")
        assert run_sql(b, "SELECT 1").ok
        assert ms < 5000
    finally:
        destroy(b)


def test_template_is_quiescent(box):
    # template volume exists and no container (running or stopped) has it attached
    assert docker("volume", "inspect", TEMPLATE_VOLUME).returncode == 0
    assert docker("ps", "-aq", "--filter", f"volume={TEMPLATE_VOLUME}").stdout.strip() == ""
    # cleanly shut down: no postmaster.pid left in the template
    p = docker("run", "--rm", "--network", "none", "--user", "postgres", "--entrypoint", "ls",
               "-v", f"{TEMPLATE_VOLUME}:/t:ro", IMAGE, "/t/postmaster.pid")
    assert p.returncode != 0 and "No such file" in p.stderr, p.stderr
    # the run box holds a copy of the seed, mounts only its own volume, has no network/DSN
    r = run_sql(box, "SELECT count(*) FROM placeholder")
    assert r.ok and int(r.stdout) > 0
    info = inspect(box)
    assert [m["Name"] for m in info["Mounts"] if m["Type"] == "volume"] == [box.volume]
    assert info["HostConfig"]["NetworkMode"] == "none"
    assert not any(k.startswith(("PGHOST", "DATABASE_URL")) for k in info["Config"]["Env"])


def test_exec_role_non_superuser(box):
    r = run_sql(box, "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                     "FROM pg_roles WHERE rolname = current_user")
    assert r.ok and r.stdout.strip() == "f|f|f|f|f"
    r = run_sql(box, "SELECT count(*) FROM pg_roles r WHERE pg_has_role(current_user, r.oid, 'MEMBER') "
                     "AND r.rolname IN ('pg_read_server_files','pg_write_server_files',"
                     "'pg_execute_server_program','postgres')")
    assert r.ok and r.stdout.strip() == "0"
    for table in ("pg_authid", "pg_shadow"):
        r = run_sql(box, f"SELECT 1 FROM {table}")
        assert not r.ok and r.denied and "permission denied" in r.error


def test_server_copy_denied(box):
    r = run_sql(box, "COPY (SELECT 1) TO PROGRAM 'true'")
    assert not r.ok and r.denied is True and r.code == "42501"


def test_timeout_and_temp_limit(box):
    r = run_sql(box, "SHOW statement_timeout; SHOW temp_file_limit")
    assert r.ok and r.stdout.split() == [STATEMENT_TIMEOUT, TEMP_FILE_LIMIT]
    t0 = time.perf_counter()
    r = run_sql(box, "SELECT pg_sleep(30)")
    assert not r.ok and r.code == "57014" and "statement timeout" in r.error
    assert time.perf_counter() - t0 < 15


@pytest.mark.parametrize("host", ["1.1.1.1", "172.17.0.1", "host.docker.internal"])
def test_no_network(box, host):
    p = in_box(box, "psql", f"host={host} port=80 connect_timeout=3 user=x dbname=x")
    assert p.returncode != 0 and ("unreachable" in p.stderr or "translate host name" in p.stderr), p.stderr


def test_metadata_blocked(box):
    p = in_box(box, "psql", "host=169.254.169.254 port=80 connect_timeout=3 user=x dbname=x")
    assert p.returncode != 0 and "unreachable" in p.stderr, p.stderr


def test_socket_only(box):
    assert run_sql(box, "SHOW listen_addresses").stdout.strip() == ""
    # no LISTEN (state 0A) sockets in the container's TCP tables
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        lines = in_box(box, "cat", f).stdout.splitlines()[1:]
        assert not [l for l in lines if l.split()[3] == "0A"], lines
    assert "host=/var/run/postgresql" in box.sock_exec
    assert run_sql(box, "SELECT inet_server_addr() IS NULL").stdout.strip() == "t"  # socket conn
    p = in_box(box, "psql", "host=127.0.0.1 user=executor dbname=drydock", "-c", "SELECT 1")
    assert p.returncode != 0


def test_no_password_anywhere(box):
    env = inspect(box)["Config"]["Env"]
    assert not [e for e in env if "PASS" in e.upper() or "SECRET" in e.upper()], env
    hba = in_box(box, "cat", "/var/lib/postgresql/data/pg_hba.conf").stdout
    rules = [l for l in hba.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert rules and all(l.split()[0] == "local" for l in rules), rules
    assert not [l for l in rules if any(m in l for m in ("password", "md5", "scram", "trust"))]
    conf = in_box(box, "cat", "/var/lib/postgresql/data/postgresql.auto.conf").stdout
    assert "password" not in conf.lower()
    p = in_box(box, "psql", "-XAt", "-d", box.sock_admin,
               "-c", "SELECT count(*) FROM pg_authid WHERE rolpassword IS NOT NULL")
    assert p.stdout.strip() == "0", p.stderr


def test_exec_cannot_become_admin(box):
    r = run_sql(box, "SET ROLE postgres")
    assert not r.ok and r.denied and "permission denied" in r.error


def _not_superuser(box):
    # whatever the model sent, the box still has only the executor path and admin was never reached
    r = run_sql(box, "SELECT current_user, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)")
    assert r.ok and r.stdout.strip() == "executor|f"


@pytest.mark.parametrize("sql", [
    '\\c "host=/var/run/postgresql dbname=drydock user=postgres"',   # reconnect as admin
    "\\! id",                                                         # shell escape
    "\\copy (SELECT 1) TO '/tmp/x'",                                  # client-side file copy
    "\\i /var/lib/postgresql/data/pg_hba.conf",                       # client-side file read
    "SELECT 1;\n\\! id",                                              # meta-command after SQL
    "  \\o |id",                                                      # leading whitespace
])
def test_meta_commands_refused(box, sql):
    r = run_sql(box, sql)
    assert not r.ok and r.error, r
    assert "uid=" not in (r.stdout + (r.error or ""))
    _not_superuser(box)


def test_exec_os_user_cannot_reach_admin(box):
    # even with a shell as the executor's OS user, admin peer auth and PGDATA are out of reach
    p = in_box_as(box, "nobody", "psql", "-XAt", "-d", box.sock_admin, "-c", "SELECT 1")
    assert p.returncode != 0 and "authentication failed" in p.stderr, p.stderr
    p = in_box_as(box, "nobody", "ls", "/var/lib/postgresql/data")
    assert p.returncode != 0 and "Permission denied" in p.stderr


def test_backstop_kills_disabled_timeout(box):
    t0 = time.perf_counter()
    r = run_sql(box, "SET statement_timeout = 0; SELECT pg_sleep(60)")
    elapsed = time.perf_counter() - t0
    assert not r.ok and r.code == "57014" and "backstop" in r.error and r.backstop
    assert elapsed < BACKSTOP_S + 5
    r = run_sql(box, "SELECT count(*) FROM pg_stat_activity WHERE usename = 'executor' "
                     "AND pid <> pg_backend_pid()")
    assert r.ok and r.stdout.strip() == "0"


def test_persisted_timeout_override_ignored(box):
    # PG lets a role ALTER its own user-set GUCs; the runner's per-connection options= wins over it
    assert run_sql(box, "ALTER ROLE executor SET statement_timeout = 0").ok
    assert run_sql(box, "ALTER ROLE executor RESET ALL").ok  # cannot drop superuser-only L
    r = run_sql(box, "SHOW statement_timeout; SHOW temp_file_limit")
    assert r.ok and r.stdout.split() == [STATEMENT_TIMEOUT, TEMP_FILE_LIMIT]
    assert run_sql(box, "SET temp_file_limit = -1").denied


def test_caps_present(box):
    hc = inspect(box)["HostConfig"]
    assert hc["Memory"] > 0 and hc["NanoCpus"] > 0 and hc["PidsLimit"] > 0
    assert hc["NetworkMode"] == "none"
    assert hc["CapDrop"] == ["ALL"] and hc["ReadonlyRootfs"] is True
    assert "/var/run/docker.sock" not in json.dumps(inspect(box)["Mounts"])


def test_self_test_chips(box):
    chips = self_test(box)
    for c in chips:
        print(f"\n{c.name}: {c.ok} {c.detail}")
    assert len(chips) == 4
    assert all(c.ok for c in chips), chips


@pytest.mark.parametrize("sql", ["DO $$ BEGIN NULL; END $$", "CREATE TEMP TABLE t (a int)", "SELECT lo_from_bytea(0, 'x')",
                                 "SELECT lo_create(0)", "SELECT pg_read_file('/etc/passwd')", "SELECT lo_export(0, '/tmp/x')",
                                 "COPY invoices FROM '/etc/passwd'", "CREATE EXTENSION dblink", "CREATE TABLE t (a int)"])
def test_executor_denied(box, sql):  # audit-1107 S1, S15: turns "by default" into evidence
    r = run_sql(box, sql)
    assert not r.ok and r.code == "42501", (sql, r.code, r.error)


def test_destroy():
    b = start_run_container()
    assert run_sql(b, "SELECT 1").ok
    destroyed_at = destroy(b)
    assert destroyed_at >= b.created_at
    assert docker("inspect", b.id).returncode != 0
    assert docker("volume", "inspect", b.volume).returncode != 0
    assert not run_sql(b, "SELECT 1").ok
    assert destroy(b) >= destroyed_at  # idempotent: already gone is fine


def test_destroy_raises_when_not_removed(monkeypatch):
    import sandbox.container as c
    b = start_run_container()
    try:
        real = c.docker
        monkeypatch.setattr(c, "docker", lambda *a, **k: real("ps") if a[0] in ("rm", "volume") and a[1] in ("-f", "rm") else real(*a, **k))
        with pytest.raises(RuntimeError):
            destroy(b)  # rm/volume rm silently did nothing -> container still there -> must raise
    finally:
        monkeypatch.undo()
        destroy(b)
