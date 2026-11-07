"""Task 7: apply inside the boundary, undo, checksum, task-end reset (plan D1a, LC1). No model: SQL from golden.json."""
import hashlib
import json
from datetime import timezone
from pathlib import Path

import pytest
import yaml

from core.measure import checksum, checksum_by_table, diff, one_statement, snapshot_hashes
from orch import apply as A
from sandbox.container import HARDEN, destroy, docker, start_run_container
from sandbox import runner as R
from sandbox.runner import apply_sql, ledger_row, psql, run_sql
from sandbox.template import refresh_template
from seed import seed as S

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads((ROOT / "seed" / "golden.json").read_text(encoding="utf-8"))
E6, E8 = GOLDEN["E6"]["sql"], GOLDEN["E8"]["sql"]


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module", autouse=True)
def template(tmp_path_factory):
    p = tmp_path_factory.mktemp("seed") / "seed.sql"
    p.write_text(S.build_sql(), encoding="utf-8")
    refresh_template(p)
    A.seed_checksum(refresh=True)
    yield
    A.shutdown()


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "RECEIPTS_DIR", tmp_path / "receipts")
    A.RUNS.clear()
    A.LEDGER.clear()
    A.demo_reset()  # every test starts from a seed live-db
    yield


def dry_run(sql):
    """The dry run on a disposable copy: the measured effect the human approves. It validates with the same
    one_statement apply uses, so SQL apply would refuse is rejected before approval (audit-1105 #2)."""
    one_statement(sql)
    box = start_run_container()
    try:
        before = snapshot_hashes(box)
        r = run_sql(box, sql)
        assert r.ok, r.error
        return diff(before, snapshot_hashes(box))
    finally:
        destroy(box)


def admin(box, sql):
    p = psql(box.id, box.sock_admin, sql)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def approve(run_id, sql, cls="B", effect=None):
    return A.approve(run_id, sql, sha(sql), effect or dry_run(sql), cls, "billing_ops")


def test_checksum_unchanged_before_approve():
    before = checksum(A.live())
    for _ in range(2):
        dry_run(E8)
    assert checksum(A.live()) == before


def test_apply_in_scope_only():
    effect = dry_run(E8)
    live = A.live()
    before = snapshot_hashes(live)
    r = A.approve("r1", E8, sha(E8), effect, "B", "billing_ops")
    assert r.ok and not r.rolled_back, r.reason
    measured = diff(before, snapshot_hashes(live))
    assert measured.changed_pks == effect.changed_pks == r.effect.changed_pks
    assert sorted(measured.changed_pks) == ["accounts", "invoices"]


def test_snapshot_undo():
    live = A.live()
    admin(live, "UPDATE customers SET name = name || ' (edited)' WHERE id = 1")  # live state != seed
    before = checksum(live)
    assert before != A.seed_checksum()
    effect = dry_run(E6)
    assert approve("r1", E6, effect=effect).ok
    assert checksum(live) != before
    u = A.undo("r1")
    assert u.checksum == before and u.matches_before is True
    assert A.ledger("r1")[-2]["matches_before"] is True  # the undo row, before the task_end row
    end = u.task_end
    assert end.checksum_ok and checksum(A.live()) == A.seed_checksum()


def test_reset_from_seed():
    assert approve("r1", E6).ok
    assert checksum(A.live()) != A.seed_checksum()
    end = A.demo_reset()  # test helper: the public route was removed (audit-1107 security B5)
    assert end.checksum_ok and checksum(A.live()) == A.seed_checksum()


def test_approve_idempotent():
    effect = dry_run(E6)
    first = A.approve("r1", E6, sha(E6), effect, "B", "billing_ops")
    after_first = checksum(A.live())
    second = A.approve("r1", E6, sha(E6), effect, "B", "billing_ops")
    assert first.ok and second is first
    assert checksum(A.live()) == after_first  # E6 again would cut another 10%
    assert sum(row["step"] == "apply" for row in A.ledger("r1")) == 1


def test_next_run_sees_seed():
    assert approve("r1", E6).ok
    A.end_task("r1", "close")
    box = start_run_container()
    try:
        assert checksum(box) == A.seed_checksum()
    finally:
        destroy(box)


def test_task_end_destroys_live():
    assert approve("r1", E6).ok
    old = A.live().id
    end = A.end_task("r1", "close")
    assert docker("inspect", old).returncode != 0
    assert end.new_live_id != old and A.live().id == end.new_live_id
    assert docker("inspect", end.new_live_id).returncode == 0
    assert end.checksum_ok and checksum(A.live()) == A.seed_checksum()
    row = [r for r in A.ledger("r1") if r["step"] == "task_end"][-1]
    assert row["destroyed_at"] and row["reset_at"] and row["reason"] == "close"


@pytest.mark.parametrize("reason", ["reject", "class_c", "undo", "close", "idle_10min", "next_run"])
def test_reset_on_reject_classC_undo_idle_next_run(reason):
    assert approve("r1", E6).ok  # an applied run resets on any reason (tests/test_lifecycle.py: unapplied runs do not)
    end = A.end_task("r1", reason)
    assert end.checksum_ok and checksum(A.live()) == A.seed_checksum()


def test_idle_trigger_fires_after_10min():
    assert approve("r1", E6).ok
    applied = A.RUNS["r1"]["applied_at"]
    assert A.reap_idle(applied.timestamp() + A.IDLE_S - 1) == []
    ends = A.reap_idle(applied.timestamp() + A.IDLE_S + 1)
    assert len(ends) == 1 and checksum(A.live()) == A.seed_checksum()
    assert A.ledger("r1")[-1]["reason"] == "idle_10min"


def test_receipt_survives_reset():
    assert approve("r1", E6).ok
    A.end_task("r1", "close")
    rec = A.receipt("r1")
    assert rec["sql_sha256"] == sha(E6) and rec["effect_match"] is True
    assert rec["role"] == "executor" and rec["approval_class"] == "B" and rec["approver_role_asserted"] == "billing_ops"
    body = {k: v for k, v in rec.items() if k != "receipt_sha256"}
    assert rec["receipt_sha256"] == sha(json.dumps(body, sort_keys=True))


def test_approve_blocked_when_red():
    before = checksum(A.live())
    r = approve("r1", E6, cls="C")
    assert not r.ok and "class C" in r.reason
    assert checksum(A.live()) == before and not A.ledger("r1")


def test_apply_runs_as_executor():
    sql = "UPDATE customers SET name = current_user WHERE id = 1"
    assert approve("r1", sql, cls="A").ok
    assert admin(A.live(), "SELECT name FROM customers WHERE id = 1") == "executor"


INSPECT = "{{json .HostConfig}}|{{.Config.Image}}|{{.Config.User}}"
ROLE_SQL = "SELECT setconfig FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole WHERE r.rolname = 'executor'"


def _limits(box):
    hc, image, user = docker("inspect", "-f", INSPECT, box.id).stdout.strip().split("|")
    hc = json.loads(hc)
    keys = ["NetworkMode", "CapDrop", "CapAdd", "Memory", "MemorySwap", "NanoCpus", "PidsLimit",
            "ReadonlyRootfs", "SecurityOpt", "Tmpfs", "Privileged"]
    return {k: hc.get(k) for k in keys} | {"image": image, "user": user, "role": admin(box, ROLE_SQL)}


def test_apply_same_caps():
    live, box = A.live(), start_run_container()
    try:
        assert _limits(live) == _limits(box)
        assert _limits(live)["NetworkMode"] == "none"
    finally:
        destroy(box)
    svc = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["live-db"]
    assert svc["x-drydock-flags"] == HARDEN  # compose file cannot drift from the run-container flags


def test_apply_timeout_kills():
    sql = "UPDATE customers SET name = name WHERE id = 1 AND (SELECT true FROM pg_sleep(6))"
    before = checksum(A.live())
    effect = diff(snapshot_hashes(A.live()), snapshot_hashes(A.live()))  # approved effect irrelevant: never reached
    r = A.approve("r1", sql, sha(sql), effect, "A", "ops_lead")
    assert not r.ok and r.code == "57014" and r.reason.startswith("stopped by time limit")  # one label (cap_label)
    assert checksum(A.live()) == before
    apply_row, end_row = A.ledger("r1")
    assert apply_row["stopped_by"] == "statement_timeout" and end_row["reason"] == "apply_failed"


def test_apply_sha_mismatch_refused():
    effect = dry_run(E6)
    before = checksum(A.live())
    edited = E6.replace("0.9", "0.5")
    r = A.approve("r1", edited, sha(E6), effect, "B", "billing_ops")
    assert not r.ok and "sha256" in r.reason and not r.rolled_back
    assert checksum(A.live()) == before and not A.ledger("r1")


def drift_e8(live):
    """live drifted from the dry run's template: one more invoice now matches E8's WHERE"""
    admin(live, "UPDATE invoices SET status = 'open', due_on = DATE '2026-01-01' WHERE id = "
                "(SELECT min(id) FROM invoices WHERE status = 'paid')")


def prepared(box):
    return admin(box, "SELECT count(*) FROM pg_prepared_xacts")


def test_apply_effect_mismatch_rolls_back():
    effect = dry_run(E8)
    live = A.live()
    drift_e8(live)
    before = checksum(live)
    r = apply_sql(live, E8, sha(E8), effect)  # runner level: the live-db is still there to inspect
    assert not r.ok and r.rolled_back and "effect" in r.reason
    assert r.effect.changed_pks != effect.changed_pks
    assert checksum(live) == before
    assert prepared(live) == "0"


def test_failed_apply_ends_task():
    effect = dry_run(E8)
    drift_e8(A.live())
    r = A.approve("r1", E8, sha(E8), effect, "B", "billing_ops")
    assert not r.ok and r.rolled_back
    rows = A.ledger("r1")
    assert [x["step"] for x in rows] == ["apply", "task_end"] and rows[1]["reason"] == "apply_failed"
    assert rows[0]["reason"] == r.reason
    assert checksum(A.live()) == A.seed_checksum()
    again = A.approve("r1", E8, sha(E8), effect, "B", "billing_ops")
    assert not again.ok and "ended" in again.reason


# ---- never leave a PREPARED transaction behind (review HIGH) ----
def test_exception_after_prepare_rolls_back():
    live = A.live()
    before = checksum(live)
    r = apply_sql(live, E6, sha(E6), object())  # malformed approved_effect: raises after PREPARE
    assert not r.ok and r.rolled_back and "after PREPARE" in r.reason
    assert prepared(live) == "0" and checksum(live) == before


def _fail_commit(monkeypatch):
    real = R.run_sql
    monkeypatch.setattr(R, "run_sql", lambda box, sql: R.RunResult(False, "boom", False)
                        if sql.startswith("COMMIT PREPARED") else real(box, sql))


def test_failed_commit_prepared_rolls_back(monkeypatch):
    effect = dry_run(E6)
    _fail_commit(monkeypatch)
    live = A.live()
    before = checksum(live)
    r = apply_sql(live, E6, sha(E6), effect)
    assert not r.ok and r.rolled_back and "boom" in r.reason
    assert prepared(live) == "0" and checksum(live) == before


def test_backstop_kill_after_prepare_rolls_back(monkeypatch):
    effect = dry_run(E6)
    real = R.run_sql

    def killed_after_prepare(box, sql):
        r = real(box, sql)
        if "PREPARE TRANSACTION" in sql:  # PREPARE succeeded, then the runner backstop killed psql
            assert r.ok
            return R.RunResult(False, "runner backstop: killed", False, "57014", backstop=True)
        return r

    monkeypatch.setattr(R, "run_sql", killed_after_prepare)
    live = A.live()
    before = checksum(live)
    r = apply_sql(live, E6, sha(E6), effect)
    assert not r.ok and r.rolled_back and "prepared" in r.reason
    assert prepared(live) == "0" and checksum(live) == before


def test_left_prepared_is_truthful_and_resets(monkeypatch):
    effect = dry_run(E6)
    _fail_commit(monkeypatch)
    monkeypatch.setattr(R, "_settle", lambda box, gid: "left")  # ROLLBACK PREPARED also failed
    old = A.live().id
    r = approve("r1", E6, effect=effect)
    assert not r.ok and not r.rolled_back and "left prepared" in r.reason
    rows = A.ledger("r1")
    assert [x["step"] for x in rows] == ["apply", "task_end"] and not rows[0]["ok"]
    assert docker("inspect", old).returncode != 0  # the box holding the prepared xact is gone
    assert prepared(A.live()) == "0" and checksum(A.live()) == A.seed_checksum()


def test_approve_exception_logs_and_resets(monkeypatch):
    effect = dry_run(E6)

    def boom(*a):
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(A, "apply_sql", boom)
    old = A.live().id
    with pytest.raises(RuntimeError, match="exploded"):
        approve("r1", E6, effect=effect)
    rows = A.ledger("r1")
    assert [x["step"] for x in rows] == ["apply", "task_end"]
    assert not rows[0]["ok"] and rows[0]["reason"] == "apply error: RuntimeError" and "exploded" not in rows[0]["reason"]
    assert docker("inspect", old).returncode != 0 and checksum(A.live()) == A.seed_checksum()


# ---- lifecycle (judge G1/G2, assumptions #3/#4) ----
def test_approve_refuses_ended_run():
    A.end_task("r1", "reject")  # ended before any approve
    r = approve("r1", E6)
    assert not r.ok and "ended" in r.reason
    assert checksum(A.live()) == A.seed_checksum()


def test_stale_result_after_undo_refused():
    assert approve("r1", E6).ok
    A.undo("r1")
    r = approve("r1", E6)
    assert not r.ok and "ended" in r.reason


def test_reset_ends_every_open_run():
    assert approve("r1", E6).ok
    A.demo_reset()
    assert A.RUNS["r1"]["ended"] and "snapshot" not in A.RUNS["r1"]
    assert A.reap_idle(A.RUNS["r1"]["applied_at"].timestamp() + A.IDLE_S + 1) == []


def test_next_run_ends_previous_task():
    assert approve("r1", E6).ok
    assert A.next_run("r1") is None  # same run: nothing to end
    end = A.next_run("r2")
    assert end.checksum_ok and checksum(A.live()) == A.seed_checksum()
    assert A.ledger("r1")[-1]["reason"] == "next_run"
    with pytest.raises(RuntimeError):
        A.undo("r1")
    assert approve("r2", E6).ok  # run 2 applies on seed, not on run 1's state
    assert A.next_run("r2") is None


# ---- wow: per-table checksums, warm start ----
def test_checksum_by_table_in_receipt():
    assert approve("r1", E8).ok
    rec = A.receipt("r1")
    b, a = rec["checksum_by_table_before"], rec["checksum_by_table_after"]
    assert set(b) == set(a) == set(S.TABLES)
    assert sorted(t for t in S.TABLES if b[t] != a[t]) == ["accounts", "invoices"]
    assert checksum_by_table(A.live()) == a


def test_warm_start(monkeypatch):
    monkeypatch.setattr(A, "_seed_checksum", None)
    A.warm()
    assert A._seed_checksum == checksum(A.live())

    def no_box(*a):
        raise AssertionError("seed box started after warm()")

    monkeypatch.setattr(A, "start_run_container", no_box)
    assert A.seed_checksum() == A._seed_checksum


# ---- minors ----
def test_receipt_path_validated():
    for bad in ("../x", "a/b", "", "x" * 65):
        with pytest.raises(ValueError):
            A.receipt(bad)
    r = A.approve("../evil", E6, sha(E6), None, "B", "billing_ops")
    assert not r.ok and "run_id" in r.reason


def test_receipt_failure_resets(monkeypatch):
    effect = dry_run(E6)

    def fail(*a):
        raise OSError("disk full")

    monkeypatch.setattr(A, "_write_receipt", fail)
    with pytest.raises(OSError):
        approve("r1", E6, effect=effect)
    assert "result" not in A.RUNS["r1"]  # no state for an apply that has no receipt
    assert A.ledger("r1")[-1]["reason"] == "apply_failed"
    assert checksum(A.live()) == A.seed_checksum()


def test_failed_reset_checksum_leaves_no_live(monkeypatch):
    monkeypatch.setattr(A, "seed_checksum", lambda refresh=False: "not-the-seed")
    A.RUNS["r1"] = {"applied_at": A.now()}
    with pytest.raises(RuntimeError, match="checksum"):
        A.end_task("r1", "close")
    assert A._live is None
    assert docker("inspect", A.LEDGER[-1]["new_live_id"]).returncode != 0


def test_restart_sweep_removes_orphan_live_volume():
    orphan = start_run_container("live")  # a live-db left by a previous process
    try:
        A.demo_reset()
        assert docker("inspect", orphan.id).returncode != 0
        assert docker("volume", "inspect", orphan.volume).returncode != 0
    finally:
        docker("volume", "rm", "-f", orphan.volume)


def test_apply_ledger_row():
    assert approve("r1", E6).ok
    row = [r for r in A.ledger("r1") if r["step"] == "apply"][0]
    box = start_run_container()
    try:
        r = run_sql(box, "SELECT 1")
        dry = ledger_row("dry_run", box, "SELECT 1", r.ok, r.code, A.now(), A.now())
    finally:
        destroy(box)
    assert row.keys() == dry.keys()
    assert row["role"] == "executor" and row["container_id"] == A.live().id and row["sql_sha256"] == sha(E6)
    assert row["caps"] == dry["caps"]
    for k in ("started_at", "ended_at"):
        assert row[k].tzinfo == timezone.utc


# ---- audit-1105 (.superpowers/sdd/audit-1105-bugs.md) ----
def test_old_run_cannot_approve_on_newer_runs_state():  # audit #1, kept true after audit-1107 S3
    eff6, eff8 = dry_run(E6), dry_run(E8)
    assert A.next_run("a") is None  # run a dry-runs, then waits
    assert A.next_run("b") is None  # run b starts: a stays open, live is still seed
    assert approve("b", E8, effect=eff8).ok
    r = approve("a", E6, effect=eff6)
    assert not r.ok and not r.executed and "another visitor" in r.reason


def test_open_run_can_still_apply_after_a_newer_run_started():  # audit-1107 S3
    eff6 = dry_run(E6)
    A.next_run("a")
    A.next_run("b")
    assert approve("a", E6, effect=eff6).ok
    assert A.next_run("c").checksum_ok and A.RUNS["a"]["ended"]  # the applied one ends, and only it
    assert not A.RUNS["b"].get("ended")


def test_trailing_comment_sql_applies():  # audit #2a: "-- note" must not comment out the wrapper
    sql = E8.rstrip().rstrip(";") + " -- duplicate charge"
    r = approve("r1", sql, effect=dry_run(sql))
    assert r.ok, r.reason


def test_dry_run_rejects_what_apply_would_refuse():  # audit #2b: rejected before approval, not after
    for sql in (E8.rstrip().rstrip(";") + "; -- duplicate charge", "SELECT 1; SELECT 2"):
        with pytest.raises(ValueError, match="one statement"):
            dry_run(sql)


def test_failed_reset_still_ends_runs(monkeypatch):  # audit #3
    assert approve("r1", E6).ok

    def down():
        raise RuntimeError("docker hiccup")

    monkeypatch.setattr(A, "recreate_live", down)
    with pytest.raises(RuntimeError, match="hiccup"):
        A.demo_reset()
    monkeypatch.undo()
    assert A.RUNS["r1"]["ended"] and "snapshot" not in A.RUNS["r1"]
    r = approve("r1", E6)
    assert not r.ok and "ended" in r.reason


@pytest.mark.parametrize("where", ["apply", "receipt"])
def test_reset_error_keeps_apply_error(monkeypatch, where):  # audit #4
    effect = dry_run(E6)

    def boom(*a, **k):
        raise RuntimeError("original cause")

    def reset_fails(*a, **k):
        raise RuntimeError("docker: container still present")

    monkeypatch.setattr(A, "apply_sql" if where == "apply" else "_write_receipt", boom)
    monkeypatch.setattr(A, "destroy", reset_fails)  # the real _reset runs and fails at its docker step (H3)
    with pytest.raises(RuntimeError, match="original cause"):
        approve("r1", E6, effect=effect)
    assert A._live is None  # the old live-db is never reused: the next live() recreates (review-1105 H2/A3)


def test_undo_restores_crlf_sensitive_values():  # audit "Not verified": restore_data stdin from a Windows host
    live = A.live()
    admin(live, "UPDATE customers SET name = E'line1\nline2\r\nline3\r' WHERE id = 1")
    assert admin(live, "SELECT position(chr(13) in name) > 0 FROM customers WHERE id = 1") == "t"  # \r stored (M10)
    before = checksum(live)
    assert approve("r1", E6).ok
    u = A.undo("r1")
    assert u.matches_before and u.checksum == before


# ---- review-1105 ----
def test_destroy_failure_never_keeps_old_live(monkeypatch):  # H2/A3
    assert approve("r1", E6).ok
    old = A.live().id

    def stuck(*a, **k):
        raise RuntimeError("docker: container still present")

    monkeypatch.setattr(A, "destroy", stuck)
    with pytest.raises(RuntimeError, match="still present"):
        A.demo_reset()
    monkeypatch.undo()
    assert A._live is None
    new = A.live()
    assert new.id != old and checksum(new) == A.seed_checksum()
    assert docker("inspect", old).returncode != 0  # recreate_live swept the stuck container


def test_failed_apply_keeps_result_when_reset_fails(monkeypatch):  # A2
    effect = dry_run(E8)
    drift_e8(A.live())

    def stuck(*a, **k):
        raise RuntimeError("docker: container still present")

    monkeypatch.setattr(A, "destroy", stuck)
    r = A.approve("r1", E8, sha(E8), effect, "B", "billing_ops")  # returns the apply result, never the reset error
    assert not r.ok and r.rolled_back and "effect" in r.reason
    assert A.RUNS["r1"]["ended"] and A._live is None


def test_stale_run_refused_without_next_run():  # A1: guard holds even if the caller never calls next_run
    eff6, eff8 = dry_run(E6), dry_run(E8)
    assert approve("b", E8, effect=eff8).ok
    after_b = checksum(A.live())
    r = approve("a", E6, effect=eff6)
    assert not r.ok and "another visitor" in r.reason and not r.executed
    assert checksum(A.live()) == after_b  # b's change stays, a never ran


def test_next_run_without_apply_skips_reset():  # A6, audit-1107 S3: nothing applied -> nothing ends, no reset
    A.next_run("a")
    old = A.live().id
    assert A.next_run("b") is None
    assert not A.RUNS["a"].get("ended") and A.live().id == old
