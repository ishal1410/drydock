"""Docker-free apply lifecycle: S2 (effect match compares money), S3 (a new run leaves other open runs alone), B11."""
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import core.measure as measure
import orch.apply as A
import sandbox.container as container
import sandbox.runner as runner
from core.measure import Effect, MoneyDelta
from sandbox.runner import RunResult

T0 = datetime(2026, 11, 7, tzinfo=timezone.utc)
SQL = "UPDATE invoices SET amount_cents = 1 WHERE id = 1"


@pytest.fixture(autouse=True)
def clean():
    A.RUNS.clear()
    A.LEDGER.clear()
    yield
    A.RUNS.clear()
    A.LEDGER.clear()


def no_docker(*a, **k):
    raise AssertionError("no Docker call expected")


def applied(rid):
    A.RUNS[rid] = {"applied_at": T0, "result": object(), "snapshot": "dump"}


def test_new_run_leaves_an_unapplied_open_run_alone(monkeypatch):
    monkeypatch.setattr(A, "recreate_live", no_docker)
    A.next_run("a")
    assert A.next_run("b") is None
    assert not A.RUNS["a"].get("ended") and not A.RUNS["b"].get("ended")


def test_new_run_still_ends_an_applied_run(monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_reset", lambda rid, reason, everyone=False: calls.append((rid, reason)))
    applied("a")
    A.next_run("b")
    assert calls == [("a", "next_run")]


@pytest.mark.parametrize("reason", ["reject", "class_c", "close"])
def test_ending_an_unapplied_run_touches_nothing_else(monkeypatch, reason):
    monkeypatch.setattr(A, "recreate_live", no_docker)
    monkeypatch.setattr(A, "destroy", no_docker)
    A.next_run("a")
    A.next_run("b")
    end = A.end_task("a", reason)
    assert A.RUNS["a"]["ended"] and not A.RUNS["b"].get("ended")
    assert end.reset_at is None and end.destroyed_at is None  # no reset happened, so none is claimed
    assert A.ledger("a")[-1]["reason"] == reason


@pytest.mark.parametrize("reason", ["undo", "idle_10min", "next_run", "apply_failed"])
def test_other_reasons_still_reset(monkeypatch, reason):
    monkeypatch.setattr(A, "_reset", lambda rid, r, everyone=False: "reset")
    assert A.end_task("a", reason) == "reset"


def test_reset_ends_the_applied_run_but_not_a_stranger(monkeypatch):
    monkeypatch.setattr(A, "recreate_live", lambda: SimpleNamespace(id="live-2"))
    monkeypatch.setattr(A, "checksum", lambda b: "s")
    monkeypatch.setattr(A, "seed_checksum", lambda refresh=False: "s")
    monkeypatch.setattr(A, "_live", None)
    applied("a")
    A.RUNS["b"] = {}
    A.end_task("a", "close")
    assert A.RUNS["a"]["ended"] and "snapshot" not in A.RUNS["a"] and not A.RUNS["b"].get("ended")


def test_refusal_names_no_run_id():
    applied("secret-run-id")
    r = A.approve("b", SQL, "x", None, "A", "ops")
    assert not r.executed and "secret-run-id" not in r.reason and "another visitor" in r.reason


# ---- S2 ----
def _effect(cents):
    return Effect({"invoices": {"count_delta": 0, "touched": 1}},
                  {"invoices": {"inserted": [], "updated": [1], "deleted": []}}, {}, [],
                  {"invoices.amount_cents": MoneyDelta(100, cents, cents - 100, True, 1, "x")})


@pytest.mark.parametrize("live_cents,verb,ok", [(50, "COMMIT", True), (77, "ROLLBACK", False)])
def test_s2_effect_match_compares_money(monkeypatch, live_cents, verb, ok):
    """Same rows, different money (random() in the SQL): the approver never saw that amount, so it rolls back."""
    sent = []

    def fake_run(box, sql, tag=False):
        sent.append(sql)
        return RunResult(True, None, False, stdout="{}")
    monkeypatch.setattr(runner, "run_sql", fake_run)
    monkeypatch.setattr(measure, "snapshot_hashes", lambda b: {})
    monkeypatch.setattr(measure, "parse_snapshot", lambda s: {})
    monkeypatch.setattr(measure, "diff", lambda a, b: _effect(live_cents))
    import hashlib
    r = runner.apply_sql(SimpleNamespace(id="live"), SQL, hashlib.sha256(SQL.encode()).hexdigest(), _effect(50))
    assert r.ok is ok and sent[-1].startswith(verb)


# ---- S4: psql error text on the apply path ----
def test_s4_apply_failure_reason_is_the_first_line_only(monkeypatch):
    monkeypatch.setattr(runner, "run_sql", lambda box, sql, tag=False: RunResult(
        False, "ERROR:  23514: bad row\nDETAIL:  Failing row contains (secret-row-value).", False, "23514"))
    monkeypatch.setattr(measure, "snapshot_hashes", lambda b: {})
    import hashlib
    r = runner.apply_sql(SimpleNamespace(id="live"), SQL, hashlib.sha256(SQL.encode()).hexdigest(), _effect(50))
    assert "secret-row-value" not in r.reason and "bad row" in r.reason


# ---- B11 ----
def test_b11_orphan_sweep_removes_run_containers_and_volumes(monkeypatch):
    calls = []

    def fake_docker(*args, input=None, timeout=60):
        calls.append(args)
        out = ""
        if args[0] == "ps":
            assert "label=drydock=run" in args
            out = "drydock-run-a\n"
        if args[:2] == ("volume", "ls"):
            assert "label=drydock=run" in args
            out = "drydock-run-a\ndrydock-run-b\n"
        code = 1 if "inspect" in args else 0
        return subprocess.CompletedProcess(args, code, out, "")
    monkeypatch.setattr(container, "docker", fake_docker)
    assert container.sweep_orphans() == ["drydock-run-a", "drydock-run-b"]
    removed = [a[-1] for a in calls if a[:2] == ("rm", "-f")]
    assert removed == ["drydock-run-a", "drydock-run-b"]
    assert not any("drydock=live" in a or "drydock=template" in a for a in calls)


# ---- S1's server-side half (the statements only run under Docker: tests/test_sandbox.py) ----
def test_s1_template_revokes():
    from sandbox.template import ROLES_SQL
    assert "REVOKE USAGE ON LANGUAGE plpgsql FROM PUBLIC" in ROLES_SQL
    assert "REVOKE TEMP ON DATABASE drydock FROM PUBLIC" in ROLES_SQL
    assert "lo_from_bytea(oid, bytea)" in ROLES_SQL


# ---- B3 bounded output ----
def test_output_over_the_limit_is_killed_and_reported(monkeypatch):
    import sys
    cmd = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 5000000); sys.stderr.write('e')"]
    p = runner._capture(cmd, timeout=30, limit=1000)
    assert p.returncode != 0 and len(p.stdout) <= 1000 and runner.TOO_MUCH in p.stderr


def test_capture_returns_normal_output_and_times_out():
    import sys
    p = runner._capture([sys.executable, "-c", "print('hello')"], timeout=30)
    assert p.returncode == 0 and p.stdout.strip() == "hello"
    with pytest.raises(subprocess.TimeoutExpired):
        runner._capture([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)


def test_run_sql_reports_too_much_output_and_kills_the_session(monkeypatch):
    calls = []

    def fake(cmd, timeout=None, limit=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "x", f"ERROR:  {runner.TOO_MUCH}") if len(calls) == 1 \
            else subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(runner, "_capture", fake)
    r = runner.run_sql(SimpleNamespace(id="b", sock_exec="e", sock_admin="a"), "SELECT repeat('x', 100000000)")
    assert not r.ok and r.code == "54000" and r.stdout == ""
    assert runner.KILL_SQL in calls[1]


def test_undo_on_a_never_applied_run_says_so():  # bugs.md F5
    A.RUNS["r"] = {}
    with pytest.raises(RuntimeError, match="nothing to undo: this run was never applied"):
        A.undo("r")


def test_undo_restores_then_a_failed_reset_still_answers(monkeypatch):  # bugs.md F6
    applied("r")
    A.RUNS["r"]["before"] = "c0"
    box = SimpleNamespace(id="live-1")
    monkeypatch.setattr(A, "live", lambda: box)
    monkeypatch.setattr(A, "restore_data", lambda b, dump: None)
    monkeypatch.setattr(A, "checksum", lambda b: "c0")

    def broken_reset(run_id, reason):
        raise RuntimeError("reset checksum differs from seed")
    monkeypatch.setattr(A, "end_task", broken_reset)
    r = A.undo("r")
    assert r.task_end is None and r.matches_before is False
    assert [x["step"] for x in A.LEDGER] == ["undo"]
