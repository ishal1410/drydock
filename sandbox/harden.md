# Sandbox hardening

Each run gets a fresh Postgres 17 container whose data volume is a copy of a stopped seed template. All model SQL goes runner -> `docker exec -u nobody` -> `psql -c` -> the container's own Unix socket -> `executor` role. Numbers live in `docs/BRIEF.md`.

## Socket access: `docker exec psql` (option a)

The design says Postgres listens **only on a Unix socket**, the container has `--network none`, and there are no passwords. On Windows, a host process cannot use a Unix socket that lives inside a Linux container through a bind mount. So the runner never opens the socket from the host. It runs `docker exec -u nobody <box> psql -d "host=/var/run/postgresql dbname=drydock user=executor options='-c statement_timeout=5s'" -c <sql>`. It parses the result from `-At` stdout and from the SQLSTATE in stderr (`VERBOSITY=verbose`).

- The same code runs on Windows dev and on the Ubuntu VM, with no extra image and no shared socket volume.
- Trade-off: one `docker exec` per statement (~100-200 ms overhead) and text output instead of typed rows. Task 8 can parse more fields from psql output. If that gets too slow, option (b) is a runner container that shares a per-run named volume for the socket directory.
- Only the runner process uses the Docker CLI. No DB container ever mounts `/var/run/docker.sock` (`test_caps_present` checks this).
- `RunBox.sock_admin` / `sock_exec` are libpq conninfo strings that point to the same in-container socket directory. The identity comes from the role, not from the socket.

## Container flags (`sandbox/container.py` `HARDEN`)

| Flag | Why |
|---|---|
| `--network none` | Only loopback. No egress, no host, no `169.254.169.254` metadata (`test_no_network`, `test_metadata_blocked`) |
| `--memory 512m --memory-swap 512m` | Hard cgroup memory cap, no swap. `work_mem` is **not** a memory cap |
| `--cpus 1` | CPU cap |
| `--pids-limit 128` | No fork bombs. Postgres needs ~10 processes |
| `--cap-drop ALL` | Postgres runs as uid 999 and owns its files, so it needs no capabilities |
| `--security-opt no-new-privileges` | No setuid escalation |
| `--read-only` | Read-only root fs. Postgres writes only to its data volume and the socket tmpfs |
| `--tmpfs /var/run/postgresql:...,uid=999,mode=0711` | Socket directory owned by postgres; `nobody` may traverse it to reach the socket but not list it |
| `--user postgres` + `--entrypoint postgres` | Skips the image entrypoint, so no `POSTGRES_PASSWORD` or init env is needed |
| `-v drydock-run-<id>:/var/lib/postgresql/data` | Its own copied volume. The only mount |

The template copy runs in a short `--rm --network none --user postgres` container that mounts the template **read-only**.

**VM-only (Task deploy):** add a `DOCKER-USER` iptables DROP for `169.254.0.0/16` and the host IP. That covers the case where a container ever gets a network. `--network none` already blocks egress on its own.

**Known gap:** the stock `postgres:17` image still contains bash/coreutils ("no shell tooling" would need a custom image, which means a pull/build, not done). Nothing in the sandbox needs a shell: the network probes use `psql` itself.

## Socket-only, password-free auth

- `postgresql.conf`: `listen_addresses = ''` (no TCP listener; `test_socket_only` checks that `/proc/net/tcp*` has no LISTEN entries), `unix_socket_directories = '/var/run/postgresql'`.
- `pg_hba.conf` (whole file):
  ```
  local all postgres peer
  local drydock executor peer map=exec
  ```
  `pg_ident.conf`: `exec nobody executor`. OS user `postgres` maps only to admin `postgres` (runner setup, backstop kill); OS user `nobody` maps only to `executor` (model SQL). Only local-socket `peer` lines, so no `host` lines, no `trust`, no passwords.
- `initdb` runs without `--pwfile`, so no role has a password (`pg_authid.rolpassword` is NULL for every role). The container env has no password variable.

## Role split

- **Admin = `postgres`** (bootstrap superuser). Used only by the runner for setup and for the backstop kill.
- **Executor = `executor`**: `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`. It is a member of no role (not `postgres`, not `pg_read_server_files` / `pg_write_server_files` / `pg_execute_server_program`). It has `USAGE` on `public`, DML (`SELECT/INSERT/UPDATE/DELETE`) on the seed tables, and use of the sequences. It can connect only to `drydock`.
  - `COPY ... TO PROGRAM` fails with 42501 (`denied=True`). `pg_authid` / `pg_shadow` fail with permission denied. `SET ROLE postgres` fails with permission denied.
- **Executor caps (final values; Task 2 sizing and the Task 8 guard use these):**
  - **T = `statement_timeout` = 5s**
  - **L = `temp_file_limit` = 64MB**

  Both are set with `ALTER ROLE executor SET`; T is also sent per connection (`options='-c statement_timeout=5s'` in `sock_exec`), which outranks role settings.
  - `temp_file_limit` is superuser-only: the executor cannot `SET` it (42501), and `ALTER ROLE executor RESET ALL` leaves it in place.
  - `statement_timeout` is user-settable. Postgres has no privilege that stops a role from `SET statement_timeout = 0` or `ALTER ROLE executor SET statement_timeout = 0` on itself. The persistent `ALTER ROLE` path is neutralised by the per-connection `options=` (`test_persisted_timeout_override_ignored`); an in-statement `SET` is not.
  - **Hard ceiling = runner backstop, T + 10 s = 15 s.** `run_sql` kills the psql call at 15 s and runs `pg_terminate_backend` on executor sessions as admin; if that kill times out or fails, it raises so the caller destroys the box. `test_backstop_kills_disabled_timeout` runs `SET statement_timeout = 0; SELECT pg_sleep(60)` and checks it is killed with 0 executor sessions left. Task 8 should still reject statement-leading SET/RESET/ALTER, and set_config( anywhere, in model SQL (plan v4.2: leading keyword only, never "anywhere", which would block every `UPDATE … SET`).

## Executor client path (no meta-commands, no privileged OS user)

- The SQL is passed as one `psql -c` argument. psql sends a `-c` string to the server verbatim and does not lex it for backslash meta-commands, so `\c`, `\!`, `\copy`, `\o`, `\i`, `\gexec` inside the SQL are just syntax errors. A `-c` string that itself starts with `\` *is* run as a meta-command, so `run_sql` refuses any input whose first non-space character is `\` (`denied=True`).
- Defense in depth: the client runs as OS user `nobody`. Even with a shell as `nobody`, peer auth refuses `postgres`, and PGDATA (mode 700, owned by postgres) is unreadable (`test_exec_os_user_cannot_reach_admin`). Verified by hand without the backslash guard: `\c drydock postgres` -> `Peer authentication failed for user "postgres"`.
- `test_meta_commands_refused` covers reconnect-as-admin, shell escape, client-side `\copy`, `\i`, a meta-command after SQL, and leading whitespace.

## Teardown

`destroy()` removes container and volume, then re-inspects both; it returns `destroyed_at` only if both are gone and raises otherwise. Already-gone is fine (idempotent).

## Template rule (§10 C1)

PostgreSQL 17 docs, *Template Databases* (https://www.postgresql.org/docs/17/manage-ag-templatedbs.html):

> "The principal limitation is that no other sessions can be connected to the source database while it is being copied. `CREATE DATABASE` will fail if any other connection exists when it starts; during the copy operation, new connections to the source database are prevented."

So DryDock never clones from a live database. `refresh_template()` builds the `drydock_template` volume once from the seed SQL (initdb -> config -> seed -> roles -> `pg_ctl stop`), in a `--rm` container, and leaves no container attached to it. Each run copies the stopped volume at the file level, so the copy has exactly one client: the runner. There is no template refresh after an apply. Per the plan (cut S5), this is documented here instead of tested.

## Measured

- `start_run_container()` cold (volume copy + start + ready): **1054 ms** (Task 1), **972 ms** (fix round 1) on Docker Desktop / Windows (placeholder seed). That is under the 5 s target, so no pool is needed. Re-measure on the VM with the real Task 2 seed.

## Apply inside the boundary (Task 7, plan D1a / LC1)

- The live demo DB is a `drydock-live-<id>` container from `start_run_container("live")`: same image, same `HARDEN` flags, same template copy, same executor role and caps (`test_apply_same_caps`, which also pins `deploy/docker-compose.yml` `x-drydock-flags` to `HARDEN`).
- `runner.apply_sql`: sha256 pin (refuse, nothing executed) -> one statement only (`measure.one_statement`: no second statement can run in the wrapper; a lone `COMMIT` or `PREPARE TRANSACTION` still passes, but it only ends the wrapper with an empty effect that the dry run already showed, so the effect match still gates it) -> `BEGIN; <sql>; <snapshot>; PREPARE TRANSACTION` as the executor via `run_sql` -> diff against the pre-apply snapshot -> `COMMIT PREPARED` if tables, per-table counts and changed pks equal the approved dry run, else `ROLLBACK PREPARED`. Any failure after `PREPARE` (exception, failed finish, backstop kill) gets a best-effort admin `ROLLBACK PREPARED`; the result says whether it really rolled back, and `orch.apply.approve` logs a ledger row and resets the live-db, so no prepared transaction outlives the task. Needs `max_prepared_transactions = 1` (template `CONF`).
- Undo = restore the pre-apply `pg_dump --data-only` (admin, trusted), then task end. Task end (`orch/apply.end_task`) = destroy live container + volume, `recreate_live()` from the seed template, live checksum (content-only, `measure.checksum`) == seed checksum, ledger `destroyed_at` / `reset_at`. The template is never refreshed after an apply.
