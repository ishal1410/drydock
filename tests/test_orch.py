"""Task 6: orchestrator API (plan Task 6 L663-671, D3, D4, D14, D15; pins in .superpowers/sdd/task5-6-interface-pins.md).

Docker-free: the sandbox (start/destroy/self_test), the agent seam and the live-db calls in orch/apply.py are
replaced with fakes. The real apply path is covered by tests/test_apply.py.
"""
import asyncio
import hashlib
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import orch.apply as A
import orch.runs as R
from core.contract import Failure, Verdict
from core.measure import Effect, MoneyDelta
from llm.client import PUBLIC, ConfigError, ModelRequestError, ModelTruncated, ModelUnavailable
from orch.app import app, lifespan
from sandbox.runner import CAPS

T0 = datetime(2026, 11, 5, tzinfo=timezone.utc)
SQL = "UPDATE invoices SET status = 'written_off' WHERE id IN (1, 2)"


def effect(rows=2, tables=("invoices",)):
    return Effect({t: {"count_delta": 0, "touched": rows} for t in tables},
                  {t: {"inserted": [], "updated": list(range(rows)), "deleted": []} for t in tables},
                  {}, [f"invoices.status: open→written_off ×{rows}"],
                  {"invoices.amount_cents": MoneyDelta(100, 90, -10, True, rows, "across the changed rows")},
                  {t: 300 for t in tables})


def trace(green=True, rows=2, revisions=0, sql=SQL, policy_failed_before=False):
    verdicts = [Verdict(False, [Failure("policy", "P-7: violations 0 → 3")])] if policy_failed_before else []
    v = Verdict(green, [] if green else [Failure("row_count", "invoices: contract 1-5 rows, measured 9")])
    steps = [{"n": 1, "name": "read", "payload": {"text": "write off"}, "model_id": "glm-5.3-flash", "tokens": 110,
              "tokens_in": 100, "tokens_out": 10, "ms": 900, "service": "api.groq.com, glm-5.3-flash"},
             {"n": 2, "name": "lookup", "payload": {"sql": "SELECT 1", "result": {"count": 2}, "elapsed_ms": 38}},
             {"n": 5, "name": "run", "payload": {"command_tag": f"UPDATE {rows}", "elapsed_ms": 61, "cap_ms": 5000,
                                                 "mem_peak_bytes": 7, "mem_cap_bytes": 9, "ok": True}},
             {"n": 6, "name": "compare", "payload": {"attempt": 1, "green": green, "effect": effect(rows)}}]
    return SimpleNamespace(steps=steps, verdict=v, verdicts=verdicts + [v], revisions=revisions,
                           effect=effect(rows), sql=sql,
                           explanation="wrote off 2 invoices", reply="Done: 2 invoices written off.")


class Fake:
    """Records sandbox / agent / live-db calls in order."""

    def __init__(self):
        self.calls, self.n, self.trace = [], 0, trace()

    def start(self):
        self.n += 1
        box = SimpleNamespace(id=f"drydock-run-{self.n}", created_at=T0)
        self.calls.append(("start", box.id))
        return box

    def destroy(self, box):
        self.calls.append(("destroy", box.id))
        return T0

    def self_test(self, box):
        self.calls.append(("self_test", box.id))
        return [SimpleNamespace(name="server-side COPY denied", ok=True, detail=box.id)]

    def agent(self, ticket, box):
        self.calls.append(("agent", ticket["body"], box.id))
        t = self.trace
        if isinstance(t, Exception):
            raise t
        return t


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = Fake()
    monkeypatch.setattr(R, "start_run_container", f.start)
    monkeypatch.setattr(R, "destroy", f.destroy)
    monkeypatch.setattr(R, "self_test", f.self_test)
    monkeypatch.setattr(R, "run_agent", f.agent)
    monkeypatch.setattr(R, "preflight", lambda: None)
    monkeypatch.setattr(R, "docker_up", lambda: True)
    monkeypatch.setattr(R, "spawn", lambda fn: fn())  # the dry run inline; test_c_* use the real thread
    monkeypatch.setattr(R, "measure_live", lambda: "live-sum")
    monkeypatch.setattr(R, "seed_sum", lambda: "seed-sum")
    monkeypatch.setattr(A, "live", lambda: SimpleNamespace(id="drydock-live-1"))
    monkeypatch.setattr(A, "_live", SimpleNamespace(id="drydock-live-1"))
    R.LIVE_SUM.clear()
    orig_next = A.next_run
    monkeypatch.setattr(A, "next_run", lambda rid: (f.calls.append(("next_run", rid)), orig_next(rid))[1])
    def end_task(rid, reason):  # like the real one: the run is ended (no Docker reset here)
        f.calls.append(("end_task", rid, reason))
        A.RUNS.setdefault(rid, {})["ended"] = True
        return SimpleNamespace(destroyed_at=T0, reset_at=T0, new_live_id="live-2", checksum_ok=True)
    monkeypatch.setattr(A, "end_task", end_task)
    monkeypatch.setattr(A, "RECEIPTS_DIR", tmp_path)
    for d in (A.RUNS, R.RUNS, R.HITS, R.GET_HITS):
        d.clear()
    A.LEDGER.clear()
    R.DAY.update(day=None, count=0)
    yield f
    for d in (A.RUNS, R.RUNS, R.HITS):
        d.clear()


@pytest.fixture
def client(fake):
    return TestClient(app, client=("127.0.0.1", 50000))  # the peer Caddy has on the VM (network_mode: host)


def post_run(client, ip="10.0.0.1", **body):
    return client.post("/api/runs", json=body or {"ticket_id": "E8"}, headers={"X-Forwarded-For": ip})


def make_run(client, fake, t=None, ip="10.0.0.1", **body):
    fake.trace = t or trace()
    r = post_run(client, ip=ip, **body)
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


def test_health(client):
    h = client.get("/api/health").json()
    assert h["ok"] is True and h["docker"] is True


def test_health_names_the_live_model_only_when_one_can_run(client, monkeypatch):
    """The landing header says what serves live runs. It may only name a model the app can actually call."""
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.vultrinference.com/v1")
    monkeypatch.setenv("DRYDOCK_MODEL", "m-1")
    assert client.get("/api/health").json()["model"] == "Vultr Serverless Inference, m-1"
    monkeypatch.setenv("LLM_BASE_URL", "https://other.example/v1")
    assert "Vultr" not in client.get("/api/health").json()["model"]
    monkeypatch.delenv("LLM_API_KEY")
    monkeypatch.delenv("VULTR_INFERENCE_API_KEY", raising=False)
    assert client.get("/api/health").json()["model"] is None


def test_run_isolation(client, fake):
    a = make_run(client, fake, ip="1.1.1.1")
    b = make_run(client, fake, t=trace(rows=4), text="Close the duplicate account for Okafor & Lund", ip="2.2.2.2")
    assert a != b
    ra, rb = client.get(f"/api/runs/{a}").json(), client.get(f"/api/runs/{b}").json()
    assert ra["ticket"]["id"] == "E8" and rb["ticket"]["id"] is None and rb["source_kind"] == "text"
    assert rb["ticket"]["body"].startswith("Close the duplicate")
    assert ra["strip"][0]["container_id"] == "drydock-run-1" and rb["strip"][0]["container_id"] == "drydock-run-2"
    assert ra["trace"]["effect"]["rows_by_table"]["invoices"]["touched"] == 2
    assert rb["trace"]["effect"]["rows_by_table"]["invoices"]["touched"] == 4
    assert ("destroy", "drydock-run-1") in fake.calls and ("destroy", "drydock-run-2") in fake.calls


def test_next_run_called_on_create(client, fake):
    rid = make_run(client, fake)
    names = [c[0] for c in fake.calls]
    assert names.index("next_run") < names.index("start") < names.index("agent")
    assert ("next_run", rid) in fake.calls


def test_rate_limit(client, fake):
    for _ in range(5):
        assert post_run(client, ip="9.9.9.9").status_code == 201
    r = post_run(client, ip="9.9.9.9")
    assert r.status_code == 429 and r.json()["card"] == "rate_limited"
    assert post_run(client, ip="8.8.8.8").status_code == 201  # per IP


def test_rate_limit_uses_proxy_added_ip(client, fake):
    """A client-sent X-Forwarded-For cannot dodge the limit: the last entry is the one Caddy added."""
    for i in range(5):
        assert post_run(client, ip=f"6.6.6.{i}, 7.7.7.7").status_code == 201
    assert post_run(client, ip="6.6.6.99, 7.7.7.7").status_code == 429


def test_xff_is_ignored_unless_the_peer_is_loopback(fake):
    """B1: port 8000 reachable from anywhere else must not let a client pick its own bucket."""
    direct = TestClient(app, client=("203.0.113.9", 50000))
    for i in range(5):
        assert post_run(direct, ip=f"6.6.6.{i}").status_code == 201
    assert post_run(direct, ip="6.6.6.99").status_code == 429


@pytest.mark.parametrize("xff, peer, want", [
    ("9.9.9.9, 1.2.3.4", "127.0.0.1", "1.2.3.4"),   # Caddy on loopback: the entry Caddy added
    ("1.2.3.4", "::1", "1.2.3.4"),
    ("1.2.3.4", "203.0.113.9", "203.0.113.9"),      # anyone else: the peer, XFF ignored
    (None, "203.0.113.9", "203.0.113.9"),
    (None, None, "unknown"),
    ("2001:db8:1:2:3:4:5:6", "127.0.0.1", "2001:db8:1:2::/64"),  # one IPv6 host owns a /64: rotating in it is one client
    ("not an ip", "127.0.0.1", "not an ip"),
])
def test_client_ip(xff, peer, want):
    assert R.client_ip(xff, peer) == want


def test_daily_cap(client, fake, monkeypatch):
    monkeypatch.setattr(R, "DAILY_CAP", 2)
    assert post_run(client, ip="1.0.0.1").status_code == 201
    assert post_run(client, ip="1.0.0.2").status_code == 201
    r = post_run(client, ip="1.0.0.3")
    assert r.status_code == 429 and r.json()["card"] == "daily_cap"


def test_one_concurrent_sandbox(client, fake):
    assert R.BUSY.acquire(blocking=False)
    try:
        r = post_run(client)
        assert r.status_code == 503 and r.json()["card"] == "busy"
        assert "start" not in [c[0] for c in fake.calls]
    finally:
        R.BUSY.release()
    assert post_run(client).status_code == 201


def test_selftest_runs_every_run(client, fake):
    a, b = make_run(client, fake), make_run(client, fake)
    assert [c for c in fake.calls if c[0] == "self_test"] == [("self_test", "drydock-run-1"),
                                                             ("self_test", "drydock-run-2")]
    assert client.get(f"/api/runs/{b}").json()["chips"][0]["detail"] == "drydock-run-2"
    assert client.get(f"/api/runs/{a}").json()["chips"][0]["ok"] is True


@pytest.mark.parametrize("err", [ModelUnavailable(detail="HTTP 503: secret upstream body"),
                                 ModelRequestError(detail="HTTP 400: secret upstream body"),
                                 ModelTruncated(SimpleNamespace(tokens_out=2048))])
@pytest.mark.parametrize("body", [{"ticket_id": "E8"}, {"text": "Refund the duplicate charge on INV-10101"}])
def test_model_down_shows_unavailable(client, fake, err, body):
    fake.trace = err
    rid = post_run(client, **body).json()["run_id"]
    r = client.get(f"/api/runs/{rid}")
    assert r.json()["status"] == "unavailable" and r.json()["card"] == "model_unavailable"
    assert r.json()["message"] == PUBLIC
    assert "secret" not in r.text and "trace" not in r.json()  # nothing to render, nothing to approve
    assert fake.calls[-1][0] == "destroy"                  # sandbox still destroyed
    r = client.post(f"/api/runs/{rid}/approve", json={"role": "engineer"})
    assert r.status_code == 409 and r.json()["card"] == "not_ready"


def test_seam_today_is_model_unavailable(client, fake, monkeypatch):
    """Task 5 not built (or no key): the card, before any container starts or any open task ends. No cache, no replay."""
    monkeypatch.setattr(R, "preflight", R.__dict__["_preflight"])
    monkeypatch.delenv("VULTR_INFERENCE_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    r = post_run(client)
    assert r.status_code == 503 and r.json() == {"card": "model_unavailable", "message": PUBLIC}
    assert fake.calls == [] and R.RUNS == {}


def test_approve_role_matches_class(client, fake, monkeypatch):
    applied = []

    def approve(run_id, sql, sha, eff, cls, role):
        applied.append((run_id, sql, sha, eff, cls, role))
        r = SimpleNamespace(ok=True, executed=True, reason="applied", effect=eff, rolled_back=False,
                            started_at=T0, ended_at=T0, code=None)
        A._write_receipt(run_id, SimpleNamespace(id="drydock-live-1"), sql, r, cls, role, {"t": "a"}, {"t": "b"})
        return r

    monkeypatch.setattr(A, "approve", approve)
    a = make_run(client, fake)                                       # one table, 2 rows, ticket: class A
    assert client.get(f"/api/runs/{a}").json()["approval_class"]["class"] == "A"
    assert client.post(f"/api/runs/{a}/approve", json={"role": "ops"}).status_code == 200
    b = make_run(client, fake, t=trace(revisions=1, policy_failed_before=True))  # class B
    assert client.get(f"/api/runs/{b}").json()["approval_class"]["class"] == "B"
    r = client.post(f"/api/runs/{b}/approve", json={"role": "ops"})
    assert r.status_code == 403 and r.json()["card"] == "role_not_allowed"
    assert client.post(f"/api/runs/{b}/approve", json={"role": "engineer"}).status_code == 200
    c = make_run(client, fake, t=trace(green=False))                 # class C
    for role in ("ops", "engineer"):
        assert client.post(f"/api/runs/{c}/approve", json={"role": role}).status_code == 409
    n = make_run(client, fake, t=trace(rows=0))                      # no-op
    assert client.post(f"/api/runs/{n}/approve", json={"role": "engineer"}).status_code == 409
    # sha and effect are the server's, from the dry run (D1a guards), never from the request
    assert [x[0] for x in applied] == [a, b]
    _, sql, sha, eff, cls, role = applied[1]
    assert (sql, sha, cls, role) == (SQL, hashlib.sha256(SQL.encode()).hexdigest(), "B", "engineer")
    assert eff.rows_by_table == effect(2).rows_by_table


def test_approve_body_cannot_carry_sql_or_sha(client, fake):
    rid = make_run(client, fake)
    r = client.post(f"/api/runs/{rid}/approve", json={"role": "ops", "sql": "DELETE FROM invoices",
                                                      "approved_sha": "x"})
    assert r.status_code == 422 and r.json()["card"] == "invalid"
    assert client.post(f"/api/runs/{rid}/approve", json={"role": "admin"}).status_code == 422


def test_refused_apply_is_409_with_reason(client, fake, monkeypatch):
    rid = make_run(client, fake)
    monkeypatch.setattr(A, "approve", lambda *a: SimpleNamespace(
        ok=False, executed=False, reason="refused: task ended, environment reset to seed; start a new run",
        effect=None, rolled_back=False, started_at=T0, ended_at=T0, code=None))
    r = client.post(f"/api/runs/{rid}/approve", json={"role": "ops"})
    assert r.status_code == 409 and r.json()["message"].startswith("refused: task ended")


def test_failed_apply_shows_reason_as_is(client, fake, monkeypatch):
    """pin 3: a rolled-back apply returns its ApplyResult; r.reason is shown unchanged."""
    rid = make_run(client, fake)
    monkeypatch.setattr(A, "approve", lambda *a: SimpleNamespace(
        ok=False, executed=True, reason="stopped by disk spill cap", effect=None, rolled_back=True,
        started_at=T0, ended_at=T0, code="53400"))
    r = client.post(f"/api/runs/{rid}/approve", json={"role": "ops"})
    # non-2xx: the UI renders any 2xx as "applied" (Run.jsx go()); the reason is shown unchanged
    assert r.status_code == 409 and r.json() == {"card": "apply_failed", "message": "stopped by disk spill cap"}


def test_approve_response_matches_ui(client, fake, monkeypatch):
    """ui-api-contract: {apply{...}, live_checksum_before/after, receipt}, built from the real receipt writer."""
    def approve(run_id, sql, sha, eff, cls, role):
        r = SimpleNamespace(ok=True, executed=True, reason="applied", effect=eff, rolled_back=False,
                            started_at=T0, ended_at=T0, code=None)
        A._write_receipt(run_id, SimpleNamespace(id="drydock-live-1"), sql, r, cls, role, {"t": "a"}, {"t": "b"})
        return r
    monkeypatch.setattr(A, "approve", approve)
    rid = make_run(client, fake)
    b = client.post(f"/api/runs/{rid}/approve", json={"role": "ops"}).json()
    sha = hashlib.sha256(SQL.encode()).hexdigest()
    a = b["apply"]
    assert a["role"] == "executor" and a["container_id"] == "drydock-live-1" and a["effect_match"] is True
    assert a["sql_sha256"] == a["approved_sha256"] == sha
    assert a["caps"] == {"statement_timeout": CAPS["statement_timeout"], "temp_file_limit": CAPS["temp_file_limit"],
                         "memory": "512m", "network": "none"}
    assert a["rows_by_table"]["invoices"]["touched"] == 2
    assert b["live_checksum_before"] != b["live_checksum_after"]
    assert b["receipt"]["approval_class"] == "A" and b["receipt"]["approver_role_asserted"] == "ops"
    assert b["receipt"]["receipt_sha256"]


def test_view_matches_ui_contract(client, fake):
    b = client.get(f"/api/runs/{make_run(client, fake)}").json()
    assert {"run_id", "ticket", "source_kind", "status", "verdict", "approval_class", "chips", "strip", "destroyed_at",
            "ledger", "explanation", "reply", "live_checksum", "stopped", "trace"} <= set(b)
    assert b["status"] == "done" and b["task_state"] == "ready" and b["stopped"] is None
    assert b["live_checksum"] == "live-sum"
    assert set(b["approval_class"]) == {"class", "reason"} and b["trace"]["revisions"] == 0
    assert [(r["step"], r["what"], r["role"], r["elapsed_ms"]) for r in b["strip"]] == [
        (2, "lookup", "executor", 38), (5, "sql", "executor", 61)]
    read, compare = b["trace"]["steps"][0], b["trace"]["steps"][-1]
    assert read["tokens"] == {"in": 100, "out": 10}
    assert compare["payload"]["effect"]["money"][0]["key"] == "invoices.amount_cents"
    assert compare["payload"]["effect"]["money"][0]["delta_cents"] == -10
    ledger = {x["service"]: x["detail"] for x in b["ledger"]}
    assert "Vultr Serverless Inference" not in ledger  # B2: the real provider, from the step that really ran
    assert "glm-5.3-flash" in ledger["Model provider: api.groq.com"] and "100" in ledger["Model provider: api.groq.com"]
    assert "drydock-run-1" in ledger["Sandbox container"] and "untouched" in ledger["Live-db container"]
    assert "Vultr Cloud Compute VM" not in ledger  # only claimed when deployed on one


def test_ledger_names_vm_only_when_deployed(client, fake, monkeypatch):
    monkeypatch.setenv("DRYDOCK_VM_ID", "vm-123")
    rid = make_run(client, fake)
    ledger = {x["service"]: x["detail"] for x in client.get(f"/api/runs/{rid}").json()["ledger"]}
    assert ledger["Vultr Cloud Compute VM"].startswith("vm-123")


def test_stopped_comes_from_the_measured_code(client, fake):
    """E11 shape: the card only from a real SQLSTATE; the label is derived from it, checksums are measured."""
    t = trace(green=False)
    t.steps[2] = {"n": 5, "name": "run", "started_at": T0.isoformat(),
                  "payload": {"code": "53400", "elapsed_ms": 1043, "stopped_at": T0.isoformat(), "ok": False}}
    b = client.get(f"/api/runs/{make_run(client, fake, t=t)}").json()
    s = b["stopped"]
    assert s["cap_label"] == "disk spill cap" and s["code"] == "53400" and s["elapsed_ms"] == 1043
    assert s["container_id"] == "drydock-run-1" and s["live_checksum_before"] == s["live_checksum_after"] == "live-sum"
    assert s["rows_label"] is None   # needs Task 8; never invented
    assert b["strip"][1]["stopped_by"] == "temp_file_limit" and b["strip"][1]["code"] == "53400"


def test_class_c_ends_task(client, fake):
    rid = make_run(client, fake, t=trace(green=False))
    assert ("end_task", rid, "class_c") in fake.calls
    body = client.get(f"/api/runs/{rid}").json()
    assert body["approval_class"]["class"] == "C" and body["approval_class"]["reason"].startswith("red verdict")


def test_two_statement_sql_is_class_c(client, fake):
    """judge precondition 3: SQL apply would refuse (one_statement) never reaches Approve."""
    rid = make_run(client, fake, t=trace(sql=SQL + "; DELETE FROM payments"))
    body = client.get(f"/api/runs/{rid}").json()
    assert body["approval_class"]["class"] == "C" and "one statement" in body["approval_class"]["reason"]
    assert ("end_task", rid, "class_c") in fake.calls


def test_reject_ends_task(client, fake):
    rid = make_run(client, fake)
    r = client.post(f"/api/runs/{rid}/close", json={"reason": "reject"})
    assert r.status_code == 200 and r.json()["new_live_id"] == "live-2" and r.json()["checksum_ok"] is True
    assert ("end_task", rid, "reject") in fake.calls
    assert client.get(f"/api/runs/{rid}").json()["task_state"] == "ended"
    assert client.post(f"/api/runs/{rid}/close", json={"reason": "bogus"}).status_code == 422


def test_close_twice_resets_once_and_reports_the_last_reset(client, fake):
    rid = make_run(client, fake)
    A.LEDGER.append({"run_id": rid, "step": "task_end", "destroyed_at": T0, "reset_at": T0, "new_live_id": "live-9",
                     "checksum_ok": True})
    A.RUNS[rid]["ended"] = True
    r = client.post(f"/api/runs/{rid}/close")
    assert r.json()["new_live_id"] == "live-9"
    assert not [c for c in fake.calls if c[0] == "end_task"]


def test_close_undo_reset(client, fake, monkeypatch):
    rid = make_run(client, fake)
    assert client.post(f"/api/runs/{rid}/close").status_code == 200
    assert ("end_task", rid, "close") in fake.calls

    def no_undo(run_id):
        raise RuntimeError("nothing to undo: task closed, environment reset to seed")
    monkeypatch.setattr(A, "undo", no_undo)
    r = client.post(f"/api/runs/{rid}/undo")
    assert r.status_code == 409 and r.json()["message"].startswith("nothing to undo")
    monkeypatch.setattr(A, "undo", lambda run_id: SimpleNamespace(checksum="abc", matches_before=True,
                                                                 task_end={"reset_at": T0}))
    assert client.post(f"/api/runs/{rid}/undo").json()["matches_before"] is True


def test_error_states(client, fake):
    for body in ({"text": ""}, {"text": "   "}, {"text": "x" * 2001}, {"text": "a\x00b"}, {},
                 {"ticket_id": "E8", "text": "both"}, {"ticket_id": "E99"}, {"ticket_id": "E8", "extra": 1}):
        r = client.post("/api/runs", json=body, headers={"X-Forwarded-For": f"5.5.5.{len(str(body))}"})
        assert r.status_code == 422, body
        assert r.json()["card"] == "invalid"
    assert post_run(client, ip="5.5.6.1", text="x" * 2000).status_code == 201
    for bad in ("..%2Fx", "a" * 65, "not%20an%20id"):
        assert client.get(f"/api/runs/{bad}").status_code == 404
    assert client.get("/api/runs/" + "b" * 32).json()["card"] == "invalid"
    assert R.RUNS.keys() == {next(iter(R.RUNS))}  # only the one valid run


def test_internal_error_hides_details(fake):
    fake.trace = RuntimeError("boom /secret/path line 42")
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/runs/" + c.post("/api/runs", json={"ticket_id": "E1"}).json()["run_id"])
    assert r.status_code == 200 and r.json()["status"] == "unavailable" and r.json()["card"] == "error"
    assert "secret" not in r.text and "Traceback" not in r.text
    assert fake.calls[-1][0] == "destroy" and not R.BUSY.locked()


def test_orch_has_no_db_dsn():
    dsn = re.compile(r"postgres(ql)?://|\bdbname=|\bhost=\S+\s+\S*port=", re.I)
    assert not [k for k, v in os.environ.items() if dsn.search(v or "")]
    for name, mod in list(sys.modules.items()):
        if name.startswith("orch"):
            assert not [k for k, v in vars(mod).items() if isinstance(v, str) and dsn.search(v)], name
    env_example = open(os.path.join(os.path.dirname(__file__), "..", ".env.example"), encoding="utf-8").read()
    assert not dsn.search(env_example) and "DATABASE_URL" not in env_example


def test_reaper_calls_reap_idle(monkeypatch):
    import threading
    seen = threading.Event()
    monkeypatch.setattr(A, "warm", lambda: None)
    monkeypatch.setattr(A, "reap_idle", lambda: seen.set() or [])
    stop = threading.Event()
    t = threading.Thread(target=R.reaper, args=(stop, 0.01), daemon=True)
    t.start()
    assert seen.wait(2)
    stop.set()
    t.join(2)


def test_receipt_download(client, fake, tmp_path):
    rid = make_run(client, fake)
    assert client.get(f"/api/runs/{rid}/receipt").status_code == 404
    (tmp_path / f"{rid}.json").write_text('{"run_id": "%s", "receipt_sha256": "x"}' % rid, encoding="utf-8")
    r = client.get(f"/api/runs/{rid}/receipt")
    assert r.status_code == 200 and r.json()["run_id"] == rid
    assert "attachment" in r.headers["content-disposition"]


def test_tickets_list(client):
    b = client.get("/api/tickets").json()
    assert [t["id"] for t in b["tickets"]] == ["E1", "E2", "E5", "E6", "E8", "E11"]
    assert b["hidden"] == [] and b["live_checksum"] == "live-sum" and b["seed_checksum"] == "seed-sum"


def test_tickets_checksums_null_when_docker_down(fake, monkeypatch):
    def down():
        raise RuntimeError("docker down")
    monkeypatch.setattr(R, "measure_live", down)
    monkeypatch.setattr(R, "seed_sum", down)
    b = TestClient(app).get("/api/tickets").json()
    assert b["live_checksum"] is None and b["seed_checksum"] is None and len(b["tickets"]) == 6


def test_live_checksum_cached_until_the_ledger_moves(client, fake, monkeypatch):
    n = []
    monkeypatch.setattr(R, "measure_live", lambda: n.append(1) or "s")
    client.get("/api/tickets"), client.get("/api/tickets")
    assert len(n) == 1
    A.LEDGER.append({"run_id": "x", "step": "apply"})
    client.get("/api/tickets")
    assert len(n) == 2


def test_eval_reads_newest_results_file(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "EVAL_DIR", tmp_path)
    assert R.eval_score() is None
    (tmp_path / "results-2026-11-05.md").write_text("- **14/18 correct vs hand-written goldens** (demo)",
                                                    encoding="utf-8")
    assert R.eval_score() == {"correct": 14, "total": 18, "scope": None, "file": "results-2026-11-05.md"}
    (tmp_path / "results-2026-11-05b.md").write_text("- **2/2 correct vs hand-written goldens** (reduced: E8, E11 x1)",
                                                     encoding="utf-8")
    assert R.eval_score()["scope"] == "E8, E11 x1"
    (tmp_path / "results-2026-11-06.md").write_text("INCOMPLETE: model unavailable", encoding="utf-8")
    assert R.eval_score() is None  # newest is partial: no score shown (N5)


def test_security_headers(client):
    h = client.get("/api/health").headers
    assert h["x-content-type-options"] == "nosniff" and h["cache-control"] == "no-store"


# ---- audit-1107 logic ----
def test_b3_green_run_shows_no_stopped_card_from_an_old_step(client, fake):
    t = trace()
    t.steps[1]["payload"]["code"] = "53400"  # a lookup hit the cap, but nothing in this trace says so
    t.capped = None
    assert client.get(f"/api/runs/{make_run(client, fake, t=t)}").json()["stopped"] is None


def test_b3_capped_green_run_is_locked_and_says_why(client, fake):
    t = trace()
    t.capped = "disk spill cap"
    t.steps[1]["payload"].update(code="53400", container_id="drydock-run-1")
    rid = make_run(client, fake, t=t)
    b = client.get(f"/api/runs/{rid}").json()
    assert b["approval_class"]["class"] == "C" and "disk spill cap" in b["approval_class"]["reason"]
    assert b["stopped"]["cap_label"] == "disk spill cap"
    assert client.post(f"/api/runs/{rid}/approve", json={"role": "engineer"}).status_code == 409


def test_b9_strip_and_stopped_name_the_box_that_ran(client, fake):
    t = trace(green=False)
    stamp = datetime(2026, 11, 7, tzinfo=timezone.utc)
    t.boxes = [{"id": "drydock-run-fresh", "created_at": T0, "destroyed_at": stamp}]
    t.steps[2] = {"n": 5, "name": "run", "payload": {"code": "53400", "elapsed_ms": 9, "container_id": "drydock-run-fresh"}}
    b = client.get(f"/api/runs/{make_run(client, fake, t=t)}").json()
    assert b["strip"][1]["container_id"] == "drydock-run-fresh"
    assert b["stopped"]["container_id"] == "drydock-run-fresh" and b["stopped"]["destroyed_at"].startswith("2026-11-07")
    ledger = {x["service"]: x["detail"] for x in b["ledger"]}
    assert "drydock-run-1" in ledger["Sandbox container"] and "drydock-run-fresh" in ledger["Sandbox container"]
    assert sum(r["step"] == "dry_run" for r in b["audit"]) == 2


def test_b22_failed_destroy_keeps_the_model_card(client, fake, monkeypatch):
    def boom(box):
        raise RuntimeError("docker gone")
    monkeypatch.setattr(R, "destroy", boom)
    fake.trace = ModelUnavailable(detail="HTTP 503: x")
    r = client.get("/api/runs/" + post_run(client).json()["run_id"])
    assert r.json()["status"] == "unavailable" and r.json()["card"] == "model_unavailable"


def test_b22_failed_destroy_is_not_claimed_on_a_finished_run(client, fake, monkeypatch):
    def boom(box):
        raise RuntimeError("docker gone")
    monkeypatch.setattr(R, "destroy", boom)
    b = client.get(f"/api/runs/{make_run(client, fake)}").json()
    assert b["destroyed_at"] is None
    assert "not confirmed" in {x["service"]: x["detail"] for x in b["ledger"]}["Sandbox container"]


def test_b11_reaper_sweeps_orphans_before_warming(monkeypatch):
    import threading
    order = []
    monkeypatch.setattr(R, "sweep_orphans", lambda: order.append("sweep") or [])
    monkeypatch.setattr(A, "warm", lambda: order.append("warm"))
    monkeypatch.setattr(A, "reap_idle", lambda: [])
    stop = threading.Event()
    stop.set()
    R.reaper(stop, 0.01)
    assert order == ["sweep", "warm"]


def test_b11_sweep_failure_does_not_stop_warm(monkeypatch):
    import threading
    order = []

    def down():
        raise OSError("docker not found")
    monkeypatch.setattr(R, "sweep_orphans", down)
    monkeypatch.setattr(A, "warm", lambda: order.append("warm"))
    stop = threading.Event()
    stop.set()
    R.reaper(stop, 0.01)
    assert order == ["warm"]


def test_close_after_end_reports_this_runs_row_not_anothers(client, fake):
    rid = make_run(client, fake)
    A.RUNS[rid]["ended"] = True
    A.LEDGER.append({"run_id": "someone-else", "step": "task_end", "new_live_id": "theirs"})
    assert client.post(f"/api/runs/{rid}/close").json()["new_live_id"] is None


def test_b2_one_ip_cannot_spend_the_day(client, fake, monkeypatch):
    monkeypatch.setattr(R, "IP_DAILY_RUNS", 2)
    monkeypatch.setattr(R, "RATE_MAX", 99)
    assert post_run(client, ip="1.0.0.1").status_code == 201
    assert post_run(client, ip="1.0.0.1").status_code == 201
    r = post_run(client, ip="1.0.0.1")
    assert r.status_code == 429 and r.json()["card"] == "rate_limited"
    assert post_run(client, ip="1.0.0.2").status_code == 201  # everyone else still gets in


def test_b2_free_text_has_a_lower_per_ip_cap(client, fake, monkeypatch):
    monkeypatch.setattr(R, "IP_DAILY_TEXT_RUNS", 1)
    assert post_run(client, text="Refund the duplicate charge").status_code == 201
    r = post_run(client, text="Refund the duplicate charge")
    assert r.status_code == 429 and r.json()["card"] == "rate_limited"
    assert post_run(client).status_code == 201  # listed tickets are still open to this IP


def test_b2_daily_token_budget_is_enforced(client, fake, monkeypatch):
    monkeypatch.setattr(R, "DAILY_TOKENS", 1000)
    monkeypatch.setattr(R, "tokens_today", lambda: 1000)
    r = post_run(client)
    assert r.status_code == 429 and r.json()["card"] == "daily_cap"
    assert "start" not in [c[0] for c in fake.calls]  # refused before a container or a model call


def test_b2_free_text_stops_first_so_the_listed_tickets_keep_working(client, fake, monkeypatch):
    monkeypatch.setattr(R, "DAILY_TOKENS", 1000)
    monkeypatch.setattr(R, "TEXT_TOKEN_PCT", 50)
    monkeypatch.setattr(R, "tokens_today", lambda: 500)
    r = post_run(client, text="Refund the duplicate charge")
    assert r.status_code == 429 and r.json()["card"] == "daily_cap"
    assert post_run(client).status_code == 201


def test_b2_usage_survives_a_restart(client, fake, monkeypatch):
    client_tokens = {"n": 777}
    monkeypatch.setattr(R, "tokens_today", lambda: client_tokens["n"])
    assert post_run(client, ip="1.0.0.1").status_code == 201
    restored = []
    monkeypatch.setattr(R, "restore_tokens_today", restored.append)
    R.DAY.update(day=None, count=0, ips={})
    R.load_usage()
    assert R.DAY["count"] == 1 and R.DAY["ips"]["1.0.0.1"] == [1, 0] and restored == [777]


def test_b2_bad_usage_file_starts_from_zero(fake):
    R._usage_file().parent.mkdir(parents=True, exist_ok=True)
    R._usage_file().write_text("not json", encoding="utf-8")
    R.DAY.update(day=None, count=0, ips={})
    R.load_usage()
    assert R.DAY["count"] == 0


def test_b2_blank_env_uses_the_default(monkeypatch):
    from llm.client import env_int
    monkeypatch.setenv("DRYDOCK_IP_DAILY_RUNS", "")
    assert env_int("DRYDOCK_IP_DAILY_RUNS", 10) == 10


def test_b5_demo_reset_route_is_gone(client):
    assert client.post("/api/demo/reset").status_code in (404, 405)  # falls through to the static page, which is GET only
    assert not [r for r in app.routes if "reset" in getattr(r, "path", "")]


def test_b5_big_body_is_refused_by_content_length(client, fake):
    r = client.post("/api/runs", content=b"x" * 20_000, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["card"] == "invalid"
    assert fake.calls == []


def test_b5_big_chunked_body_is_refused_while_reading(client, fake):
    def chunks():
        for _ in range(40):
            yield b"x" * 1024
    r = client.post("/api/runs", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["card"] == "invalid"
    assert fake.calls == []


def test_b5_longest_allowed_text_still_fits(client, fake):
    """2,000 non-ASCII chars escaped by JSON.stringify's worst case must stay under the body limit."""
    r = client.post("/api/runs", content='{"text": "' + "\u00e9" * R.MAX_TEXT + '"}',
                    headers={"Content-Type": "application/json", "X-Forwarded-For": "1.0.0.1"})
    assert r.status_code == 201


def test_b3_old_runs_are_evicted_with_their_apply_state(client, fake, monkeypatch):
    monkeypatch.setattr(R, "MAX_RUNS", 2)
    monkeypatch.setattr(R, "RATE_MAX", 99)
    monkeypatch.setattr(R, "IP_DAILY_RUNS", 99)
    a, b, c = make_run(client, fake), make_run(client, fake), make_run(client, fake)
    A.RUNS.setdefault(a, {})
    assert a not in R.RUNS and b in R.RUNS and c in R.RUNS
    assert client.get(f"/api/runs/{a}").status_code == 404
    assert not [row for row in A.LEDGER if row["run_id"] == a]


def test_b3_eviction_takes_ended_runs_first_and_never_an_applied_one(client, fake, monkeypatch):
    monkeypatch.setattr(R, "MAX_RUNS", 2)
    monkeypatch.setattr(R, "RATE_MAX", 99)
    monkeypatch.setattr(R, "IP_DAILY_RUNS", 99)
    applied, ended = make_run(client, fake), make_run(client, fake)
    A.RUNS[applied] = {"applied_at": 1.0}
    A.RUNS[ended] = {"ended": True}
    monkeypatch.setattr(A, "next_run", lambda rid: None)  # the real one would end the applied run first
    new = make_run(client, fake)
    assert applied in R.RUNS and new in R.RUNS and ended not in R.RUNS


def test_b3_eviction_never_leaves_a_stale_live_checksum(client, fake, monkeypatch):
    """One ledger row in, one out: the length alone would not move, and the old checksum would be served."""
    monkeypatch.setattr(R, "MAX_RUNS", 1)
    monkeypatch.setattr(R, "RATE_MAX", 99)
    monkeypatch.setattr(R, "IP_DAILY_RUNS", 99)
    make_run(client, fake)
    first = R.ledger_mark()
    make_run(client, fake)
    assert R.ledger_mark() != first


def test_b13_env_file_reaches_values_read_at_import(tmp_path):
    env_file = tmp_path / "custom.env"
    env_file.write_text("DRYDOCK_RECEIPTS=/data/receipts\nDRYDOCK_DAILY_RUN_CAP=7\nDRYDOCK_IP_DAILY_RUNS=\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DRYDOCK_", "LLM_", "VULTR_"))}
    code = "import orch.app, orch.apply as A, orch.runs as R; print(A.RECEIPTS_DIR.as_posix(), R.DAILY_CAP, R.IP_DAILY_RUNS)"
    p = subprocess.run([sys.executable, "-c", code], env={**env, "DRYDOCK_ENV_FILE": str(env_file)}, cwd=R.ROOT,
                       capture_output=True, text=True, timeout=60)
    assert p.stdout.split() == ["/data/receipts", "7", "10"], p.stderr


def test_b13_key_without_base_url_refuses_to_start(monkeypatch, tmp_path):
    monkeypatch.setenv("DRYDOCK_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("VULTR_INFERENCE_BASE_URL", raising=False)
    started = []
    monkeypatch.setattr(R, "reaper", lambda stop: started.append(1))

    async def boot():
        async with lifespan(app):
            pass
    with pytest.raises(ConfigError, match="LLM_BASE_URL"):
        asyncio.run(boot())
    assert not started


def _wait_until(check, seconds=5):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_c_post_returns_before_the_run_ends_and_the_ui_polls(client, fake, monkeypatch):
    monkeypatch.setattr(R, "spawn", R.__dict__["_spawn"])
    release = threading.Event()
    agent = fake.agent
    monkeypatch.setattr(R, "run_agent", lambda t, b: (release.wait(5), agent(t, b))[1])

    rid = post_run(client).json()["run_id"]                # back while the agent is still blocked

    r = client.get(f"/api/runs/{rid}").json()
    assert r["status"] == "running" and r["ticket"]["id"] == "E8" and r["task_state"] == "running"
    assert post_run(client).json()["card"] == "busy"       # still one sandbox at a time
    for path, body in (("approve", {"role": "engineer"}), ("undo", None), ("close", None)):
        r = client.post(f"/api/runs/{rid}/{path}", json=body)
        assert r.status_code == 409 and r.json()["card"] == "not_ready", path

    release.set()
    _wait_until(lambda: client.get(f"/api/runs/{rid}").json()["status"] == "done")
    assert client.get(f"/api/runs/{rid}").json()["verdict"]["green"] is True
    _wait_until(lambda: not R.BUSY.locked())               # freed once the worker is done
    assert post_run(client).status_code == 201


def test_c_done_is_only_seen_once_the_whole_view_is_ready(client, fake, monkeypatch):
    """Class C ends the task after the fields land: a poll must never read done with the task still open."""
    seen = []
    orig = R._end
    monkeypatch.setattr(R, "_end", lambda rid, why: (seen.append(R.RUNS[rid]["status"]), orig(rid, why))[1])
    make_run(client, fake, t=trace(green=False))
    assert seen == ["running"]


def test_c_crash_in_the_worker_is_a_card_and_frees_the_lock(client, fake, monkeypatch):
    fake.trace = RuntimeError("secret internal detail")
    rid = post_run(client).json()["run_id"]
    r = client.get(f"/api/runs/{rid}")
    assert r.json()["status"] == "unavailable" and r.json()["card"] == "error"
    assert "secret" not in r.text
    assert not R.BUSY.locked()


def test_c_a_run_that_spent_no_tokens_gives_its_count_back(client, fake, monkeypatch):
    """audit B23: an outage must not eat a visitor's daily runs."""
    fake.trace = ModelUnavailable(detail="HTTP 503")
    post_run(client, ip="1.0.0.1", text="Refund the duplicate charge")
    assert R.DAY["count"] == 0 and R.DAY["ips"]["1.0.0.1"] == [0, 0]


def test_c_a_failed_run_that_spent_tokens_still_counts(client, fake, monkeypatch):
    spent = {"n": 0}
    monkeypatch.setattr(R, "tokens_today", lambda: spent["n"])
    def agent(_t, _b):
        spent["n"] = 500
        raise ModelUnavailable(detail="HTTP 429")
    monkeypatch.setattr(R, "run_agent", agent)
    post_run(client, ip="1.0.0.1")
    assert R.DAY["count"] == 1


@pytest.fixture
def dist(tmp_path, monkeypatch):
    """A built web/dist stand-in on the app's static mount."""
    (tmp_path / "index.html").write_text("<div id=root></div>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "main-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path.parent / "secret.txt").write_text("nope", encoding="utf-8")
    ui = next(r.app for r in app.routes if getattr(r, "name", "") == "ui")
    monkeypatch.setattr(ui, "all_directories", [tmp_path])
    return tmp_path


@pytest.mark.parametrize("url", ["/", "/?app", "/?fixture", "/?app&run=abc", "/?fixture#dark"])
def test_d_every_screen_is_the_built_page(client, dist, url):
    r = client.get(url)
    assert r.status_code == 200 and "id=root" in r.text and r.headers["content-type"].startswith("text/html")


def test_d_api_routes_win_over_the_page(client, dist):
    assert client.get("/api/health").json()["docker"] is True
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json()["card"] == "invalid"  # JSON, never index.html: api.js would read HTML as an outage


def test_d_assets_cache_for_good_and_the_page_never(client, dist):
    assert "immutable" in client.get("/assets/main-abc123.js").headers["cache-control"]
    assert client.get("/").headers["cache-control"] == "no-store"
    assert client.get("/api/health").headers["cache-control"] == "no-store"


def test_d_static_mount_stays_inside_dist(client, dist):
    for url in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
        assert "nope" not in client.get(url).text


def test_d_no_dist_is_a_clean_404(client):
    r = client.get("/")
    assert r.status_code == 404 or "root" in r.text  # a laptop checkout may or may not have built web/dist


def test_d_csp_allows_only_what_the_page_loads(client):
    csp = client.get("/api/health").headers["content-security-policy"]
    assert "script-src 'self' https://cdnjs.cloudflare.com" in csp and "unsafe-eval" not in csp
    assert "frame-ancestors 'none'" in csp


def test_d_page_has_no_inline_script_the_csp_would_block():
    html = (R.ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and "onload=" not in html


def test_s5_get_tickets_never_starts_a_container_when_cold(client, fake, monkeypatch):
    monkeypatch.setattr(R, "seed_sum", R.__dict__["_cached_seed"])
    monkeypatch.setattr(A, "_seed_checksum", None)
    monkeypatch.setattr(A, "_live", None)
    def boom(*_a):
        raise AssertionError("a public GET started a container")
    monkeypatch.setattr(A, "seed_checksum", boom)
    monkeypatch.setattr(A, "live", boom)
    monkeypatch.setattr(A, "start_run_container", boom)
    r = client.get("/api/tickets").json()
    assert r["seed_checksum"] is None and r["live_checksum"] is None and r["tickets"]


def test_s5_get_tickets_does_not_wait_on_a_reset(client, fake, monkeypatch):
    """A reset holds LOCK for seconds. GETs must not queue behind it and fill the thread pool."""
    R.LIVE_SUM.update(key=("old", 0), value="last-known")
    done, hold = threading.Event(), threading.Event()
    def holder():
        with A.LOCK:
            hold.set()
            done.wait(5)
    threading.Thread(target=holder, daemon=True).start()
    hold.wait(5)
    try:
        assert client.get("/api/tickets").json()["live_checksum"] == "last-known"
    finally:
        done.set()


def test_s5_polling_fits_the_get_limit_and_a_flood_does_not(client, fake, monkeypatch):
    monkeypatch.setattr(R, "GET_RATE_MAX", 3)
    for _ in range(3):
        assert client.get("/api/tickets", headers={"X-Forwarded-For": "5.5.5.5"}).status_code == 200
    r = client.get("/api/tickets", headers={"X-Forwarded-For": "5.5.5.5"})
    assert r.status_code == 429 and r.json()["card"] == "rate_limited"
    assert client.get("/api/health").status_code == 200  # the health check is never limited
    assert client.get("/api/tickets", headers={"X-Forwarded-For": "5.5.5.6"}).status_code == 200


def test_s5_reaper_retries_warm_up_until_it_works(monkeypatch):
    calls = []
    stop = threading.Event()
    def warm():
        calls.append(1)
        if len(calls) >= 2:
            stop.set()
        raise RuntimeError("docker not up yet")
    monkeypatch.setattr(R, "sweep_orphans", lambda: [])
    monkeypatch.setattr(A, "warm", warm)
    monkeypatch.setattr(A, "reap_idle", lambda: [])
    monkeypatch.setattr(A, "_seed_checksum", None)
    threading.Timer(5, stop.set).start()  # a regression fails the assert, it does not hang the suite
    R.reaper(stop, interval=0.01)
    assert len(calls) == 2


def test_f6_no_route_serves_visitor_ips(client, fake):
    """harsh F6: GET /api/runs/usage/receipt served usage.json, every visitor's IP. No route may return an IP."""
    rid = make_run(client, fake, ip="203.0.113.7")
    assert "203.0.113.7" in R._usage_file().read_text(encoding="utf-8")  # it is saved, just never served
    for path in ["/api/runs/usage/receipt", "/api/runs/usage", "/api/tickets", f"/api/runs/{rid}",
                 f"/api/runs/{rid}/receipt", "/api/health"]:
        r = client.get(path)
        assert "203.0.113.7" not in r.text, path
    assert client.get("/api/runs/usage/receipt").status_code == 404


def test_m1_docker_down_is_reported_and_refused_before_a_run(client, fake, monkeypatch):
    """e2e M1: with Docker down, health said ok and every run failed with "try again", using up the visitor's day."""
    monkeypatch.setattr(R, "docker_up", lambda: False)
    h = client.get("/api/health")
    assert h.status_code == 200 and h.json()["docker"] is False  # the page and samples still serve
    r = post_run(client)
    assert r.status_code == 503 and r.json()["card"] == "sandbox_down"
    assert "Nothing ran" in r.json()["message"] and R.DAY["count"] == 0 and not R.RUNS


def test_m1_a_run_that_crashed_before_any_tokens_is_refunded(client, fake, monkeypatch):
    fake.trace = RuntimeError("docker volume create failed")
    rid = post_run(client, ip="1.2.3.4").json()["run_id"]
    assert R.RUNS[rid]["status"] == "unavailable" and R.DAY["ips"]["1.2.3.4"] == [0, 0]


def test_f10_one_visitor_cannot_spend_the_whole_day(client, fake, monkeypatch):
    """harsh F10: 150,000 tokens a day is a few runs; one IP could end the demo for every judge."""
    used = {"n": 0}
    monkeypatch.setattr(R, "tokens_today", lambda: used["n"])
    monkeypatch.setattr(R, "DAILY_TOKENS", 1000)
    monkeypatch.setattr(R, "IP_TOKEN_PCT", 50)
    real = fake.agent
    def spend(ticket, box):
        used["n"] += 450
        return real(ticket, box)
    monkeypatch.setattr(R, "run_agent", spend)
    assert post_run(client, ip="9.9.9.9").status_code == 201  # 450 used
    r = post_run(client, ip="9.9.9.9")
    assert r.status_code == 201 and R.DAY["tok"]["9.9.9.9"] == 900  # 450 is under the 500 share: still allowed
    r = post_run(client, ip="9.9.9.9")
    assert r.status_code == 429 and r.json()["card"] == "rate_limited" and "share" in r.json()["message"]
    assert post_run(client, ip="8.8.8.8").status_code == 201  # another visitor still runs


@pytest.mark.parametrize("path,body", [("approve", {"role": "engineer"}), ("close", {}), ("undo", None)])
def test_run_actions_share_the_per_ip_get_limit(client, fake, monkeypatch, path, body):
    """S4 (final claims review): /approve, /close and /undo are rate-limited per IP like the GETs."""
    monkeypatch.setattr(R, "GET_RATE_MAX", 2)
    h = {"X-Forwarded-For": "8.8.8.8"}
    url = f"/api/runs/{'0' * 32}/{path}"
    for _ in range(2):
        assert client.post(url, json=body, headers=h).status_code != 429
    r = client.post(url, json=body, headers=h)
    assert r.status_code == 429 and r.json()["card"] == "rate_limited"


def test_invalid_run_bodies_do_not_use_up_the_run_limit(client, fake):  # bugs.md F4
    h = {"X-Forwarded-For": "9.9.9.9"}
    for _ in range(R.RATE_MAX + 2):
        assert client.post("/api/runs", json={"ticket_id": "NOPE"}, headers=h).status_code == 422
    assert post_run(client, ip="9.9.9.9").status_code == 201


def test_unknown_page_is_a_page_not_raw_json(client):  # qa.md: /nope showed raw JSON with no way back
    r = client.get("/nope")
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert 'href="/"' in r.text and "{" not in r.text
    api = client.get("/api/nope")
    assert api.status_code == 404 and api.json()["card"] == "invalid"  # the API keeps its JSON card
