"""refresh_template(): seed SQL -> stopped, quiescent template volume (run once at build)."""
from pathlib import Path

from sandbox.container import DB, IMAGE, PGDATA, SOCK_DIR, STATEMENT_TIMEOUT, TEMPLATE_VOLUME, _ok, docker

# Executor caps. T and L: every later number (Task 2 sizing, Task 8 guard) uses these.
# T = STATEMENT_TIMEOUT (5s, defined in container.py)
TEMP_FILE_LIMIT = "64MB"    # L

PLACEHOLDER_SEED = Path(__file__).with_name("seed_placeholder.sql")  # Task 2 supplies the real seed

ROLES_SQL = f"""
CREATE ROLE executor LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE executor SET statement_timeout = '{STATEMENT_TIMEOUT}';
ALTER ROLE executor SET temp_file_limit = '{TEMP_FILE_LIMIT}';
GRANT USAGE ON SCHEMA public TO executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO executor;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO executor;
REVOKE USAGE ON LANGUAGE plpgsql FROM PUBLIC;
REVOKE TEMP ON DATABASE {DB} FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION lo_create(oid), lo_creat(integer), lo_from_bytea(oid, bytea), lo_put(oid, bigint, bytea),
  lowrite(integer, bytea), lo_truncate(integer, integer), lo_truncate64(integer, bigint), lo_unlink(oid) FROM PUBLIC;
"""

# Client auth: local socket + peer only, no host lines, no passwords.
# OS user postgres -> admin `postgres` (runner setup only). OS user nobody -> `executor` only (model SQL).
EXEC_OS_USER = "nobody"
PG_HBA = "local all postgres peer\nlocal drydock executor peer map=exec\n"
PG_IDENT = f"exec {EXEC_OS_USER} executor\n"
# max_prepared_transactions: the apply holds its one transaction PREPAREd while the runner measures the effect
# (sandbox/runner.py apply_sql). 1 = one apply at a time under the one-sandbox lock.
CONF = f"listen_addresses = ''\nunix_socket_directories = '{SOCK_DIR}'\nmax_prepared_transactions = 1\n"

BUILD = f"""set -e
initdb -D {PGDATA} -U postgres -E UTF8 --locale=C --auth=peer --no-instructions >/dev/null
printf '%s' "{PG_HBA}" > {PGDATA}/pg_hba.conf
printf '%s' "{PG_IDENT}" > {PGDATA}/pg_ident.conf
printf '%s' "{CONF}" >> {PGDATA}/postgresql.conf
pg_ctl -D {PGDATA} -w -s start
createdb -h {SOCK_DIR} {DB}
psql -X -q -v ON_ERROR_STOP=1 -h {SOCK_DIR} -d {DB}
pg_ctl -D {PGDATA} -w -s -m fast stop
"""


def refresh_template(seed_sql: Path = PLACEHOLDER_SEED) -> None:
    # Fails loudly if any container still uses the template (it must stay quiescent).
    rm = docker("volume", "rm", "-f", TEMPLATE_VOLUME)
    if rm.returncode != 0:
        raise RuntimeError(f"template in use: {rm.stderr.strip()}")
    _ok(docker("volume", "create", "--label", "drydock=template", TEMPLATE_VOLUME))
    _ok(docker("run", "--rm", "-i", "--network", "none", "--user", "postgres", "--entrypoint", "bash",
               "-v", f"{TEMPLATE_VOLUME}:{PGDATA}", IMAGE, "-c", BUILD,
               input=Path(seed_sql).read_text(encoding="utf-8") + ROLES_SQL, timeout=300))
