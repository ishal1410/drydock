"""Task 5: change contract agent loop (plan L619-661, pins task5-6-interface-pins.md).

Offline tests: every model response below is a TEST FIXTURE authored by hand in the OpenAI chat shape; none was ever
returned by a real model, and the product has no replay path (plan S1). The only fake below the agent is FakePg in
place of the docker exec -> psql call, so run_sql, run_read_only, scope_pks, run_checks, policy_results, snapshot_hashes,
diff, compare and approval are the real code.

Tests named test_docker_* run the same loop on a real sandbox and skip when Docker is unreachable.
"""
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.encoders import jsonable_encoder

import core.measure as measure
import sandbox.runner as runner
from core.contract import MAX_REVISIONS, approval
from eval import run_eval
from llm import agent
from llm.agent import CHECK_AUTHOR, MAX_LOOKUPS, run_agent
from llm.client import ModelRequestError, ModelTruncated, ModelUnavailable
from sandbox.container import RunBox
from seed.seed import TABLES

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads((ROOT / "seed" / "golden.json").read_text(encoding="utf-8"))
TICKETS = {t["id"]: t for t in json.loads((ROOT / "seed" / "tickets.json").read_text(encoding="utf-8"))}
OVERDUE = "status = 'open' AND due_on <= DATE '2026-11-01' - 90"
PLAN_SAFE = OVERDUE + " AND NOT EXISTS (SELECT 1 FROM payment_plans p WHERE p.invoice_id = invoices.id AND p.status = 'active')"
# The loop's mechanics, on invoices only: FakePg models no accounts balances (the golden's P-5 half is test_seed's)
NAIVE, SAFE = GOLDEN["E8"]["naive_sql"], f"UPDATE invoices SET status = 'written_off' WHERE {PLAN_SAFE}"
LOOK_ALL = f"SELECT amount_cents FROM invoices WHERE {OVERDUE}"
LOOK_SAFE = f"SELECT amount_cents FROM invoices WHERE {PLAN_SAFE}"
CHECKS = [{"name": "only_written_off", "sql": "SELECT count(*) = 87 FROM invoices WHERE status = 'written_off'"},
          {"name": "none_recent", "sql": "SELECT count(*) = 0 FROM invoices WHERE status = 'written_off' AND id > 90"}]
E8_REPLY = {"explanation": "87 invoices moved open to written_off, worth $31,319.00. Policy P-7 failed first, on 3 rows "
                           "with $1,169.18 owed, so the revision left them open.",
            "reply": "Done on a sandbox copy: 87 invoices written off ($31,319.00)."}
GREEN = {"explanation": "87 invoices moved open to written_off, worth $31,319.00.",
         "reply": "Done on a sandbox copy: 87 invoices written off ($31,319.00)."}  # a first-try green: no P-7 to mention
E11_REPLY = {"explanation": "usage_events.amount_cents is the rated amount, so no rate is needed, but the scan was "
                            "stopped by the disk spill cap. Nothing changed. It needs a batched job.",
             "reply": "This re-rate is larger than the per-run cap and needs a batched job."}


# ---- hand-written model responses (test fixtures, never from a real model) ----
def _resp(message, finish="stop"):
    return {"id": "fixture", "model": "fixture-model-1", "choices": [{"index": 0, "finish_reason": finish,
                                                                      "message": {"role": "assistant", **message}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}}


def text(s):
    return _resp({"content": s})


def js(obj):
    return text(json.dumps(obj))


def tool(name, **args):  # the normal OpenAI tool-call shape: content is null
    return _resp({"content": None, "tool_calls": [{"id": f"call_{name}", "type": "function", "function": {
        "name": name, "arguments": json.dumps(args)}}]}, "tool_calls")


def contract(lo=85, hi=95, where=OVERDUE, checks=CHECKS, table="invoices"):
    return js({"tables": [table], "expected_rows": {table: [lo, hi]}, "scope_rule": {"table": table, "where": where},
               "checks": checks})


def sql(s):
    return js({"sql": s})


class Model:
    """Scripted endpoint: answers each POST with the next fixture and records every request body."""

    def __init__(self, *script):
        self.script, self.requests = list(script), []

    def __call__(self, request):
        self.requests.append(json.loads(request.content))
        r = self.script.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)

    @property
    def transport(self):
        return httpx.MockTransport(self)


def first_attempt(checks=CHECKS):
    return [text("Write off the overdue open invoices."), tool("lookup", select_sql=LOOK_ALL),
            contract(checks=checks), sql(NAIVE)]


def e8_model(reply=E8_REPLY):
    """E8 as the plan tells it: naive v1 hits P-7, lookup_policy, recount, v2 excludes the plan invoices."""
    return Model(*first_attempt(), tool("lookup_policy", policy_id="P-7"), tool("lookup", select_sql=LOOK_SAFE), contract(80, 90, PLAN_SAFE), sql(SAFE), js(reply))


# ---- FakePg stands in for `docker exec psql`: rows in memory, xmin bumps on write, keyed by container ----
class FakePg:
    def __init__(self):
        self.boxes, self.check_results, self.destroyed, self.sql_log = {}, {}, [], []
        self.dropped = set()

    def state(self, box_id):
        if box_id not in self.boxes:
            # 3 invoices on active plans inside the overdue set; 87 others summing to the golden 3131900; 10 paid
            inv = {i: {"id": i, "status": "open", "amount_cents": 36000 - (100 if i == 4 else 0), "paid_cents": 0}
                   for i in range(1, 91)}
            inv.update({i: {"id": i, "status": "paid", "amount_cents": 500, "paid_cents": 500} for i in range(91, 101)})
            self.boxes[box_id] = {"invoices": {k: ["10", v] for k, v in inv.items()},
                                  "accounts": {i: ["10", {"id": i, "usage_cents_oct": 5}] for i in (1, 2)}}
        return self.boxes[box_id]

    def _ids(self, s, q):
        inv = s["invoices"]
        if "id = -1" in q:
            return []
        if "id BETWEEN 4 AND 18" in q:
            return list(range(4, 19))
        if "status = 'open'" in q:
            return [k for k, (_, r) in inv.items() if r["status"] == "open" and ("NOT EXISTS" not in q or k > 3)]
        return list(inv)

    def __call__(self, box_id, conninfo, q, timeout=None, user="postgres", quiet=True):
        self.sql_log.append((box_id, q))
        s = self.state(box_id)
        ok = lambda out="": subprocess.CompletedProcess([], 0, out, "")  # noqa: E731
        bad = lambda code, msg: subprocess.CompletedProcess([], 1, "", f"ERROR:  {code}: {msg}\nDETAIL:  Failing row contains (secret-row-value).")  # noqa: E731
        if q == measure.SNAPSHOT_SQL:
            if box_id in self.dropped:
                return subprocess.CompletedProcess([], 1, "", 'ERROR:  relation "invoices" does not exist')
            out = {t: [len(s.get(t, {})), {str(k): v for k, v in s.get(t, {}).items()}] for t in TABLES}
            return ok(json.dumps(out))
        if "usage_events" in q:
            return bad("53400", "temporary file size exceeds temp_file_limit (65536kB)")
        if "BADSQL" in q:
            return bad("42601", "syntax error at or near BADSQL")
        if q.startswith("BEGIN TRANSACTION READ ONLY"):
            inner = q.split("\n", 1)[1].rsplit("\n;", 1)[0]
            if inner.startswith("WITH q AS"):
                ids = self._ids(s, inner)
                total = sum(s["invoices"][k][1]["amount_cents"] for k in ids)
                if "LEAK" in inner:
                    return ok(json.dumps({"count": 1, "sums": {"name": "Cedarfall Clinics"}}))
                return ok(json.dumps({"count": len(ids), "sums": {"amount_cents": total}}))
            if inner.startswith("SELECT id FROM"):
                if "BADSCOPE" in inner:
                    return bad("42703", "column badscope does not exist")
                return ok("\n".join(map(str, self._ids(s, inner))))
            return ok(self.check_results.get(inner, "t"))
        for p in measure.POLICIES:
            if q == p["check_sql"]:
                n = sum(1 for k in (1, 2, 3) if s["invoices"][k][1]["status"] == "written_off") if p["id"] == "P-7" else 0
                return ok(str(n))
        if q.startswith("SELECT coalesce(json_agg"):  # P-7 detail: real rows, must never reach a prompt
            rows = [{"customer": f"Customer {k}", "owed_cents": o, "arr_cents": a}
                    for k, o, a in ((1, 6668, 240000), (2, 49950, 1198800), (3, 60300, 1206000))
                    if s["invoices"][k][1]["status"] == "written_off"]
            return ok(json.dumps(rows))
        if q.startswith("UPDATE invoices"):
            ids = self._ids(s, q)
            for k in ids:
                s["invoices"][k] = [str(int(s["invoices"][k][0]) + 1), {**s["invoices"][k][1], "status": "written_off"}]
            if "DROPME" in q:
                self.dropped.add(box_id)
            return ok("" if quiet else f"UPDATE {len(ids)}\n")
        if q.startswith("DROP"):
            return bad("42501", "must be owner of table")
        raise AssertionError(f"FakePg got unexpected SQL: {q[:120]}")


def make_box(name="box-given"):
    return RunBox(name, "admin", "exec", datetime.now(timezone.utc), name)


@pytest.fixture
def pg(monkeypatch):
    p = FakePg()
    monkeypatch.setattr(runner, "psql", p)
    monkeypatch.setattr(measure, "psql", p)
    n = iter(range(1, 20))
    monkeypatch.setattr(agent, "start_run_container", lambda: make_box(f"box-fresh-{next(n)}"))
    monkeypatch.setattr(agent, "destroy", lambda b: p.destroyed.append(b.id))
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("DRYDOCK_MODEL", "fixture-model-1")
    monkeypatch.delenv("DRYDOCK_VM_ID", raising=False)
    return p


def run(model, ticket="E8", box=None):
    t = TICKETS[ticket] if isinstance(ticket, str) else ticket
    return run_agent(t, box or make_box(), transport=model.transport)


def names(trace):
    return [s.name for s in trace.steps]


def steps(trace, name):
    return [s for s in trace.steps if s.name == name]


# ---- plan Step 1 table ----
def test_steps_in_order(pg):
    # plan L639 pattern, plus the contract/sql/lookup steps a revise really has (pin 6, ui-api-contract)
    order = " ".join(names(run(e8_model())))
    assert order == ("read lookup contract sql run compare "
                     "revise lookup_policy lookup contract sql run compare explain")
    assert re.fullmatch(r"read( lookup)+ contract sql run compare"
                        r"( revise( lookup_policy| lookup)* contract sql run compare)* explain", order)


def test_lookup_returns_numbers_only(pg):
    t = run(e8_model())
    p = steps(t, "lookup")[0].payload
    assert p["result"] == {"count": 90, "sums": {"amount_cents": 3239900}}
    assert p["sql"] == LOOK_ALL
    assert "stdout" not in json.dumps(jsonable_encoder(p))


def test_lookup_refuses_non_numeric_output(pg):
    m = Model(text("r"), tool("lookup", select_sql="SELECT name FROM customers -- LEAK"), text("done"),
              contract(80, 90, PLAN_SAFE), sql(SAFE), js(GREEN))
    t = run(m)
    assert "Cedarfall" not in json.dumps(m.requests) and steps(t, "lookup")[0].payload["result"] is None
    assert "error" in [x for x in m.requests[2]["messages"] if x["role"] == "tool"][0]["content"]


def test_lookup_wrapper_is_read_only_counted_subquery(pg):
    run(e8_model())
    wrapped = [q for _, q in pg.sql_log if "WITH q AS" in q][0]
    assert wrapped.startswith("BEGIN TRANSACTION READ ONLY") and wrapped.rstrip().endswith("ROLLBACK")
    assert "count(*)" in wrapped and LOOK_ALL in wrapped


def test_lookups_capped_per_phase(pg):
    m = Model(text("r"), *[tool("lookup", select_sql="SELECT 1; SELECT 2")] * MAX_LOOKUPS, contract(80, 90, PLAN_SAFE),
              sql(SAFE), js(GREEN))
    assert len(steps(run(m), "lookup")) == MAX_LOOKUPS == 3  # D8 "Lookups (1-3)": no fourth ask


def test_policy_tool_only_on_revise(pg):
    m = e8_model()
    run(m)
    offered = [{f["function"]["name"] for f in r.get("tools", [])} for r in m.requests]
    first_red = 4  # read, lookup, contract, sql came before it
    assert all("lookup_policy" not in o for o in offered[:first_red])
    assert "lookup_policy" in offered[first_red]


def test_policy_tool_not_offered_when_no_policy_named(pg):
    pg.check_results[CHECKS[0]["sql"]] = "f"  # a check is red, no policy is: SAFE leaves P-7 at 0
    m = Model(text("r"), text("done"), contract(80, 90, PLAN_SAFE), sql(SAFE), text("done"),
              contract(80, 90, PLAN_SAFE), sql(SAFE), text("done"), contract(80, 90, PLAN_SAFE), sql(SAFE),
              js({"explanation": "A check failed.", "reply": "Not done."}))
    run(m)
    assert all("lookup_policy" not in {f["function"]["name"] for f in r.get("tools", [])} for r in m.requests)


def test_revise_on_red(pg):
    t = run(e8_model())
    assert [v.green for v in t.verdicts] == [False, True] and t.revisions == 1 and t.verdict.green
    assert t.verdicts[0].failures[0].kind == "policy" and t.verdicts[0].failures[0].detail.startswith("P-7")


def test_max_two_revisions(pg):
    m = Model(*first_attempt(), text("done"), contract(), sql(NAIVE), text("done"), contract(), sql(NAIVE),
              js({"explanation": "P-7 failed on every attempt, so nothing can be approved.", "reply": "Not done."}))
    t = run(m)
    assert t.revisions == MAX_REVISIONS == 2 and len(t.verdicts) == 3 and not t.verdict.green
    assert not m.script  # no fourth attempt
    assert "P-7" in t.explanation
    assert t.approval_class == "C" and t.approval_reason.startswith("red verdict")  # Approve disabled


def test_free_text_gets_checks(pg):
    ticket = {"id": None, "requester": "You", "subject": "Free-text ticket", "body": "write off the old open invoices"}
    t = run(e8_model(), ticket)
    lines = steps(t, "compare")[0].payload["lines"]
    assert {"P-1", "P-2", "P-3", "P-4", "P-7"} <= {x["title"] for x in lines if x["kind"] == "policy"}
    assert steps(t, "contract")[0].payload["contract"]["tables"] == ["invoices"]
    assert "free-text request" in t.approval_reason


def test_reply_uses_measured_numbers(pg):
    t = run(e8_model())
    assert t.reply == E8_REPLY["reply"] and t.effect.rows_by_table["invoices"]["touched"] == 87
    prompt = (ROOT / "llm" / "prompt.txt").read_text(encoding="utf-8")
    assert not re.search(r"\$\s?\d", prompt)  # judge-3 N8: no hard-coded money


@pytest.mark.parametrize("bad", ["Done: 87 invoices written off ($99,999.00).", "Done: 999 invoices written off."])
def test_reply_with_untraceable_number_never_shown(pg, bad):
    m = e8_model({"explanation": E8_REPLY["explanation"], "reply": bad})
    m.script.append(js({"explanation": E8_REPLY["explanation"], "reply": bad}))  # the one re-ask repeats it
    t = run(m)
    assert t.reply != bad and "99,999" not in t.reply and "999 " not in t.reply
    assert "87" in t.reply  # code-built reply from the measured summary
    assert steps(t, "explain")[-1].payload["rejected"]


def test_reply_reask_once_then_accepts(pg):
    m = e8_model({"explanation": E8_REPLY["explanation"], "reply": "Done ($99,999.00)."})
    m.script.append(js(E8_REPLY))
    assert run(m).reply == E8_REPLY["reply"]


def test_reply_sum_of_policy_rows_is_traceable(pg):
    # $1,169.18 = 6668 + 49950 + 60300: a sum of measured P-7 rows from the red attempt (E8_REPLY uses it)
    assert run(e8_model()).explanation == E8_REPLY["explanation"]


def e11_model(reply=E11_REPLY):
    bad = GOLDEN["E11"]["sql"]
    w, ch = "id > 0", [{"name": "a", "sql": "SELECT count(*) > 0 FROM accounts WHERE usage_cents_oct >= 0"},
                       {"name": "b", "sql": "SELECT count(*) = 2 FROM accounts WHERE usage_cents_oct >= 0"}]
    c = contract(2, 2, w, ch, "accounts")
    return Model(text("r"), text("done"), c, sql(bad), js(reply))


def test_e11_explanation_names_source(pg):
    m = e11_model()
    t = run(m, "E11")
    assert "usage_events.amount_cents" in t.explanation and "rated amount" in t.explanation
    assert "disk spill cap" in t.explanation
    assert "disk spill cap" in json.dumps(m.requests[-1]["messages"], ensure_ascii=False)  # pin 7
    assert "usage_events.amount_cents" in json.dumps(m.requests[-1]["messages"])


def test_e11_explanation_missing_cap_is_code_built(pg):
    weak = {"explanation": "It did not work.", "reply": "Not done."}
    m = e11_model(weak)
    m.script.append(js(weak))
    t = run(m, "E11")
    assert "disk spill cap" in t.explanation and "usage_events.amount_cents" in t.explanation


def test_explanation_from_measured_summary(pg):
    m = e8_model()
    t = run(m)
    prompt = json.dumps(m.requests[-1]["messages"], ensure_ascii=False)
    assert "invoices.status: open→written_off ×87" in prompt
    assert "$31,319.00" in prompt and "$1,169.18" in prompt
    assert "Customer 1" not in prompt and "amount_cents\": 36000" not in prompt
    assert t.explanation == E8_REPLY["explanation"]
    assert not m.requests[-1].get("tools")


def test_command_tag_recorded(pg):
    t = run(e8_model())
    p = steps(t, "run")[0].payload
    assert p["command_tag"] == "UPDATE 90" and isinstance(p["elapsed_ms"], int) and p["cap_ms"] == 5000 and p["ok"]
    assert steps(t, "compare")[0].payload["command_tag"] == "UPDATE 90"


def test_every_step_names_its_service(pg):
    for s in run(e8_model()).steps:
        assert s.service, s.name


def test_service_names_never_claim_a_vm_or_vultr_they_did_not_use(pg, monkeypatch):
    t = run(e8_model())  # no DRYDOCK_VM_ID, base url example.test
    assert not any("Vultr" in s.service for s in t.steps)
    monkeypatch.setenv("DRYDOCK_VM_ID", "vm-1")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.vultrinference.com/v1")
    pg.boxes.clear()
    t = run(e8_model())
    assert steps(t, "read")[0].service == "Vultr Serverless Inference, fixture-model-1"
    assert "Vultr" in steps(t, "run")[0].service and "Vultr" in steps(t, "compare")[0].service


def test_contract_locked_before_sql(pg):
    t = run(e8_model())
    c, s = steps(t, "contract")[0], steps(t, "sql")[0]
    assert t.contract_locked_at < s.started_at
    assert t.contract_sha == c.payload["sha256"] == agent.contract_sha(agent.parse_contract(json.dumps(c.payload["contract"])))
    assert c.payload["locked_at"] == t.contract_locked_at
    assert steps(t, "contract")[1].payload["sha256"] != t.contract_sha  # v2 has its own; trace keeps v1 (plan L650)


def test_trace_exposes_effect_verdicts_revisions(pg):
    t = run(e8_model())
    assert len(t.verdicts) == 2 and t.revisions == 1
    assert t.effect.rows_by_table == {"invoices": {"count_delta": 0, "touched": 87}}
    assert t.sql == SAFE
    for s in t.steps:
        if s.actor == "model":
            assert s.model_id == "fixture-model-1" and s.tokens == s.tokens_in + s.tokens_out > 0 and s.ms is not None
        else:
            assert s.model_id is None and s.tokens is None and not hasattr(s, "tokens_in")


def test_tokens_of_unstepped_calls_roll_forward(pg):
    # 9 scripted model calls at 110 tokens each: the eval's tokens-per-run must count the lookup asks too
    t = run(e8_model())
    assert sum(s.tokens or 0 for s in t.steps) == 9 * 110


def test_failure_text_fed_back(pg):
    m = e8_model()
    t = run(m)
    revise = steps(t, "revise")[0].payload["fed_back"]
    assert revise == ["P-7: violations 0 → 3"]
    assert "P-7: violations 0 → 3" in json.dumps(m.requests[4]["messages"], ensure_ascii=False)
    assert "never written off" not in json.dumps(m.requests[4]["messages"])  # the rule only comes from the lookup
    lp = steps(t, "lookup_policy")[0].payload
    assert lp == {"policy_id": "P-7", "text": "Invoices on an active payment plan are never written off."}
    assert lp["text"] in json.dumps(m.requests[5]["messages"])
    d = steps(t, "contract")[1].payload["diff"]
    assert "- expected_rows.invoices: [85, 95]" in d and "+ expected_rows.invoices: [80, 90]" in d
    assert any(x.startswith("+ scope_rule.where:") and "NOT EXISTS" in x for x in d)


def test_a_good_lookup_does_not_skip_an_unread_policy(pg):
    """Live E8 2026-11-07: on the revise the model looked up rows first, the good lookup ended the tool phase, and the
    P-7 rule it was told to read never reached it. Attempt 3 dropped the plan exclusion and the run ended red."""
    m = Model(*first_attempt(), tool("lookup", select_sql=LOOK_SAFE), tool("lookup_policy", policy_id="P-7"),
              contract(80, 90, PLAN_SAFE), sql(SAFE), js(E8_REPLY))
    t = run(m)
    assert steps(t, "lookup_policy")[0].payload["text"] == "Invoices on an active payment plan are never written off."
    assert t.verdict.green and t.revisions == 1


def test_a_policy_read_once_is_restated_on_later_revises(pg):
    """compact() drops the last attempt's chatter; a rule the model already read must survive it."""
    a = agent._Run.__new__(agent._Run)
    a.msgs, a.t, a.contract, a.last_sql = [1, 2, 3, 4], agent.RunTrace(), None, None
    a.read_policies = {"P-7"}
    assert "never written off" in a.compact()


def test_lookup_policy_refuses_a_policy_no_check_named(pg):
    m = Model(*first_attempt(), tool("lookup_policy", policy_id="P-2"), text("done"), contract(80, 90, PLAN_SAFE),
              sql(SAFE), js(E8_REPLY))
    t = run(m)
    assert "error" in steps(t, "lookup_policy")[0].payload
    assert "refunded twice" not in json.dumps(m.requests)


def test_checks_via_run_checks(pg):
    pg.check_results[CHECKS[1]["sql"]] = "f"
    t = run(Model(*first_attempt(), text("done"), contract(), sql(NAIVE), text("done"), contract(), sql(NAIVE),
                  js({"explanation": "Checks failed.", "reply": "Not done."})))
    v = t.verdicts[0]
    assert "none_recent did not return true" in [f.detail for f in v.failures]  # "f" -> False
    lines = {x["title"]: x for x in steps(t, "compare")[0].payload["lines"]}
    assert lines["only_written_off"]["ok"] is True and lines["none_recent"]["ok"] is False


def test_constant_check_is_contract_revise_never_green(pg):
    const = [{"name": "c1", "sql": "SELECT true"}, CHECKS[0]]
    m = Model(text("r"), tool("lookup", select_sql=LOOK_SAFE), contract(80, 90, PLAN_SAFE, const),
              contract(80, 90, PLAN_SAFE), sql(SAFE), js(GREEN))
    m.script.insert(3, text("done"))  # the revise's tool phase answers before the second contract
    t = run(m)
    bad = steps(t, "contract")[0].payload
    assert "error" in bad and "constant check" in bad["error"] and "contract" not in bad
    assert t.revisions == 1 and len(t.verdicts) == 1 and t.verdict.green
    assert "constant check" in steps(t, "revise")[0].payload["fed_back"][0]
    assert "constant check" in json.dumps(m.requests[3]["messages"])


def test_unparseable_contract_marks_step_invalid_for_scorer(pg):
    m = Model(text("r"), text("done"), text("I cannot produce JSON, sorry."), text("done"), contract(80, 90, PLAN_SAFE),
              sql(SAFE), js(GREEN))
    s = run_eval.score("E8", GOLDEN["E8"], run(m))
    assert s["contracts"] == 2 and s["contracts_valid"] == 1


def test_contract_fails_three_times_is_red_not_a_crash(pg):
    m = Model(text("r"), text("done"), text("no"), text("done"), text("no"), text("done"), text("no"),
              js({"explanation": "No valid contract.", "reply": "Not done."}))
    t = run(m)
    assert not t.verdict.green and t.revisions == 2 and t.verdicts == [] and t.sql is None
    assert t.effect.rows_by_table == {} and t.approval_class == "C"
    assert run_eval.score("E8", GOLDEN["E8"], t)["passed"] is False


def test_no_op_and_class_c_reason(pg):
    m = Model(text("r"), tool("lookup", select_sql=f"SELECT id FROM invoices WHERE {OVERDUE}"),
              contract(0, 1, "id = -1"), sql("UPDATE invoices SET status = 'written_off' WHERE id = -1"),
              js({"explanation": "Nothing changed.", "reply": "Nothing to do."}))
    t = run(m)
    assert (t.approval_class, t.approval_reason) == ("no-op", "nothing changed: nothing to approve")
    m = Model(*first_attempt(), text("done"), contract(), sql(NAIVE), text("done"), contract(80, 90, PLAN_SAFE), sql(SAFE),
              js(E8_REPLY))
    t = run(m)
    assert t.verdict.green and t.revisions == 2
    assert (t.approval_class, t.approval_reason) == ("C", "2 revisions used: too many retries to trust a green run")
    assert approval(t.verdict, t, "ticket") == (t.approval_class, t.approval_reason)


def test_contract_range_traceable(pg):
    t = run(e8_model())
    lines = [x for x in steps(t, "compare")[1].payload["lines"] if x["kind"] == "row_count"]
    assert lines[0]["detail"] == "lookup 87 → promised [80,90] → measured 87"
    assert (lines[0]["lookup"], lines[0]["promised"], lines[0]["measured"]) == (87, [80, 90], 87)
    assert 80 <= steps(t, "lookup")[1].payload["result"]["count"] <= 90 and t.verdict.warnings == []
    lo, hi = 4, 18
    q = f"UPDATE invoices SET status = 'written_off' WHERE id BETWEEN {lo} AND {hi}"
    m = Model(text("r"), tool("lookup", select_sql=LOOK_SAFE), contract(10, 20, f"id BETWEEN {lo} AND {hi}"),
              sql(q), js({"explanation": "15 invoices moved.", "reply": "15 done."}))
    t = run(m)
    assert t.verdict.green and "range not backed by a lookup: invoices [10,20]" in t.verdict.warnings
    assert t.approval_class != "A"


def test_range_with_no_lookup_on_that_table_warns(pg):
    q = "UPDATE invoices SET status = 'written_off' WHERE id BETWEEN 4 AND 18"
    m = Model(text("r"), tool("lookup", select_sql="SELECT id FROM accounts"),
              contract(10, 20, "id BETWEEN 4 AND 18"), sql(q), js({"explanation": "15 moved.", "reply": "15 done."}))
    assert "range not backed by a lookup: invoices [10,20]" in run(m).verdict.warnings


def test_panel_lines_tagged_by_author(pg):
    pg.check_results[CHECKS[1]["sql"]] = "f"
    t = run(Model(*first_attempt(), text("done"), contract(), sql(NAIVE), text("done"), contract(), sql(NAIVE),
                  js({"explanation": "Failed.", "reply": "Not done."})))
    lines = steps(t, "compare")[0].payload["lines"]
    assert CHECK_AUTHOR == {"check": "AI-written"}
    for x in lines:
        assert x["author"] == ("AI-written" if x["kind"] == "check" else "DryDock-checked"), x
    assert any(x["kind"] == "policy" and not x["ok"] and x["author"] == "DryDock-checked" for x in lines)
    assert any(x["kind"] == "check" and not x["ok"] and x["author"] == "AI-written" for x in lines)


def test_eval_score_source(tmp_path):
    def crash(ticket, box):
        raise ValueError("no agent in this test")
    res = run_eval.run("fixture-model-1", 3, agent=crash, start=lambda: None, stop=lambda b: None)
    text_ = run_eval.write_results(res, tmp_path).read_text(encoding="utf-8")
    assert re.search(r"\d+/18 correct vs hand-written goldens", text_)
    for p in [ROOT / "README.md", *(ROOT / "docs").glob("**/*.md")]:
        if p.is_file():
            assert not re.search(r"\b\d+/18\b", p.read_text(encoding="utf-8")), p


def test_checks_reference_changed_columns(pg):
    prompt = (ROOT / "llm" / "prompt.txt").read_text(encoding="utf-8")
    assert "must reference at least one column" in prompt
    constant = [{"name": "c1", "sql": "SELECT count(*) >= 0 FROM invoices"}, CHECKS[0]]
    m = Model(*first_attempt(constant), text("done"), contract(80, 90, PLAN_SAFE), sql(SAFE), js(GREEN))
    t = run(m)
    assert t.revisions == 1 and len(steps(t, "run")) == 1 and t.verdict.green  # v1 never ran
    assert "c1" in steps(t, "revise")[0].payload["fed_back"][0] and "status" in steps(t, "revise")[0].payload["fed_back"][0]
    for line in steps(t, "compare")[0].payload["lines"]:
        if line["kind"] == "check":
            assert line["sql"] and isinstance(line["ok"], bool)  # SQL text next to its bool


@pytest.mark.parametrize("q,cols", [
    ("UPDATE invoices SET status = 'void' WHERE id = 1", ["status"]),
    ("update accounts a set usage_cents_oct = (select sum(x, y) from t), status = 'a,b' where a.id = 1",
     ["usage_cents_oct", "status"]),
    ("WITH c AS (UPDATE accounts SET status = 'closed' WHERE id = 1 RETURNING id) "
     "UPDATE subscriptions SET status = 'cancelled' FROM c WHERE true", ["status", "status"]),
    ("UPDATE t SET (a, b) = (1, 2)", ["a", "b"]),
    ("INSERT INTO credits (reason) VALUES ('SET x = 1')", []),
    ("-- UPDATE t SET nope = 1\nDELETE FROM credits", []),
])
def test_set_columns(q, cols):
    assert agent.set_columns(q) == cols


def test_compare_gets_before_and_sql(pg, monkeypatch):
    seen = []
    real = agent.compare
    monkeypatch.setattr(agent, "compare", lambda *a, **k: (seen.append(k), real(*a, **k))[1])
    real_pol = agent.policy_results

    def pre_existing_p3(box):
        return {**real_pol(box), "P-3": measure.PolicyResult(1, [])}  # a violation already there before the SQL
    monkeypatch.setattr(agent, "policy_results", pre_existing_p3)
    t = run(e8_model())
    assert seen[0]["sql"] == NAIVE and seen[1]["sql"] == SAFE
    assert seen[0]["policy_before"]["P-7"].count == 0 and seen[0]["cap"] is None
    assert not any(f.detail.startswith("P-3") for v in t.verdicts for f in v.failures) and t.verdict.green


# ---- pins (task5-6-interface-pins.md) ----
@pytest.mark.parametrize("bad", ["UPDATE invoices SET status = 'void'; DELETE FROM credits",
                                 "UPDATE invoices SET status = 'a;b' WHERE id = 1"])  # strict about ; in literals (A4)
def test_pin4_one_statement_before_run_sql(pg, bad):
    m = Model(*first_attempt()[:-1], sql(bad), text("done"), contract(80, 90, PLAN_SAFE), sql(SAFE), js(GREEN))
    t = run(m)
    assert not any(q == bad or "DELETE FROM credits" in q for _, q in pg.sql_log)
    assert "one statement" in steps(t, "revise")[0].payload["fed_back"][0]
    assert t.revisions == 1 and t.sql == SAFE


def test_pin4_lookup_one_statement(pg):
    m = Model(text("r"), tool("lookup", select_sql="SELECT 1; SELECT 2"), text("done"), contract(80, 90, PLAN_SAFE),
              sql(SAFE), js(GREEN))
    t = run(m)
    assert not any("SELECT 2" in q for _, q in pg.sql_log)
    assert steps(t, "lookup")[0].payload["ok"] is False


def test_trailing_semicolon_normalised_so_apply_accepts_it(pg):
    m = Model(*first_attempt()[:-1], sql(SAFE + " ;"), js(GREEN))
    t = run(m)
    assert t.sql == SAFE and measure.one_statement(t.sql) == t.sql


def test_pin5_fresh_box_per_attempt(pg):
    given = make_box()
    t = run(Model(*first_attempt(), text("done"), contract(), sql(NAIVE), text("done"), contract(80, 90, PLAN_SAFE),
                  sql(SAFE), js(E8_REPLY)), box=given)
    used = [b for b, q in pg.sql_log if q.startswith("UPDATE invoices")]
    assert used == ["box-given", "box-fresh-1", "box-fresh-2"]
    # B10: the first box goes before a revise box starts, so a run holds one sandbox at a time. The caller's destroy
    # after this is a no-op on a gone box (sandbox/container.py destroy is idempotent).
    assert pg.destroyed == ["box-given", "box-fresh-1", "box-fresh-2"]
    assert [s.payload["container_id"] for s in steps(t, "run")] == used
    assert t.effect.rows_by_table["invoices"]["touched"] == 87  # measured on a clean copy, not on top of attempt 1


def test_pin5_revise_lookups_see_clean_box(pg):
    t = run(e8_model())
    assert steps(t, "lookup")[1].payload["result"]["count"] == 87  # attempt 1 would have left 0 open
    assert "box-fresh-1" in steps(t, "lookup")[1].service


@pytest.mark.parametrize("at", [0, 3, 5])
def test_pin8_model_unavailable_escapes(pg, at):
    m = e8_model()
    m.script[at:] = [httpx.Response(503, text="down")] * 6  # an outage, not one blip: the client retries a blip
    with pytest.raises(ModelUnavailable):
        run(m)
    assert pg.destroyed == (["box-given", "box-fresh-1"] if at >= 5 else [])  # no leaked box


def test_pin8_escapes_even_when_destroy_fails(pg, monkeypatch):
    def boom(b):
        raise RuntimeError("docker gone")
    monkeypatch.setattr(agent, "destroy", boom)
    m = e8_model()
    m.script[6:] = [httpx.Response(503, text="down")] * 6
    with pytest.raises(ModelUnavailable):
        run(m)


def test_truncated_reply_escapes(pg):
    m = e8_model()
    m.script[3] = _resp({"content": "{"}, "length")
    with pytest.raises(ModelTruncated):
        run(m)


def test_pin9_reads_tool_calls_not_text(pg):
    m = e8_model()
    assert m.script[1]["choices"][0]["message"]["content"] is None
    t = run(m)
    assert steps(t, "lookup")
    echoed = [x for x in m.requests[2]["messages"] if x.get("tool_calls")][0]
    assert echoed["tool_calls"][0]["id"] == "call_lookup" and set(echoed) == {"role", "content", "tool_calls"}
    assert [x for x in m.requests[2]["messages"] if x["role"] == "tool"][0]["tool_call_id"] == "call_lookup"


def test_pin6_scorer_reads_recorded_e8(pg):
    t = run(e8_model())
    assert not run_eval.score("E8", GOLDEN["E8"], t)["passed"]  # harsh F3: invoices only leaves balances stale
    invoices_only = {**GOLDEN["E8"], "expected": {"invoices": GOLDEN["E8"]["expected"]["invoices"]}}
    s = run_eval.score("E8", invoices_only, t)
    assert s["passed"] and s["p7_first"] is True and s["contracts_valid"] == s["contracts"] == 2
    assert s["amount"] == 3131900 and s["tokens"] == 9 * 110 and s["model_ids"] == ["fixture-model-1"] and s["ms"]


def test_pin6_scorer_reads_recorded_e11(pg):
    t = run(e11_model(), "E11")
    s = run_eval.score("E11", GOLDEN["E11"], t)
    assert s["passed"] and s["cap"] == "53400" and s["explained"]
    assert t.effect.rows_by_table == {} and approval(t.verdict, t, "ticket")[0] == "C"
    p = steps(t, "run")[0].payload
    assert (p["ok"], p["code"], p["stopped_by"], p["cap_label"], p["command_tag"]) == (
        False, "53400", "temp_file_limit", "disk spill cap", "ERROR")
    assert p["stopped_at"] and p["result"].code == "53400"
    assert t.revisions == 0 and not steps(t, "revise")  # B6: a cap ends the run
    assert any(x["kind"] == "cap" and not x["ok"] for x in steps(t, "compare")[0].payload["lines"])


# ---- generic provider fallback: the endpoint has never been called for real, so neither shape is assumed ----
def test_tools_rejected_falls_back_to_json_in_content(pg):
    m = Model(text("r"), httpx.Response(400, text="tools not supported"),
              js({"tool": "lookup", "select_sql": LOOK_SAFE}), contract(80, 90, PLAN_SAFE), sql(SAFE),
              js(GREEN))
    t = run(m)
    assert m.requests[1].get("tools") and "tools" not in m.requests[2] and "tools" not in m.requests[3]
    assert steps(t, "lookup")[0].payload["result"]["count"] == 87
    assert any("tools" in f for f in t.fallbacks)
    assert "87" in m.requests[3]["messages"][-2]["content"]  # result fed back as a plain message


def test_response_format_rejected_falls_back_to_plain_json(pg):
    m = Model(text("r"), text("done"), httpx.Response(400, text="response_format unsupported"),
              contract(80, 90, PLAN_SAFE), sql(SAFE), js(GREEN))
    t = run(m)
    assert m.requests[2]["response_format"]["type"] == "json_schema" and "response_format" not in m.requests[3]
    assert "response_format" not in m.requests[4]  # stays off for the run
    assert t.verdict.green and any("response_format" in f for f in t.fallbacks)


def test_bad_request_without_shape_params_still_raises(pg):
    with pytest.raises(ModelRequestError):
        run(Model(httpx.Response(400, text="bad model id")))


def test_bad_request_twice_raises(pg):
    with pytest.raises(ModelRequestError):
        run(Model(text("r"), httpx.Response(400, text="bad model"), httpx.Response(400, text="bad model")))


@pytest.mark.parametrize("wrap", ["```json\n{}\n```", "Here you go:\n{}", "<think>maybe {{}}</think>{}"])
def test_contract_json_wrapped_in_prose_still_parses(pg, wrap):
    body = contract(80, 90, PLAN_SAFE)["choices"][0]["message"]["content"]
    m = Model(text("r"), text("done"), text(wrap.format(body)), sql(SAFE), js(GREEN))
    assert run(m).verdict.green


@pytest.mark.parametrize("reply", ["```sql\n{}\n```", "{}"])
def test_sql_without_json_wrapper_still_runs(pg, reply):
    m = Model(text("r"), text("done"), contract(80, 90, PLAN_SAFE), text(reply.format(SAFE)), js(GREEN))
    assert run(m).sql == SAFE


def test_tool_arguments_given_as_object(pg):
    m = e8_model()
    m.script[1]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = {"select_sql": LOOK_ALL}
    assert steps(run(m), "lookup")[0].payload["result"]["count"] == 90


def test_malformed_tool_arguments_are_fed_back(pg):
    bad = tool("lookup", select_sql="x")
    bad["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
    m = Model(text("r"), bad, text("done"), contract(80, 90, PLAN_SAFE), sql(SAFE), js(GREEN))
    run(m)
    assert "error" in [x for x in m.requests[2]["messages"] if x["role"] == "tool"][0]["content"]


# ---- red paths that must stay red, never crash ----
def test_failed_sql_is_never_green_even_if_zero_rows_were_promised(pg):
    q = "UPDATE invoices SET status = 'x' BADSQL"
    m = Model(text("r"), text("done"), contract(0, 100), sql(q), text("done"), contract(0, 100), sql(q), text("done"),
              contract(0, 100), sql(q), js({"explanation": "It failed.", "reply": "Not done."}))
    t = run(m)
    assert not t.verdict.green and t.verdict.failures[0].detail.startswith("SQL failed")
    fed = steps(t, "revise")[0].payload["fed_back"][0]
    assert "syntax error" in fed and "secret-row-value" not in json.dumps(m.requests)  # DETAIL lines hold row values


def test_bad_scope_rule_is_fed_back_not_a_crash(pg):
    m = Model(text("r"), text("done"), contract(80, 90, "BADSCOPE = 1"), sql(SAFE), text("done"),
              contract(80, 90, PLAN_SAFE), sql(SAFE), js(GREEN))
    t = run(m)
    assert "scope rule failed" in steps(t, "revise")[0].payload["fed_back"][0] and t.verdict.green


def test_schema_changed_is_named_red_ddl(pg):
    q = NAIVE + " AND 'DROPME' = 'DROPME'"
    m = Model(text("r"), text("done"), contract(), sql(q), text("done"), contract(80, 90, PLAN_SAFE), sql(SAFE), js(GREEN))
    t = run(m)
    v = t.verdicts[0]
    assert [f.kind for f in v.failures] == ["ddl"] and "schema changed" in v.failures[0].detail
    assert any(x["kind"] == "ddl" and not x["ok"] for x in steps(t, "compare")[0].payload["lines"])


def test_ddl_attempt_is_named_red(pg):
    m = Model(text("r"), text("done"), contract(), sql("DROP TABLE invoices"), text("done"),
              contract(80, 90, PLAN_SAFE), sql(SAFE), js(GREEN))
    t = run(m)
    assert "ddl" in [f.kind for f in t.verdicts[0].failures]


def test_trace_encodes_for_the_api(pg):
    """orch/runs.py feeds the trace through jsonable_encoder; UI shapes from ui-api-contract.md."""
    runs = pytest.importorskip("orch.runs")  # Task 6 is not committed yet
    t = run(e8_model())
    v = runs._trace_view(t)
    json.dumps(v)
    by = {}
    for s in v["steps"]:
        by.setdefault(s["name"], s)
    assert by["read"]["tokens"] == {"in": 100, "out": 10} and by["run"]["tokens"] is None
    assert by["read"]["payload"] == {"text": "Write off the overdue open invoices."}
    assert set(by["contract"]["payload"]) >= {"version", "contract", "sha256", "locked_at"}
    assert by["sql"]["payload"] == {"sql": NAIVE}
    assert set(by["compare"]["payload"]) >= {"attempt", "green", "command_tag", "warnings", "failures", "lines", "effect"}
    assert by["compare"]["payload"]["effect"]["money"][0]["key"]
    assert {by["read"]["actor"], by["lookup"]["actor"], by["compare"]["actor"]} == {"model", "sandbox", "drydock"}
    line = by["compare"]["payload"]["lines"][0]
    assert set(line) >= {"kind", "ok", "title", "detail", "lookup", "promised", "measured", "sql", "before", "after"}
    failed = [f for f in by["compare"]["payload"]["failures"] if f["kind"] == "policy"][0]
    assert failed["detail"].startswith([x for x in by["compare"]["payload"]["lines"] if x["kind"] == "policy" and not x["ok"]][0]["title"])
    assert runs._strip({"box_id": "b"}, v["steps"])[0]["what"] == "lookup"


def test_prompt_has_no_policy_text_or_model_quirks():
    prompt = (ROOT / "llm" / "prompt.txt").read_text(encoding="utf-8")
    assert not any(p["text"] in prompt for p in measure.POLICIES) and "P-7" not in prompt
    src = (ROOT / "llm" / "agent.py").read_text(encoding="utf-8")
    assert "httpx" not in src and "glm" not in src.lower() and "api.vultrinference" not in src
    for g in GOLDEN.values():  # few-shot examples are "none from this set" (§2)
        assert g["sql"] not in prompt


# ---- runner: the real command tag (psql without -q), default path unchanged ----
def _fake_run(stdout, returncode=0, stderr=""):
    seen = []

    def r(cmd, timeout=None, limit=None):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    return r, seen


@pytest.mark.parametrize("stdout,tag", [("UPDATE 87\n", "UPDATE 87"), ("INSERT 0 1\n", "INSERT 0 1"),
                                        ("7\n8\nDELETE 2\n", "DELETE 2"), ("", None), ("ABC\n", None)])
def test_run_sql_command_tag(monkeypatch, stdout, tag):
    fake, seen = _fake_run(stdout)
    monkeypatch.setattr(runner, "_capture", fake)
    r = runner.run_sql(make_box(), "UPDATE invoices SET status = 'void'", tag=True)
    assert r.command_tag == tag and "-q" not in seen[0]


def test_run_sql_default_stays_quiet(monkeypatch):
    fake, seen = _fake_run("t\n")
    monkeypatch.setattr(runner, "_capture", fake)
    r = runner.run_sql(make_box(), "SELECT true")
    assert r.command_tag is None and r.stdout == "t\n" and "-q" in seen[0]


# ---- Docker-gated: the same loop on a real sandbox. NOT RUN until Docker is up. ----
def _docker_up():
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(scope="module")
def real_template(tmp_path_factory):
    if not _docker_up():
        pytest.skip("Docker unreachable")
    from sandbox.template import refresh_template
    from seed import seed as S
    p = tmp_path_factory.mktemp("seed") / "seed.sql"
    p.write_text(S.build_sql(), encoding="utf-8")
    refresh_template(p)


@pytest.fixture
def real_box(real_template, monkeypatch):
    from sandbox.container import destroy, start_run_container
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test/v1")  # dummy host: the transport is faked, nothing is called
    monkeypatch.setenv("DRYDOCK_MODEL", "fixture-model-1")
    b = start_run_container()
    yield b
    destroy(b)


def test_docker_lookup_wrapper_returns_numbers(real_box):
    r, nums = agent.lookup(real_box, f"SELECT * FROM invoices WHERE {OVERDUE}")
    assert r.ok and nums["count"] == 90 and nums["sums"]["amount_cents"] > 0
    assert all(isinstance(v, (int, float)) for v in nums["sums"].values())
    assert "status" not in nums["sums"]  # text columns never come back
    # live E1 2026-11-07: "sums": {"invoice_id": 202} for two rows of invoice 101 sent the model to invoice 202
    assert not any(k == "id" or k.endswith("_id") for k in nums["sums"])
    r, nums = agent.lookup(real_box, "UPDATE invoices SET status = 'void' RETURNING id")
    assert not r.ok and nums is None and r.code == "25006"


def test_docker_real_command_tag(real_box):
    r = runner.run_sql(real_box, GOLDEN["E8"]["sql"], tag=True)
    assert r.ok and r.command_tag == "UPDATE 75"  # the golden's outer statement lowers 75 account balances
    r = runner.run_sql(real_box, "UPDATE invoices SET status = status WHERE id = 1 RETURNING id", tag=True)
    assert r.command_tag == "UPDATE 1"
    r = runner.run_sql(real_box, GOLDEN["H1"]["sql"], tag=True)
    assert r.command_tag == "INSERT 0 1"


def test_docker_recorded_e8_on_real_sandbox(real_box):
    t = run_agent(TICKETS["E8"], real_box, transport=e8_real_checks().transport)
    s = run_eval.score("E8", GOLDEN["E8"], t)
    assert s["passed"] and s["p7_first"] and t.revisions == 1
    assert steps(t, "run")[0].payload["command_tag"] == "UPDATE 90"
    assert steps(t, "run")[1].payload["command_tag"] == "UPDATE 75"  # golden SQL: 87 invoices, then 75 balances
    assert steps(t, "lookup")[0].payload["result"]["count"] == 90


def e8_real_checks():
    checks = [{"name": "written_off_87", "sql": "SELECT count(*) = 87 FROM invoices WHERE status = 'written_off' AND due_on <= DATE '2026-08-03'"},
              {"name": "none_recent", "sql": "SELECT count(*) = 0 FROM invoices WHERE status = 'written_off' AND due_on > DATE '2026-08-03'"}]
    v1 = [dict(checks[0], sql=checks[0]["sql"].replace("= 87", ">= 87")), checks[1]]
    return Model(text("Write off the overdue open invoices."), tool("lookup", select_sql=LOOK_ALL),
                 contract(85, 95, OVERDUE, v1), sql(NAIVE),
                 # the naive SQL also breaks P-5 (balances not lowered), so the model reads both named rules before the lookup
                 tool("lookup_policy", policy_id="P-7"), tool("lookup_policy", policy_id="P-5"), tool("lookup", select_sql=LOOK_SAFE),
                 # v2 is the golden: it also lowers balances (P-5), so the contract names both tables, as live green runs did
                 js({"tables": ["invoices", "accounts"], "expected_rows": {"invoices": [80, 90], "accounts": [1, 90]},
                     "scope_rule": {"table": "invoices", "where": PLAN_SAFE}, "checks": checks}),
                 sql(GOLDEN["E8"]["sql"]), js(E8_REPLY))


def test_docker_recorded_e11_really_stops(real_box):
    t = run_agent(TICKETS["E11"], real_box, transport=e11_real().transport)
    s = run_eval.score("E11", GOLDEN["E11"], t)
    assert s["passed"] and s["cap"] in GOLDEN["E11"]["expected_stop"]
    assert t.effect.rows_by_table == {} and t.revisions == 0


def e11_real():
    bad = GOLDEN["E11"]["sql"]
    ch = [{"name": "a", "sql": "SELECT count(*) > 0 FROM accounts WHERE usage_cents_oct > 0"},
          {"name": "b", "sql": "SELECT count(*) >= 0 FROM accounts WHERE usage_cents_oct >= 0"}]
    c = contract(1, 1000, "id > 0", ch, "accounts")
    return Model(text("r"), text("done"), c, sql(bad), js(E11_REPLY))


def test_docker_constant_or_bad_check_never_green(real_box):
    m = Model(text("r"), tool("lookup", select_sql=LOOK_SAFE),
              contract(80, 90, PLAN_SAFE, [{"name": "fails", "sql": "SELECT count(*) = -1 FROM invoices WHERE status = 'written_off'"},
                                           {"name": "writes", "sql": "UPDATE invoices SET status = 'void' FROM invoices i RETURNING true"}]),
              sql(SAFE), text("done"), contract(80, 90, PLAN_SAFE, [{"name": "fails", "sql": "SELECT count(*) = -1 FROM invoices WHERE status = 'written_off'"},
                                                                     {"name": "ok", "sql": "SELECT count(*) = 87 FROM invoices WHERE status = 'written_off' AND due_on <= DATE '2026-08-03'"}]),
              sql(SAFE), text("done"), contract(80, 90, PLAN_SAFE, [{"name": "fails", "sql": "SELECT count(*) = -1 FROM invoices WHERE status = 'written_off'"},
                                                                     {"name": "ok", "sql": "SELECT count(*) = 87 FROM invoices WHERE status = 'written_off' AND due_on <= DATE '2026-08-03'"}]),
              sql(SAFE), js({"explanation": "A check failed.", "reply": "Not done."}))
    t = run_agent(TICKETS["E8"], real_box, transport=m.transport)
    assert not t.verdict.green and any(f.kind == "check" for f in t.verdict.failures)



# ---- audit-1107: tokens per run, run budget ----
def _chars(m):
    return sum(len(json.dumps(r)) for r in m.requests)




def test_e8_is_nine_calls_and_well_under_the_old_size(pg):
    m = e8_model()
    run(m)
    assert len(m.requests) == 9 and _chars(m) < 55000, (len(m.requests), _chars(m))


def test_no_done_round_trip_after_a_successful_lookup(pg):
    m = Model(text("r"), tool("lookup", select_sql=LOOK_SAFE), contract(80, 90, PLAN_SAFE), sql(SAFE), js(GREEN))
    assert run(m).verdict.green and not m.script
    assert m.requests[2]["response_format"]["json_schema"]["name"] == "change_contract"


def test_failed_lookup_may_try_again(pg):
    m = Model(text("r"), tool("lookup", select_sql="SELECT 1; SELECT 2"), tool("lookup", select_sql=LOOK_SAFE),
              contract(80, 90, PLAN_SAFE), sql(SAFE), js(GREEN))
    assert len(steps(run(m), "lookup")) == 2 and not m.script


def test_read_call_does_not_carry_the_schema_or_examples(pg):
    m = e8_model()
    run(m)
    sent = json.dumps(m.requests[0]["messages"])
    assert "CREATE TABLE" not in sent and "Worked examples" not in sent
    assert "CREATE TABLE" in json.dumps(m.requests[1]["messages"])


def test_examples_only_on_the_first_contract_pass(pg):
    m = e8_model()
    run(m)
    has = ["Worked examples" in json.dumps(r["messages"]) for r in m.requests]
    assert has == [False, False, True, True, False, False, False, False, False]


def test_revise_drops_tool_chatter_and_keeps_what_the_model_needs(pg):
    m = e8_model()
    run(m)
    revise = m.requests[4]["messages"]
    assert [x["role"] for x in revise] == ["system", "user", "assistant", "user"]
    last = revise[-1]["content"]
    assert LOOK_ALL in last and "90" in last and NAIVE in last and "P-7: violations 0" in last
    assert "expected_rows" in last


def test_explain_failure_keeps_the_measured_result(pg):
    m = e8_model()
    m.script[-1:] = [httpx.Response(503, text="down")] * 6
    t = run(m)
    assert t.verdict.green and t.effect.rows_by_table["invoices"]["touched"] == 87
    assert "invoices.status" in t.explanation and t.reply
    assert steps(t, "explain")[0].payload["rejected"]


def test_run_token_budget_stops_the_agent(pg, monkeypatch):
    monkeypatch.setenv("DRYDOCK_RUN_TOKENS", "200")  # each scripted call spends 110
    with pytest.raises(ModelUnavailable) as e:
        run(e8_model())
    assert "token budget" in e.value.public


def test_run_deadline_stops_before_sandbox_work(pg, monkeypatch):
    from llm import client
    now = [0.0]
    monkeypatch.setattr(client, "_now", lambda: now[0])
    m = Model(text("r"), tool("lookup", select_sql=LOOK_ALL))
    real = m.__call__

    def slow(request):
        now[0] += 500
        return real(request)
    with pytest.raises(ModelUnavailable) as e:
        run_agent(TICKETS["E8"], make_box(), transport=httpx.MockTransport(slow))
    assert "time limit" in e.value.public
    assert not any("WITH q AS" in q for _, q in pg.sql_log)


# ---- audit-1107 logic: B3, B6, B7, B8, B9, B14/S4, B20, S1 ----
def test_b6_a_cap_ends_the_loop_with_no_revise(pg):
    bad = GOLDEN["E11"]["sql"]
    ch = [{"name": "a", "sql": "SELECT count(*) > 0 FROM accounts WHERE usage_cents_oct >= 0"},
          {"name": "b", "sql": "SELECT count(*) = 2 FROM accounts WHERE usage_cents_oct >= 0"}]
    # a narrower, approvable revise is scripted next: it must never be asked for
    m = Model(text("r"), text("done"), contract(2, 2, "id > 0", ch, "accounts"), sql(bad), js(E11_REPLY),
              text("done"), contract(80, 90, PLAN_SAFE), sql(SAFE))
    t = run(m, "E11")
    assert t.revisions == 0 and len(steps(t, "run")) == 1 and not steps(t, "revise")
    assert t.approval_class == "C" and t.capped == "disk spill cap" and len(m.script) == 3


def capped_lookup_then_green(reply):
    return Model(text("r"), tool("lookup", select_sql="SELECT amount_cents FROM usage_events"), text("done"),
                 contract(80, 90, PLAN_SAFE), sql(SAFE), *reply)


def test_b3_capped_lookup_then_green_is_locked(pg):
    t = run(capped_lookup_then_green([js({"explanation": "A lookup was stopped by the disk spill cap, so this is locked.",
                                          "reply": "Not done."})]))
    assert t.verdict.green and t.capped == "disk spill cap"
    assert t.approval_class == "C" and "disk spill cap" in t.approval_reason
    assert approval(t.verdict, t, "ticket")[0] == "C"
    assert run_eval.score("E8", GOLDEN["E8"], t)["cap"] is None  # M6: the eval still ignores lookups for the E11 cap


def test_b3_code_text_for_a_capped_green_run_does_not_say_waiting_for_approval(pg):
    weak = js({"explanation": "It worked.", "reply": "Done."})
    t = run(capped_lookup_then_green([weak, weak]))
    assert "disk spill cap" in t.explanation and "waiting for approval" not in t.reply


def test_b7_two_policy_calls_in_one_reply(pg):
    two = _resp({"content": None, "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": "lookup_policy", "arguments": json.dumps({"policy_id": "P-7"})}}
        for i in (1, 2)]}, "tool_calls")
    t = run(Model(*first_attempt(), two, tool("lookup", select_sql=LOOK_SAFE), contract(80, 90, PLAN_SAFE), sql(SAFE),
                  js(E8_REPLY)))
    assert len(steps(t, "lookup_policy")) == 2 and t.verdict.green
    assert sum(s.tokens or 0 for s in t.steps) == 9 * 110


def test_b7_policy_id_that_is_not_a_string(pg):
    t = run(Model(*first_attempt(), tool("lookup_policy", policy_id=["P-7"]), text("done"), contract(80, 90, PLAN_SAFE),
                  sql(SAFE), js(E8_REPLY)))
    assert "error" in steps(t, "lookup_policy")[0].payload


@pytest.mark.parametrize("q", [
    LOOK_SAFE,
    LOOK_SAFE.replace("FROM invoices", "FROM public.invoices"),
    LOOK_SAFE.replace("FROM invoices", 'FROM "invoices"'),
    "SELECT a.id FROM accounts a JOIN invoices i ON i.account_id = a.id WHERE " + PLAN_SAFE,
    "SELECT extract(month FROM paid_on) FROM payments p, invoices WHERE " + PLAN_SAFE,
])
def test_b8_lookup_phrasing_never_flips_the_warning(pg, q):
    t = run(Model(text("r"), tool("lookup", select_sql=q), contract(80, 90, PLAN_SAFE), sql(SAFE), js(GREEN)))
    assert t.verdict.warnings == []


def test_b8_a_table_named_only_in_a_literal_does_not_back_the_range(pg):
    q = "SELECT id FROM accounts WHERE code <> 'invoices'"
    t = run(Model(text("r"), tool("lookup", select_sql=q), contract(80, 90, PLAN_SAFE), sql(SAFE), js(GREEN)))
    assert "range not backed by a lookup: invoices [80,90]" in t.verdict.warnings


def test_b9_every_box_is_named_and_its_destroy_recorded(pg, monkeypatch):
    stamp = datetime(2026, 11, 7, tzinfo=timezone.utc)
    monkeypatch.setattr(agent, "destroy", lambda b: stamp)
    t = run(e8_model(), box=make_box("box-first"))
    assert [(s.payload["container_id"]) for s in steps(t, "lookup")] == ["box-first", "box-fresh-1"]
    assert [b["id"] for b in t.boxes] == ["box-fresh-1"] and t.boxes[0]["destroyed_at"] == stamp


def test_b9_failed_destroy_is_not_claimed(pg, monkeypatch):
    def boom(b):
        raise RuntimeError("docker gone")
    monkeypatch.setattr(agent, "destroy", boom)
    assert run(e8_model()).boxes[0]["destroyed_at"] is None


def test_s4_provider_text_never_reaches_the_browser(pg):
    runs = pytest.importorskip("orch.runs")
    m = Model(text("r"), text("done"), httpx.Response(400, text="SECRET-UPSTREAM-BODY"), contract(80, 90, PLAN_SAFE),
              sql(SAFE), js(GREEN))
    t = run(m)
    assert t.fallbacks == ["dropped response_format"]
    assert "SECRET-UPSTREAM-BODY" not in json.dumps(runs._trace_view(t))


def test_s4_run_step_keeps_the_code_but_not_stdout_or_detail_lines(pg):
    q = "UPDATE invoices SET status = 'x' BADSQL"
    t = run(Model(text("r"), text("done"), contract(0, 100), sql(q), text("done"), contract(0, 100), sql(q), text("done"),
                  contract(0, 100), sql(q), js({"explanation": "It failed.", "reply": "Not done."})))
    r = steps(t, "run")[0].payload["result"]
    assert r.code == "42601" and r.stdout == "" and "secret-row-value" not in r.error


def test_b20_raw_newline_in_json_string_still_parses(pg):
    raw = '{"sql": "UPDATE invoices SET status = \'written_off\'\n WHERE ' + PLAN_SAFE + '"}'
    m = Model(text("r"), text("done"), contract(80, 90, PLAN_SAFE), text(raw), js(GREEN))
    assert run(m).sql.startswith("UPDATE invoices")


# ---- S1: the Task 8 filter ----
@pytest.mark.parametrize("bad", ["SET statement_timeout = 0", "RESET ALL", "ALTER ROLE executor SET work_mem = '1GB'",
                                 "DO $$ BEGIN NULL END $$", "SELECT 1", "COPY invoices TO STDOUT", "CALL x()",
                                 "UPDATE invoices SET status = set_config('a', 'b', false)",
                                 "UPDATE invoices SET status = \"set_config\"('a', 'b', false)",
                                 "UPDATE invoices SET status = pg_sleep(9)::text",
                                 "INSERT INTO credits (reason) SELECT lo_from_bytea(0, 'x')::text",
                                 "-- note\nSET statement_timeout = 0", "/* x */ DO $$ BEGIN NULL END $$"])
def test_s1_run_step_refuses_before_the_sandbox(pg, bad):
    m = Model(text("r"), text("done"), contract(80, 90, PLAN_SAFE), sql(bad), text("done"), contract(80, 90, PLAN_SAFE),
              sql(SAFE), js(GREEN))
    t = run(m)
    assert not any(q == bad.strip() for _, q in pg.sql_log) and t.revisions == 1 and t.verdict.green
    assert "SQL rejected" in steps(t, "revise")[0].payload["fed_back"][0]


@pytest.mark.parametrize("good", [g["sql"] for g in GOLDEN.values()] + [
    "UPDATE credits SET reason = 'set_config(' WHERE id = 1", "UPDATE t SET a = 1 -- SET b\nWHERE false"])
def test_s1_goldens_and_literals_pass(good):
    assert agent.refused(good, agent.RUN_LEAD) is None


@pytest.mark.parametrize("bad", ["SELECT pg_sleep(60)", "UPDATE invoices SET status = 'void' RETURNING id", "SET x = 1"])
def test_s1_lookup_refuses(pg, bad):
    t = run(Model(text("r"), tool("lookup", select_sql=bad), text("done"), contract(80, 90, PLAN_SAFE), sql(SAFE), js(GREEN)))
    assert steps(t, "lookup")[0].payload["error"] and not any("WITH q AS" in q for _, q in pg.sql_log)


@pytest.mark.parametrize("field", ["check", "scope"])
def test_s1_contract_text_refused(pg, field):
    checks = [{"name": "c1", "sql": "SELECT set_config('a','b',false) = 'b' AND status = 'x' FROM invoices"}, CHECKS[0]]
    bad = contract(80, 90, PLAN_SAFE, checks) if field == "check" else contract(80, 90, PLAN_SAFE + " AND pg_sleep(9) IS NULL")
    m = Model(text("r"), text("done"), bad, text("done"), contract(80, 90, PLAN_SAFE), sql(SAFE), js(GREEN))
    t = run(m)
    assert t.revisions == 1 and "contract rejected" in steps(t, "revise")[0].payload["fed_back"][0]


def test_b21_rejected_attempt_does_not_keep_the_old_effect():
    """The explanation must not read attempt 1's measured change beside a later attempt's rejection."""
    r = agent._Run.__new__(agent._Run)
    r.t = agent.RunTrace(effect=agent.Effect({"invoices": {"count_delta": 0, "touched": 9}}, {}, {}, [], {}))
    assert r.rejected("contract rejected: bad") == ["contract rejected: bad"]
    assert r.t.effect == agent.EMPTY_EFFECT and r.t.verdict.green is False


def test_prompt_says_old_violations_are_not_the_models_to_fix():
    """Live E8 2026-11-07 run 1: golden-equivalent SQL went red on the model's own check that every account balance
    matched, which one pre-existing P-5 row (before = 1) made impossible. Checks must test the change, not old rows."""
    assert "not old rows you must leave alone" in agent.PROMPT["CONTRACT"]
    assert "are not yours to fix" in agent.PROMPT["REVISE"]
    assert "do not restate a policy as a check" in agent.PROMPT["CONTRACT"]  # live E8 run 3: correct SQL, red own check
    assert "pass them on with RETURNING" in agent.PROMPT["SQL"]  # live E8: CTE read pre-update invoices, P-5 1 -> 75
