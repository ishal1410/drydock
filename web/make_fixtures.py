"""Build web/src/fixtures.json for the UI's fixture mode (?fixture) from the real seed.

Every row count, id, name and dollar figure is computed from seed.seed.build_rows() and asserted against
seed/golden.json. Model-side text (contracts, explanations, replies), timings, tokens and container ids are
SAMPLE values so the screens render without a backend; the UI labels the whole mode "fixture".
Run: .venv/Scripts/python -m web.make_fixtures   (from the repo root)
"""
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from seed.seed import ASOF, build_rows

ROOT = Path(__file__).resolve().parent.parent
GOLD = json.loads((ROOT / "seed" / "golden.json").read_text(encoding="utf-8"))
TICKETS = [t for t in json.loads((ROOT / "seed" / "tickets.json").read_text(encoding="utf-8")) if t["demo"]]
POLICY_TEXT = {"P-1": "An account with open invoices is never closed.",
               "P-2": "A charge is never refunded twice.",
               "P-3": "An invoice's paid amount never exceeds its amount.",
               "P-4": "A paid invoice keeps net charges at or above its amount.",
               "P-5": "An account's balance equals what is still owed on its open invoices.",
               "P-6": "A closed account has no active subscription.",
               "P-7": "Invoices on an active payment plan are never written off."}
COLS = {"customers": ["id", "name", "email", "status"],
        "accounts": ["id", "customer_id", "code", "status", "balance_cents", "opened_on", "usage_cents_oct"],
        "subscriptions": ["id", "account_id", "plan", "status", "monthly_cents"],
        "invoices": ["id", "number", "account_id", "subscription_id", "amount_cents", "paid_cents", "issued_on",
                     "due_on", "status"],
        "payments": ["id", "invoice_id", "amount_cents", "paid_on", "method", "kind", "refund_of"],
        "payment_plans": ["id", "invoice_id", "installments", "status"],
        "credits": ["id", "account_id", "amount_cents", "reason", "created_on"]}
MODEL = "sample-model"    # SAMPLE: no model ran for any fixture
T_MS, MEM_CAP = 5000, 512 * 1024 * 1024  # sandbox/container.py STATEMENT_TIMEOUT 5s, MEMORY 512m
T0 = datetime(2026, 11, 5, 14, 2, 11, tzinfo=timezone.utc)

R = build_rows()
D = {t: [dict(zip(COLS[t], r)) for r in rows] for t, rows in R.items()}
by_id = {t: {r["id"]: r for r in rows} for t, rows in D.items()}
cust_of_acct = {a["id"]: by_id["customers"][a["customer_id"]]["name"] for a in D["accounts"]}


def usd(c):
    return f"${c / 100:,.2f}"


def checksum(d):
    return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()


def pg_round(x):  # Postgres round(numeric) = half away from zero
    return int(Decimal(str(x)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def ts(ms):
    return (T0 + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def cid(seed):
    return hashlib.sha1(seed.encode()).hexdigest()[:12]


def sample(table, pairs, label_col):
    out = []
    for before, after in pairs[:5]:
        changed = [c for c in COLS[table] if (before or {}).get(c) != (after or {}).get(c)]
        out.append({"pk": (after or before)["id"], "op": "insert" if before is None else "update",
                    "label": (after or before).get(label_col), "before": before, "after": after, "changed": changed})
    return out


def jsonable(o):
    return json.loads(json.dumps(o, default=str))


# ---------- computed effects (asserted against golden.json) ----------
overdue = [i for i in D["invoices"] if i["status"] == "open" and i["due_on"] <= ASOF - timedelta(days=90)]
active_plan_inv = {p["invoice_id"] for p in D["payment_plans"] if p["status"] == "active"}
e8_set = [i for i in overdue if i["id"] not in active_plan_inv]
e8_p7 = [i for i in overdue if i["id"] in active_plan_inv]
assert len(e8_set) == GOLD["E8"]["expected"]["invoices"]["touched"]
assert sum(i["amount_cents"] for i in e8_set) == GOLD["E8"]["written_off_cents"]
p7_rows = []
for i in e8_p7:
    s = by_id["subscriptions"][i["subscription_id"]]
    p7_rows.append({"customer": cust_of_acct[i["account_id"]], "invoice": i["number"],
                    "owed_cents": i["amount_cents"] - i["paid_cents"],
                    "arr_cents": 12 * s["monthly_cents"] if s["status"] == "active" else 0})
assert sum(r["owed_cents"] for r in p7_rows) == GOLD["E8"]["p7_protected_cents"]


def owed_by_acct(invs):  # harsh F3: each write-off also comes off its account's balance (P-5)
    out = {}
    for i in invs:
        out[i["account_id"]] = out.get(i["account_id"], 0) + i["amount_cents"] - i["paid_cents"]
    return out


e8_owed, naive_owed = owed_by_acct(e8_set), owed_by_acct(overdue)
assert len(e8_owed) == GOLD["E8"]["expected"]["accounts"]["touched"]
assert sum(r["arr_cents"] for r in p7_rows) == GOLD["E8"]["p7_arr_cents"]

e6_rows = [s for s in D["subscriptions"] if s["plan"] == "enterprise" and s["status"] == "active"]
e6_before = sum(s["monthly_cents"] for s in e6_rows)
e6_after = sum(pg_round(s["monthly_cents"] * 0.9) for s in e6_rows)
assert len(e6_rows) == GOLD["E6"]["expected"]["subscriptions"]["touched"]
assert e6_before - e6_after == GOLD["E6"]["expected_amount_cents"]

e1_charge = by_id["payments"][2565]
assert e1_charge["amount_cents"] == GOLD["E1"]["expected_amount_cents"]
e1_refund = {**e1_charge, "id": max(by_id["payments"]) + 1, "paid_on": ASOF, "kind": "refund", "refund_of": 2565}
e1_inv = by_id["invoices"][e1_charge["invoice_id"]]

e2_acct = by_id["accounts"][7]
e2_new = sum(i["amount_cents"] - i["paid_cents"] for i in D["invoices"] if i["account_id"] == 7 and i["status"] == "open")
assert e2_new == GOLD["E2"]["expected_amount_cents"]

e5_accts = sorted([a for a in D["accounts"] if a["customer_id"] == 201], key=lambda a: (a["opened_on"], a["id"]))
e5_closed = e5_accts[1:]
e5_subs = [s for s in D["subscriptions"] if s["account_id"] in {a["id"] for a in e5_closed} and s["status"] == "active"]
assert len(e5_closed) == 2 and len(e5_subs) == 2
assert sum(s["monthly_cents"] for s in e5_subs) == GOLD["E5"]["expected_amount_cents"]

SEED_SUM = checksum(D)


def applied(ticket):
    d = copy.deepcopy(D)
    ix = {t: {r["id"]: r for r in rows} for t, rows in d.items()}
    if ticket == "E8":
        for i in e8_set:
            ix["invoices"][i["id"]]["status"] = "written_off"
        for a, owed in e8_owed.items():
            ix["accounts"][a]["balance_cents"] -= owed
    elif ticket == "E6":
        for s in e6_rows:
            ix["subscriptions"][s["id"]]["monthly_cents"] = pg_round(s["monthly_cents"] * 0.9)
    elif ticket == "E1":
        d["payments"].append(e1_refund)
    elif ticket == "E2":
        ix["accounts"][7]["balance_cents"] = e2_new
    elif ticket == "E5":
        for a in e5_closed:
            ix["accounts"][a["id"]]["status"] = "closed"
        for s in e5_subs:
            ix["subscriptions"][s["id"]]["status"] = "cancelled"
    return checksum(d)


# ---------- run builder ----------
class Run:
    def __init__(self, ticket_id):
        self.t = next(t for t in TICKETS if t["id"] == ticket_id)
        self.box = cid("run-" + ticket_id)
        self.steps, self.strip, self.clock = [], [], 0

    def step(self, name, actor, payload, ms, tokens=None, service=None):
        n = len(self.steps) + 1
        svc = service or {"model": f"sample provider, {MODEL}", "sandbox": f"sandbox container {self.box}",
                          "drydock": "orchestrator"}[actor]
        s = {"n": n, "name": name, "actor": actor, "service": svc, "started_at": ts(self.clock), "ms": ms,
             "tokens": tokens, "model_id": MODEL if actor == "model" else None, "payload": payload}
        self.clock += ms + 40
        self.steps.append(s)
        return s

    def stmt(self, what, ms, mem_mb, stopped_by=None, code=None):
        self.strip.append({"step": len(self.steps), "what": what, "role": "executor", "container_id": self.box,
                           "elapsed_ms": ms, "cap_ms": T_MS, "temp_cap": "64MB",
                           "mem_peak_bytes": int(mem_mb * 1024 * 1024), "mem_cap_bytes": MEM_CAP,
                           "stopped_by": stopped_by, "code": code})

    def lookup(self, sql, table, count, sums, ms=38, mem=41.2):
        self.step("lookup", "sandbox", {"sql": sql, "table": table, "result": {"count": count, "sums": sums}}, ms)
        self.stmt("lookup", ms, mem)

    def run(self, tag, ms=54, mem=44.8, stop=None):
        p = {"command_tag": tag, "elapsed_ms": ms, "cap_ms": T_MS, "ok": stop is None}
        if stop:
            p.update(stop)
        self.step("run", "sandbox", p, ms)
        self.stmt("sql", ms, mem, stop and stop["stopped_by"], stop and stop["code"])

    def finish(self, verdict, cls, reason, explanation, reply, revisions, contract_sha, locked_at, stopped=None,
               after_sum=None):
        destroyed = ts(self.clock + 120)
        if stopped:
            stopped["destroyed_at"] = destroyed  # one time per container, whichever card reads it
        chips = [{"name": "Server-side COPY denied", "ok": True, "detail": "COPY … TO PROGRAM as executor → 42501"},
                 {"name": "Metadata unreachable, no credentials", "ok": True,
                  "detail": "169.254.169.254 unreachable, no password in env or config"},
                 {"name": "Caps in force", "ok": True,
                  "detail": "--memory 512m, --cpus 1, --pids-limit 128, network none, timeout 5s, temp 64MB"},
                 {"name": "Container destroyed", "ok": True, "detail": f"{self.box} at {destroyed}"}]
        ledger = [{"service": "Sample VM row", "detail": "orchestrator, run state, receipts volume"},
                  {"service": "Model provider: sample", "detail": f"{MODEL}, "
                   f"{sum((s['tokens'] or {}).get('in', 0) for s in self.steps):,} tokens in / "
                   f"{sum((s['tokens'] or {}).get('out', 0) for s in self.steps):,} out, "
                   f"{sum(s['ms'] for s in self.steps if s['actor'] == 'model'):,} ms"},
                  {"service": "Sandbox container", "detail": f"{self.box}, executor role, created {ts(0)}, "
                   f"destroyed {destroyed}"},
                  {"service": "Live-db container", "detail": "untouched until Approve, same flags and role"}]
        return {"run_id": "fx-" + self.t["id"].lower(), "ticket": self.t, "source_kind": "ticket", "status": "done",
                "trace": {"steps": self.steps, "revisions": revisions, "contract_sha": contract_sha,
                          "contract_locked_at": locked_at},
                "verdict": verdict, "approval_class": {"class": cls, "reason": reason}, "chips": chips,
                "strip": self.strip, "destroyed_at": destroyed, "stopped": stopped, "ledger": ledger,
                "explanation": explanation, "reply": reply,
                "live_checksum": SEED_SUM, "apply_checksum_after": after_sum}


def contract_step(r, version, contract, ms=1810, tokens=(3120, 286), diff=None):
    body = json.dumps(contract, sort_keys=True)
    sha = hashlib.sha256(body.encode()).hexdigest()
    p = {"version": version, "contract": contract, "sha256": sha, "locked_at": ts(r.clock + ms)}
    if diff:
        p["diff"] = diff
    r.step("contract", "model", p, ms, {"in": tokens[0], "out": tokens[1]})
    return sha, p["locked_at"]


def rowline(table, lookup, lo, hi, measured):
    return {"kind": "row_count", "ok": lo <= measured <= hi, "title": f"{table} rows changed",
            "lookup": lookup, "promised": [lo, hi], "measured": measured}


def std_lines(tables, scope_ok, scope_detail, checks, policies):
    lines = [{"kind": "tables", "ok": True, "title": "Only declared tables changed", "detail": ", ".join(tables)},
             {"kind": "scope", "ok": scope_ok, "title": "Every change inside scope_rule", "detail": scope_detail}]
    lines += [{"kind": "check", "ok": ok, "title": name, "sql": sql} for name, sql, ok in checks]
    lines += [{"kind": "policy", "ok": b >= a, "title": pid, "detail": POLICY_TEXT[pid], "before": b, "after": a}
              for pid, b, a in policies]
    return lines


def money(key, before, after, rows, label, changed=True, headline=True, direction=None):
    return {"key": key, "before_cents": before, "after_cents": after,
            "delta_cents": None if before is None else after - before, "column_changed": changed, "rows": rows,
            "scope_label": label, "null_rows": 0, "direction": direction, "headline": headline}


# ---------- E8: red on P-7, revise, green ----------
def e8():
    r = Run("E8")
    r.step("read", "model", {"text": "Write off open invoices due on or before 2026-08-03 so the AR aging report is clean, "
                                     "and take what was owed off each account's balance."},
           920, {"in": 1480, "out": 41})
    naive_where = "status = 'open' AND due_on <= DATE '2026-11-01' - 90"
    r.lookup(f"SELECT amount_cents FROM invoices WHERE {naive_where}", "invoices", len(overdue),
             {"amount_cents": sum(i["amount_cents"] for i in overdue)})
    r.lookup(f"SELECT DISTINCT account_id FROM invoices WHERE {naive_where}", "invoices", len(naive_owed), {})
    c1 = {"tables": ["invoices", "accounts"], "expected_rows": {"invoices": [85, 95], "accounts": [70, 85]},
          "scope_rule": {"table": "invoices", "where": naive_where},
          "checks": [{"name": "no_paid_written_off",
                      "sql": "SELECT count(*) = 0 FROM invoices WHERE status = 'written_off' AND paid_cents >= amount_cents AND amount_cents > 0"},
                     {"name": "no_recent_written_off",
                      "sql": "SELECT count(*) = 0 FROM invoices WHERE status = 'written_off' AND due_on > DATE '2026-08-03'"}]}
    sha, locked = contract_step(r, 1, c1)
    plan_clause = " AND NOT EXISTS (SELECT 1 FROM payment_plans p WHERE p.invoice_id = invoices.id AND p.status = 'active')"
    assert plan_clause in GOLD["E8"]["sql"]
    naive_sql = GOLD["E8"]["sql"].replace(plan_clause, "")  # SAMPLE attempt 1: balances right, payment plans missed
    r.step("sql", "model", {"sql": naive_sql}, 1240, {"in": 3410, "out": 118})
    r.run(f"UPDATE {len(naive_owed)}", 61, 46.1)
    v1_fail = {"kind": "policy", "detail": f"P-7: violations 0 → {len(p7_rows)}", "before": 0,
               "measured": len(p7_rows), "rows": p7_rows, "text": POLICY_TEXT["P-7"]}
    lines1 = [rowline("invoices", len(overdue), 85, 95, len(overdue)),
              rowline("accounts", len(naive_owed), 70, 85, len(naive_owed))] + std_lines(
        ["invoices", "accounts"], True, f"{len(overdue)} of {len(overdue)} changed rows match",
        [(c["name"], c["sql"], True) for c in c1["checks"]],
        [("P-7", 0, len(p7_rows)), ("P-5", 1, 1), ("P-1", 0, 0), ("P-3", 0, 0)])
    r.step("compare", "drydock", {"attempt": 1, "green": False, "lines": lines1, "warnings": [],
                                  "failures": [v1_fail], "command_tag": f"UPDATE {len(naive_owed)}"}, 212)
    r.step("revise", "drydock", {"fed_back": [v1_fail["detail"]]}, 4)
    r.step("lookup_policy", "model", {"policy_id": "P-7", "text": POLICY_TEXT["P-7"]}, 610, {"in": 3790, "out": 22})
    good_where = naive_where + plan_clause
    r.lookup(f"SELECT amount_cents FROM invoices WHERE {good_where}", "invoices", len(e8_set),
             {"amount_cents": GOLD["E8"]["written_off_cents"]})
    r.lookup(f"SELECT DISTINCT account_id FROM invoices WHERE {good_where}", "invoices", len(e8_owed), {})
    c2 = {**c1, "expected_rows": {"invoices": [80, 90], "accounts": [70, 80]},
          "scope_rule": {"table": "invoices", "where": good_where}}
    sha2, _ = contract_step(r, 2, c2, 1690, (4210, 301), diff=[
        "- expected_rows.invoices: [85, 95]", "+ expected_rows.invoices: [80, 90]",
        "- expected_rows.accounts: [70, 85]", "+ expected_rows.accounts: [70, 80]",
        f"- scope_rule.where: {naive_where}", f"+ scope_rule.where: {good_where}"])
    r.step("sql", "model", {"sql": GOLD["E8"]["sql"]}, 1180, {"in": 4380, "out": 136})
    r.run(f"UPDATE {len(e8_owed)}", 66, 46.4)
    pairs = [(i, {**i, "status": "written_off"}) for i in e8_set]
    apairs = [(a, {**a, "balance_cents": a["balance_cents"] - e8_owed[a["id"]]})
              for a in (by_id["accounts"][k] for k in sorted(e8_owed))]
    bal_before, bal_after = sum(a["balance_cents"] for a, _ in apairs), sum(b["balance_cents"] for _, b in apairs)
    assert bal_before - bal_after == GOLD["E8"]["written_off_cents"]
    distinct = len({(a["balance_cents"], b["balance_cents"]) for a, b in apairs})
    effect = {"rows_by_table": {"invoices": {"count_delta": 0, "touched": len(e8_set)},
                                "accounts": {"count_delta": 0, "touched": len(e8_owed)}},
              "sample": {"invoices": sample("invoices", pairs, "number"), "accounts": sample("accounts", apairs, "code")},
              "column_summary": [f"invoices.status: open→written_off ×{len(e8_set)}",
                                 f"accounts.balance_cents: {distinct} distinct changes ×{len(apairs)}"],
              "money": [money("invoices.amount_cents", GOLD["E8"]["written_off_cents"], GOLD["E8"]["written_off_cents"],
                              len(e8_set), f"value of the {len(e8_set)} changed rows", changed=False),
                        money("accounts.balance_cents", bal_before, bal_after, len(apairs),
                              f"across the {len(apairs)} changed rows")]}
    lines2 = [rowline("invoices", len(e8_set), 80, 90, len(e8_set)),
              rowline("accounts", len(e8_owed), 70, 80, len(e8_owed))] + std_lines(
        ["invoices", "accounts"], True, f"{len(e8_set)} of {len(e8_set)} changed rows match",
        [(c["name"], c["sql"], True) for c in c2["checks"]], [("P-7", 0, 0), ("P-5", 1, 1), ("P-1", 0, 0), ("P-3", 0, 0)])
    r.step("compare", "drydock", {"attempt": 2, "green": True, "lines": lines2, "warnings": [], "failures": [],
                                  "effect": effect, "command_tag": f"UPDATE {len(e8_owed)}"}, 204)
    names = ", ".join(x["customer"] for x in p7_rows)
    expl = (f"{len(e8_set)} open invoices due on or before 2026-08-03 were marked written_off, worth "
            f"{usd(GOLD['E8']['written_off_cents'])}, and the same amount came off {len(e8_owed)} account balances. "
            f"The first attempt also wrote off {len(p7_rows)} invoices on active payment plans ({names}); policy P-7 "
            "failed, so the revision leaves them open.")
    r.step("explain", "model", {"text": expl}, 1530, {"in": 2120, "out": 168})
    reply = (f"Done on a sandbox copy, waiting for engineer approval: {len(e8_set)} invoices 90+ days overdue "
             f"written off ({usd(GOLD['E8']['written_off_cents'])}, the aging backlog), balances updated. {len(p7_rows)} "
             f"invoices on active payment plans were left alone per policy P-7 ({usd(GOLD['E8']['p7_protected_cents'])} "
             "still owed).")
    rows_all = len(e8_set) + len(e8_owed)
    return r.finish({"green": True, "failures": [], "warnings": []}, "B",
                    f"2 tables changed; {rows_all} rows > 50; {usd(GOLD['E8']['written_off_cents'])} moved, over the ops "
                    "limit of $2,500.00; a policy failed in an earlier attempt; 1 revision used",
                    expl, reply, 1, sha, locked, after_sum=applied("E8"))


# ---------- E11: stopped by a real cap, class C ----------
def e11():
    r = Run("E11")
    r.step("read", "model", {"text": "Recompute every account's October usage charge from usage_events."}, 880,
           {"in": 1460, "out": 33})
    r.lookup("SELECT id FROM accounts", "accounts", len(D["accounts"]), {})
    where = "true"
    c = {"tables": ["accounts"], "expected_rows": {"accounts": [1, len(D["accounts"])]},
         "scope_rule": {"table": "accounts", "where": where},
         "checks": [{"name": "usage_not_negative", "sql": "SELECT count(*) = 0 FROM accounts WHERE usage_cents_oct < 0"},
                    {"name": "usage_matches_events",
                     "sql": "SELECT count(*) = 0 FROM accounts a WHERE usage_cents_oct <> (SELECT coalesce(sum(amount_cents),0) FROM usage_events u WHERE u.account_id = a.id AND u.occurred_at >= DATE '2026-10-01' AND u.occurred_at < DATE '2026-11-01')"}]}
    sha, locked = contract_step(r, 1, c)
    stops, stopped = [], None
    for attempt in (1,):  # a cap ends the run: no revise (audit-1107 B6)
        sql = GOLD["E11"]["sql"]
        r.step("sql", "model", {"sql": sql}, 1300, {"in": 3500 + 400 * attempt, "out": 120})
        ms = [1043, 1012, 1067][attempt - 1]
        started = r.clock
        stop = {"stopped_by": "temp_file_limit", "code": GOLD["E11"]["observed_stop"], "cap_label": "disk spill cap",
                "stopped_at": ts(started + ms), "error": "ERROR:  temporary file size exceeds temp_file_limit (65536kB)"}
        r.run("ERROR", ms, 61.0 + attempt, stop)
        if stopped is None:
            stopped = {"cap_label": "disk spill cap", "code": stop["code"], "started_at": ts(started),
                       "stopped_at": stop["stopped_at"], "elapsed_ms": ms, "container_id": r.box,
                       "rows_label": GOLD["E11"]["rows_label"], "statement": "sql"}
        fail = {"kind": "cap", "detail": "stopped by disk spill cap"}
        lines = [{"kind": "cap", "ok": False, "title": "Statement finished inside the caps",
                  "detail": f"stopped by disk spill cap (SQLSTATE {stop['code']}) at +{ms:,} ms"},
                 rowline("accounts", len(D["accounts"]), 1, len(D["accounts"]), 0)]
        r.step("compare", "drydock", {"attempt": attempt, "green": False, "lines": lines, "warnings": [],
                                      "failures": [fail], "command_tag": "ERROR"}, 96)
    stopped["live_checksum_before"] = stopped["live_checksum_after"] = SEED_SUM
    expl = ("Recomputing October usage means summing usage_events.amount_cents (already the rated amount, so no rate "
            f"is needed) over {GOLD['E11']['rows_label'].split(': ')[1]}. The first attempt was stopped by the disk spill "
            "cap, which ends the run. Nothing changed. This re-rate is larger than the per-run cap and "
            "needs a batched job outside the per-run sandbox.")
    r.step("explain", "model", {"text": expl}, 1610, {"in": 2380, "out": 154})
    reply = ("Not done yet. The re-rate has to read the full usage event log, and that is larger than the safety cap "
             "on a single run, so DryDock stopped it and changed nothing. It needs a batched job; an engineer will "
             "schedule it.")
    return r.finish({"green": False, "failures": [{"kind": "cap", "detail": "stopped by disk spill cap"}],
                     "warnings": []}, "C", "red verdict: cap", expl, reply, 0, sha, locked, stopped=stopped)


# ---------- simple green runs ----------
def green(tid, read, lk, contract, sql, tag, effect, lines, cls, reason, expl, reply):
    r = Run(tid)
    r.step("read", "model", {"text": read}, 900, {"in": 1450, "out": 38})
    for sql_l, table, n, sums in lk:
        r.lookup(sql_l, table, n, sums)
    sha, locked = contract_step(r, 1, contract)
    r.step("sql", "model", {"sql": sql}, 1150, {"in": 3300, "out": 70})
    r.run(tag)
    r.step("compare", "drydock", {"attempt": 1, "green": True, "lines": lines, "warnings": [], "failures": [],
                                  "effect": effect, "command_tag": tag}, 180)
    r.step("explain", "model", {"text": expl}, 1400, {"in": 2000, "out": 120})
    return r.finish({"green": True, "failures": [], "warnings": []}, cls, reason, expl, reply, 0, sha, locked,
                    after_sum=applied(tid))


def e1():
    c = {"tables": ["payments"], "expected_rows": {"payments": [1, 1]},
         "scope_rule": {"table": "payments", "where": "id = 2565"},
         "checks": [{"name": "one_refund_for_charge",
                     "sql": "SELECT count(*) = 1 FROM payments WHERE kind = 'refund' AND refund_of = 2565"},
                    {"name": "refund_equals_charge",
                     "sql": "SELECT bool_and(amount_cents = 100300) FROM payments WHERE refund_of = 2565"}]}
    eff = {"rows_by_table": {"payments": {"count_delta": 1, "touched": 1}},
           "sample": {"payments": sample("payments", [(None, e1_refund)], "kind")},
           "column_summary": [f"payments: +1 inserted (refund) · amount_cents {usd(e1_refund['amount_cents'])}"],
           "money": [money("payments.amount_cents", 0, e1_refund["amount_cents"], 1, "across the 1 changed row",
                           direction="out")]}
    lines = [rowline("payments", 2, 1, 1, 1)] + std_lines(["payments"], True, "inserted rows are not scope-checked",
                                                          [(x["name"], x["sql"], True) for x in c["checks"]],
                                                          [("P-2", 0, 0), ("P-4", 0, 0)])
    return green("E1", f"Refund the duplicate {usd(e1_charge['amount_cents'])} charge on {e1_inv['number']}.",
                 [(f"SELECT amount_cents FROM payments WHERE invoice_id = {e1_inv['id']} AND kind = 'charge'",
                   "payments", 2, {"amount_cents": 2 * e1_charge["amount_cents"]})],
                 c, GOLD["E1"]["sql"], "INSERT 0 1", eff, lines, "A", "one table, 1 row, first attempt, ticket card",
                 f"One refund row of {usd(e1_charge['amount_cents'])} was inserted against the later of the two "
                 f"identical charges on {e1_inv['number']}. P-2 and P-4 still hold.",
                 f"Done on a sandbox copy, waiting for approval: one {usd(e1_charge['amount_cents'])} refund for the "
                 f"duplicate charge on {e1_inv['number']}.")


def e2():
    c = {"tables": ["accounts"], "expected_rows": {"accounts": [1, 1]},
         "scope_rule": {"table": "accounts", "where": "id = 7"},
         "checks": [{"name": "balance_matches_open",
                     "sql": "SELECT balance_cents = 5100 FROM accounts WHERE id = 7"},
                    {"name": "balance_not_negative",
                     "sql": "SELECT count(*) = 0 FROM accounts WHERE id = 7 AND balance_cents < 0"}]}
    eff = {"rows_by_table": {"accounts": {"count_delta": 0, "touched": 1}},
           "sample": {"accounts": sample("accounts", [(e2_acct, {**e2_acct, "balance_cents": e2_new})], "code")},
           "column_summary": [f"accounts.balance_cents: {e2_acct['balance_cents']}→{e2_new} ×1"],
           "money": [money("accounts.balance_cents", e2_acct["balance_cents"], e2_new, 1, "across the 1 changed row")]}
    lines = [rowline("accounts", 1, 1, 1, 1)] + std_lines(["accounts"], True, "1 of 1 changed rows match",
                                                          [(x["name"], x["sql"], True) for x in c["checks"]],
                                                          [("P-5", 1, 0), ("P-1", 0, 0)])
    return green("E2", "Set ACC-4007's balance to the sum of its open invoices.",
                 [("SELECT amount_cents - paid_cents AS owed_cents FROM invoices WHERE account_id = 7 AND status = 'open'",
                   "invoices", 1, {"owed_cents": e2_new})],
                 c, GOLD["E2"]["sql"], "UPDATE 1", eff, lines, "A", "one table, 1 row, first attempt, ticket card",
                 f"ACC-4007's balance moved from {usd(e2_acct['balance_cents'])} to {usd(e2_new)}, the amount still "
                 f"owed on its one open invoice.",
                 f"Done on a sandbox copy, waiting for approval: Halvorsen Ledgerworks' balance is now {usd(e2_new)}.")


def e5():
    c = {"tables": ["accounts", "subscriptions"], "expected_rows": {"accounts": [2, 2], "subscriptions": [2, 2]},
         "scope_rule": {"table": "accounts", "where": f"customer_id = 201 AND id <> {e5_accts[0]['id']}"},
         "checks": [{"name": "original_kept",
                     "sql": f"SELECT status = 'active' FROM accounts WHERE id = {e5_accts[0]['id']}"},
                    {"name": "extras_closed",
                     "sql": "SELECT count(*) = 2 FROM accounts WHERE customer_id = 201 AND status = 'closed'"}]}
    eff = {"rows_by_table": {"accounts": {"count_delta": 0, "touched": 2},
                             "subscriptions": {"count_delta": 0, "touched": 2}},
           "sample": {"accounts": sample("accounts", [(a, {**a, "status": "closed"}) for a in e5_closed], "code"),
                      "subscriptions": sample("subscriptions", [(s, {**s, "status": "cancelled"}) for s in e5_subs], "plan")},
           "column_summary": ["accounts.status: active→closed ×2", "subscriptions.status: active→cancelled ×2"],
           "money": [money("subscriptions.monthly_cents", sum(s["monthly_cents"] for s in e5_subs),
                           sum(s["monthly_cents"] for s in e5_subs), 2, "value of the 2 changed rows", changed=False)]}
    lines = [rowline("accounts", 3, 2, 2, 2), rowline("subscriptions", 2, 2, 2, 2)] + std_lines(
        ["accounts", "subscriptions"], True, "2 of 2 changed rows match",
        [(x["name"], x["sql"], True) for x in c["checks"]], [("P-6", 0, 0), ("P-1", 0, 0)])
    return green("E5", "Close the two newer Okafor & Lund Bakeries accounts and keep the original.",
                 [("SELECT id FROM accounts WHERE customer_id = 201", "accounts", 3, {})],
                 c, GOLD["E5"]["sql"], "UPDATE 2", eff, lines, "B", "2 tables changed",
                 f"Two duplicate accounts were closed and their 2 active subscriptions cancelled "
                 f"({usd(GOLD['E5']['expected_amount_cents'])}/month). The original {e5_accts[0]['code']} stays active.",
                 "Done on a sandbox copy, waiting for engineer approval: the two extra accounts are closed, "
                 f"{e5_accts[0]['code']} is kept.")


def e6():
    c = {"tables": ["subscriptions"], "expected_rows": {"subscriptions": [60, 75]},
         "scope_rule": {"table": "subscriptions", "where": "plan = 'enterprise' AND status = 'active'"},
         "checks": [{"name": "enterprise_total_near_90pct",
                     "sql": "SELECT sum(monthly_cents) BETWEEN 6100000 AND 6200000 FROM subscriptions WHERE plan = 'enterprise' AND status = 'active'"},
                    {"name": "monthly_positive",
                     "sql": "SELECT count(*) = 0 FROM subscriptions WHERE monthly_cents <= 0"}]}
    pairs = [(s, {**s, "monthly_cents": pg_round(s["monthly_cents"] * 0.9)}) for s in e6_rows]
    eff = {"rows_by_table": {"subscriptions": {"count_delta": 0, "touched": len(e6_rows)}},
           "sample": {"subscriptions": sample("subscriptions", pairs, "plan")},
           "column_summary": [f"subscriptions.monthly_cents: ×0.9 each ×{len(e6_rows)}"],
           "money": [money("subscriptions.monthly_cents", e6_before, e6_after, len(e6_rows),
                           f"across the {len(e6_rows)} changed rows")]}
    lines = [rowline("subscriptions", len(e6_rows), 60, 75, len(e6_rows))] + std_lines(
        ["subscriptions"], True, f"{len(e6_rows)} of {len(e6_rows)} changed rows match",
        [(x["name"], x["sql"], True) for x in c["checks"]], [])
    return green("E6", "Cut monthly_cents by 10% for every active Enterprise subscription.",
                 [("SELECT monthly_cents FROM subscriptions WHERE plan = 'enterprise' AND status = 'active'",
                   "subscriptions", len(e6_rows), {"monthly_cents": e6_before})],
                 c, GOLD["E6"]["sql"], f"UPDATE {len(e6_rows)}", eff, lines, "B",
                 f"{len(e6_rows)} rows > 50; {usd(e6_before - e6_after)} moved, over the ops limit of $2,500.00",
                 f"{len(e6_rows)} active Enterprise subscriptions now pay 10% less: Enterprise MRR across the {len(e6_rows)} "
                 f"changed rows {usd(e6_before)} → {usd(e6_after)} · −{usd(e6_before - e6_after)}/mo (−10.0%).",
                 f"Done on a sandbox copy, waiting for engineer approval: the 10% loyalty discount is set on {len(e6_rows)} "
                 f"Enterprise subscriptions (−{usd(e6_before - e6_after)}/mo).")


runs = {r["ticket"]["id"]: jsonable(r) for r in (e11(), e8(), e1(), e2(), e5(), e6())}
out = {"note": "FIXTURE: counts, ids, names and money computed from seed/; model text, timings, tokens, ids are samples",
       "seed_checksum": SEED_SUM, "tickets": TICKETS, "runs": runs, "eval": None}
(Path(__file__).parent / "src" / "fixtures.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
print("ok", {k: v["approval_class"]["class"] for k, v in runs.items()}, "p7", p7_rows)
