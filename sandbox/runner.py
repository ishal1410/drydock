"""run_sql(box, sql): the only path model SQL takes.

docker exec -u nobody -> psql -c -> container-local socket -> executor (peer map: OS nobody -> executor only).
"""
import hashlib
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sandbox.container import DB, HARDEN, SOCK_DIR, RunBox, _ok, docker
from sandbox.template import EXEC_OS_USER, STATEMENT_TIMEOUT, TEMP_FILE_LIMIT

DENIED_CODES = {"42501"}  # insufficient_privilege
# Runner-side backstop = the hard ceiling: the executor can lower/disable its own statement_timeout
# (user-set GUC), so the runner also kills the session if psql outlives T by this margin.
BACKSTOP_S = int(STATEMENT_TIMEOUT.rstrip("s")) + 10
# Plain-language stop label for the UI, derived from the SQLSTATE Postgres reported (never hard-coded per demo).
CAP_LABELS = {"53400": "disk spill cap", "57014": "time limit"}  # temp_file_limit, statement_timeout


def cap_label(code: str | None) -> str | None:
    return CAP_LABELS.get(code)


# Model SQL can print anything; the largest honest output is SNAPSHOT_SQL over the whole seed. ponytail: 16 MB is a guess
# above that, unmeasured until Docker is up; a real snapshot over it fails loudly as TOO_MUCH.
MAX_STDOUT = 16 * 1024 * 1024
TOO_MUCH = "54000: statement output exceeded the runner's limit"
KILL_SQL = "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'executor'"


@dataclass
class RunResult:  # Task 8 adds elapsed_ms, caps, stopped_by, ...
    ok: bool
    error: str | None
    denied: bool
    code: str | None = None
    stdout: str = ""
    backstop: bool = False  # True only when the runner (not Postgres) killed the statement
    command_tag: str | None = None  # Postgres's own tag ("UPDATE 87"); only with run_sql(..., tag=True)


def psql(box_id: str, conninfo: str, sql: str, timeout: float = BACKSTOP_S, user: str = "postgres", quiet: bool = True):
    # SQL goes in one -c string: psql sends it to the server verbatim and never lexes it for
    # backslash meta-commands (\c, \!, \copy, \o, \i, \gexec), unless the string itself starts with "\".
    return _capture(
        ["docker", "exec", "-u", user, box_id, "psql", "-X", *(["-q"] if quiet else []), "-At", "-v", "ON_ERROR_STOP=1",
         "-v", "VERBOSITY=verbose", "-d", conninfo, "-c", sql], timeout=timeout)


def _capture(cmd, timeout=None, limit=None) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output=True) with stdout bounded: past the limit the client is killed and it reads as an
    error, so one SELECT cannot fill the orchestrator's memory. stderr is bounded too, without killing."""
    limit = MAX_STDOUT if limit is None else limit
    out, err, over = bytearray(), bytearray(), threading.Event()
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as p:
        def read_out():
            while chunk := p.stdout.read(65536):
                if len(out) + len(chunk) > limit:
                    over.set()
                    p.kill()
                    return
                out.extend(chunk)

        def read_err():
            while chunk := p.stderr.read(65536):
                err.extend(chunk[:65536 - len(err)])

        readers = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
        for t in readers:
            t.start()
        try:
            code = p.wait(timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            raise
        finally:
            for t in readers:
                t.join(5)

    def text(b):
        return bytes(b).decode("utf-8", "replace").replace("\r\n", "\n")
    if over.is_set():
        return subprocess.CompletedProcess(cmd, 1, text(out), f"ERROR:  {TOO_MUCH}")
    return subprocess.CompletedProcess(cmd, code, text(out), text(err))


def _backstop_kill(box: RunBox) -> None:
    try:
        p = psql(box.id, box.sock_admin, KILL_SQL, timeout=10)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{box.id}: backstop kill timed out; destroy the box") from None
    if p.returncode != 0:
        raise RuntimeError(f"{box.id}: backstop kill failed ({p.stderr.strip()}); destroy the box")


COMMAND_TAG = re.compile(r"(?:INSERT \d+ \d+|(?:UPDATE|DELETE|MERGE) \d+)\s*\Z")


def run_sql(box: RunBox, sql: str, tag: bool = False) -> RunResult:
    """tag=True drops psql -q so stdout ends with the command tag; read it back only for one DML statement (wrappers
    would print BEGIN/ROLLBACK too, so every other caller stays quiet and parses stdout as rows)."""
    if sql.lstrip().startswith("\\"):
        return RunResult(False, "refused: psql meta-command", True)
    try:
        p = psql(box.id, box.sock_exec, sql, user=EXEC_OS_USER, **({"quiet": False} if tag else {}))
    except subprocess.TimeoutExpired:
        _backstop_kill(box)
        return RunResult(False, f"runner backstop: killed after {BACKSTOP_S}s", False, "57014", backstop=True)
    if TOO_MUCH in p.stderr:  # the client was killed; the backend may still be producing rows
        _backstop_kill(box)
        return RunResult(False, f"ERROR:  {TOO_MUCH}", False, "54000")
    if p.returncode == 0:
        m = COMMAND_TAG.search(p.stdout) if tag else None
        return RunResult(True, None, False, None, p.stdout, command_tag=m and m.group().strip())
    m = re.search(r"ERROR:\s+([0-9A-Z]{5}):", p.stderr)
    code = m.group(1) if m else None
    return RunResult(False, p.stderr.strip(), code in DENIED_CODES, code, p.stdout)


# ---- Ledger row: one shape for a dry run and an apply (plan D1a "Audit"). ----
STOPPED_BY = {"57014": "statement_timeout", "53400": "temp_file_limit"}
CAPS = {"statement_timeout": STATEMENT_TIMEOUT, "temp_file_limit": TEMP_FILE_LIMIT, "container_flags": HARDEN}


def now() -> datetime:
    return datetime.now(timezone.utc)


def ledger_row(step: str, box: RunBox, sql: str, ok: bool, code: str | None, started_at: datetime,
               ended_at: datetime, run_id=None, reason: str | None = None) -> dict:
    return {"run_id": run_id, "step": step, "container_id": box.id, "role": "executor", "caps": CAPS,
            "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(), "ok": ok, "code": code,
            "stopped_by": STOPPED_BY.get(code), "reason": reason, "started_at": started_at, "ended_at": ended_at}


# ---- Apply inside the boundary (plan D1a): same runner path, same executor role, same caps as the dry run. ----
@dataclass
class ApplyResult:
    ok: bool
    effect: object          # core.measure.Effect measured inside the transaction; None if nothing ran
    rolled_back: bool       # True = SQL ran and its transaction was undone (cap, error or effect mismatch)
    reason: str
    started_at: datetime
    ended_at: datetime
    code: str | None = None  # SQLSTATE when Postgres stopped the statement (ledger stopped_by)
    executed: bool = True    # False = refused before anything ran on the live-db (nothing to log or reset)


SETTLE_MSG = {"rolled_back": "prepared transaction rolled back",
              "absent": "nothing left prepared",
              "left": "left prepared (ROLLBACK PREPARED failed); live-db must be reset"}


def _line(err: str | None) -> str:
    """First line only: psql DETAIL lines can hold row values, and these reasons reach the browser (S4)."""
    return (err or "").strip().splitlines()[0] if (err or "").strip() else "no error text"


def _settle(box: RunBox, gid: str) -> str:
    """Best-effort ROLLBACK PREPARED as admin (trusted runner code). Never raises.
    Returns "rolled_back" (we undid it), "absent" (gid not prepared: never was, or already finished) or "left"."""
    try:
        if psql(box.id, box.sock_admin, f"ROLLBACK PREPARED '{gid}'", timeout=10).returncode == 0:
            return "rolled_back"
        q = psql(box.id, box.sock_admin, f"SELECT count(*) FROM pg_prepared_xacts WHERE gid = '{gid}'", timeout=10)
        return "absent" if q.returncode == 0 and q.stdout.strip() == "0" else "left"
    except Exception:  # noqa: BLE001 - docker/psql down: report "left" so the caller resets the live-db
        return "left"


def apply_sql(live_box: RunBox, sql: str, approved_sha: str, approved_effect) -> ApplyResult:
    """Guards: (1) sha256 pin, else refuse with nothing executed; (2) one transaction whose effect is measured before
    commit and rolled back if tables, per-table counts or changed pks differ from the approved dry run.

    The transaction is PREPAREd (two-phase commit) after the SQL and an in-transaction snapshot, so the runner can
    diff it and then COMMIT PREPARED or ROLLBACK PREPARED. All of it goes through run_sql as the executor role."""
    from core.measure import (  # measure imports us
        SNAPSHOT_SQL,
        diff,
        one_statement,
        parse_snapshot,
        snapshot_hashes,
    )

    started = now()
    if hashlib.sha256(sql.encode("utf-8")).hexdigest() != approved_sha:
        return ApplyResult(False, None, False, "refused: SQL sha256 differs from the approved dry run; nothing executed",
                           started, now(), executed=False)
    try:
        # one statement: no second statement can run inside the wrapper. A lone COMMIT/PREPARE is harmless: it ends
        # the wrapper with an empty effect, which the dry run already showed, so the effect match still gates it.
        stmt = one_statement(sql)
    except ValueError as e:
        return ApplyResult(False, None, False, f"refused: {e}; nothing executed", started, now(), executed=False)
    before = snapshot_hashes(live_box)
    gid = f"drydock_apply_{uuid.uuid4().hex[:12]}"
    # newlines: a trailing "-- note" in stmt ends at its line, never comments out the wrapper (audit-1105 #2)
    r = run_sql(live_box, f"BEGIN;\n{stmt}\n;\n{SNAPSHOT_SQL};\nPREPARE TRANSACTION '{gid}'")
    if not r.ok:
        label = cap_label(r.code)  # the strip's words; the ledger's stopped_by keeps the GUC name
        why = f"stopped by {label}" if label else f"apply failed: {_line(r.error)}"
        if r.backstop:  # the runner killed psql: PREPARE may have finished first, and a prepared xact outlives it
            s = _settle(live_box, gid)  # "absent" = never prepared, so Postgres rolled it back with the session
            return ApplyResult(False, None, s != "left", f"{why}; {SETTLE_MSG[s]}", started, now(), r.code)
        # Postgres error or cap inside the transaction: it aborted before PREPARE, nothing was prepared
        return ApplyResult(False, None, True, why, started, now(), r.code)
    effect = None
    try:
        effect = diff(before, parse_snapshot(r.stdout))
        # money too (S2): the same rows at different amounts is not the change the approver saw (random(), now()-based
        # arithmetic). Column summaries stay out: a timestamp column would differ between two honest runs.
        match = (effect.rows_by_table == approved_effect.rows_by_table
                 and effect.changed_pks == approved_effect.changed_pks
                 and effect.money_cents_by_column == approved_effect.money_cents_by_column)
        verb = "COMMIT" if match else "ROLLBACK"
        fin = run_sql(live_box, f"{verb} PREPARED '{gid}'")
    except BaseException as e:  # anything between PREPARE and its finish must not leave the xact prepared
        s = _settle(live_box, gid)
        if not isinstance(e, Exception):
            raise
        return ApplyResult(False, effect, s == "rolled_back", f"apply error after PREPARE ({type(e).__name__}); {SETTLE_MSG[s]}",
                           started, now())
    if not fin.ok:
        s = _settle(live_box, gid)
        # "absent" after a failed COMMIT = outcome unknown, so never claim rolled_back; the caller resets live anyway
        return ApplyResult(False, effect, s == "rolled_back" or (s == "absent" and not match),
                           f"{verb} PREPARED failed ({_line(fin.error)}); {SETTLE_MSG[s]}", started, now(), fin.code)
    if not match:
        return ApplyResult(False, effect, True, "rolled back: measured effect differs from the approved dry run",
                           started, now())
    return ApplyResult(True, effect, False, "applied: effect matches the approved dry run", started, now())


# ---- Undo snapshot = pre-apply data dump (trusted runner code, admin role). ----
def dump_data(box: RunBox) -> str:
    from seed.seed import TABLES
    tables = [a for t in TABLES for a in ("-t", t)]
    return _ok(docker("exec", "-u", "postgres", box.id, "pg_dump", "-h", SOCK_DIR, "-d", DB, "--data-only",
                      *tables)).stdout


def restore_data(box: RunBox, dump: str) -> None:
    from seed.seed import TABLES
    _ok(docker("exec", "-i", "-u", "postgres", box.id, "psql", "-X", "-q", "-1", "-v", "ON_ERROR_STOP=1",
               "-h", SOCK_DIR, "-d", DB, input=f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE;\n" + dump))
