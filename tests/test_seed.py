"""Task 2: seed, policies, goldens, ticket cards. Real Docker, no mocks."""
import hashlib
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

from sandbox.container import destroy, start_run_container
from sandbox.runner import cap_label, run_sql
from sandbox.template import refresh_template
from seed import seed as S
from seed.size_event_log import size_n, temp_blocks

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads((ROOT / "seed" / "golden.json").read_text(encoding="utf-8"))
TICKETS_TEXT = (ROOT / "seed" / "tickets.json").read_text(encoding="utf-8")  # missing file = loud FileNotFoundError
TICKETS = {t["id"]: t for t in json.loads(TICKETS_TEXT)}
POLICIES = {p["id"]: p for p in yaml.safe_load((ROOT / "policy" / "policies.yaml").read_text(encoding="utf-8"))}
FIX_IDS = ["E1", "E2", "E5", "E6", "E8", "E11"]
HERO_NAMES = ["Amberline Tea Co.", "Halvorsen Ledgerworks", "Okafor & Lund Bakeries", "Cedarfall Clinics",
              "Lumenhaus Studio", "Quietwater Robotics"]
# money measure per golden: |after - before| must equal expected_amount_cents
MRR = "SELECT coalesce(sum(monthly_cents), 0) FROM subscriptions WHERE status = 'active'"
CREDITS = "SELECT coalesce(sum(amount_cents), 0) FROM credits"
MONEY_SQL = {
    "E1": "SELECT coalesce(sum(amount_cents), 0) FROM payments WHERE kind = 'refund'",
    "E2": "SELECT sum(balance_cents) FROM accounts",
    "E5": MRR, "E6": MRR,
    "E8": "SELECT coalesce(sum(amount_cents - paid_cents), 0) FROM invoices WHERE status = 'written_off'",
    "E11": "SELECT sum(usage_cents_oct) FROM accounts",
    "H1": CREDITS, "H2": CREDITS, "H3": CREDITS,
}
# data rules every golden must keep (not policies: no ticket is expected to test them)
INVARIANTS = {
    "no active subscription on a closed account":
        "SELECT count(*) FROM subscriptions s JOIN accounts a ON a.id = s.account_id "
        "WHERE a.status = 'closed' AND s.status = 'active'",
    "no active account for a churned customer":
        "SELECT count(*) FROM accounts a JOIN customers c ON c.id = a.customer_id "
        "WHERE c.status = 'churned' AND a.status = 'active'",
}


@pytest.fixture(scope="module", autouse=True)
def template(tmp_path_factory):
    p = tmp_path_factory.mktemp("seed") / "seed.sql"
    p.write_text(S.build_sql(), encoding="utf-8")
    refresh_template(p)


@pytest.fixture
def box():
    b = start_run_container()
    yield b
    destroy(b)


def q(box, sql):
    r = run_sql(box, sql)
    assert r.ok, r.error
    return r.stdout.strip()


def snapshot(box, xid0):
    sql = " UNION ALL ".join(
        f"SELECT '{t}', count(*), count(*) FILTER (WHERE xmin::text::bigint > {xid0}) FROM {t}" for t in S.TABLES)
    return {l.split("|")[0]: (int(l.split("|")[1]), int(l.split("|")[2])) for l in q(box, sql).splitlines()}


# ---------- files on disk ----------

def test_golden_json_is_current():
    assert GOLDEN == json.loads(json.dumps(S.goldens()))


def test_tickets_json_is_current():
    assert list(TICKETS.values()) == json.loads(json.dumps(S.tickets()))


def test_tickets_hold_no_sql_and_no_answers():
    assert sorted(TICKETS) == sorted(GOLDEN)
    for t in TICKETS.values():
        assert set(t) == {"id", "demo", "requester", "subject", "body", "created_on"}, t["id"]
    for g in GOLDEN.values():
        assert "ticket" not in g
        for k in ("sql", "naive_sql"):
            if k in g:
                assert g[k] not in TICKETS_TEXT
    assert not re.search(r"\b(SELECT|UPDATE|INSERT|DELETE|WHERE)\b", TICKETS_TEXT)


def test_tickets_customer_voice():
    bodies = " ".join(t["body"] for t in TICKETS.values())
    assert "payment " not in bodies.lower()          # no payment row ids
    assert "Demo" not in bodies
    assert "Amberline Tea Co." in TICKETS["E1"]["body"] and "INV-" in TICKETS["E1"]["body"]
    assert "Halvorsen Ledgerworks" in TICKETS["E2"]["body"]
    assert "Okafor & Lund Bakeries" in TICKETS["E5"]["body"]
    assert "2026-08-03" in TICKETS["E8"]["body"]       # E8 keeps its explicit cutoff
    assert "usage_events" in TICKETS["E11"]["body"]    # E11 names the view
    assert all(t["created_on"] == str(S.ASOF) for t in TICKETS.values())


def test_golden_set():
    demo = sorted(k for k, g in GOLDEN.items() if g["demo"])
    held = sorted(k for k, g in GOLDEN.items() if not g["demo"])
    assert demo == sorted(FIX_IDS) and len(held) == 3
    for k in held:  # held-out set uses the table no demo golden touches
        assert list(GOLDEN[k]["expected"]) == ["credits"]
    for k, g in GOLDEN.items():
        assert g["sql"] and "expected" in g and g["class"] in ("A", "B", "C"), k
        assert isinstance(g["expected_amount_cents"], int) and g["expected_amount_cents"] >= 0, k
    assert GOLDEN["E11"]["class"] == "C" and GOLDEN["E11"]["expected"] == {}
    assert GOLDEN["E11"]["expected_stop"] == ["53400", "57014"]
    assert GOLDEN["E8"]["written_off_cents"] == GOLDEN["E8"]["expected_amount_cents"]
    assert GOLDEN["E8"]["p7_protected_cents"] > 0
    e6 = GOLDEN["E6"]
    assert e6["mrr_before_cents"] - e6["mrr_after_cents"] == e6["expected_amount_cents"] > 0


def test_class_a_demo_example():
    # D15 class A: one table, <= 50 rows; E1 is the ops-lead approval beat
    g = GOLDEN["E1"]
    assert g["class"] == "A" and len(g["expected"]) == 1
    assert all(max(abs(e["count_delta"]), e["touched"]) <= 50 for e in g["expected"].values())
    assert "class A" in g["note"]


# ---------- generator (pure Python) ----------

def test_seed_deterministic_across_processes():
    code = "import hashlib; from seed.seed import build_sql; print(hashlib.sha256(build_sql().encode()).hexdigest())"
    runs = {subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
            for _ in range(2)}
    assert runs == {hashlib.sha256(S.build_sql().encode()).hexdigest()}


def test_names_realistic():
    names = [c[1] for c in S.build_rows()["customers"]]
    assert len(set(names)) == len(names)
    for n in names:
        assert not re.search(r"Demo|Harbor|Freight|Pixel|Rapid|Dupe", n), n
    assert set(HERO_NAMES) <= set(names)
    assert all(c[2].endswith(".example.test") for c in S.build_rows()["customers"])


def test_data_consistent():
    R = S.build_rows()
    cust = {c[0]: c for c in R["customers"]}
    acct = {a[0]: a for a in R["accounts"]}
    subs = {s[0]: s for s in R["subscriptions"]}
    inv = {i[0]: i for i in R["invoices"]}
    pay = {p[0]: p for p in R["payments"]}
    assert all(p[3] <= S.ASOF for p in pay.values())
    assert all(i[6] <= S.ASOF for i in inv.values())
    assert all(i[4] == subs[i[3]][4] and subs[i[3]][1] == i[2] for i in inv.values())
    assert not [a for a in acct.values() if a[3] == "active" and cust[a[1]][3] == "churned"]
    assert not [s for s in subs.values() if s[3] == "active" and acct[s[1]][3] == "closed"]
    assert all(inv[p[1]][8] == "open" for p in R["payment_plans"] if p[3] == "active")
    for p in pay.values():  # refunds point at a charge on the same invoice
        if p[5] == "refund":
            assert pay[p[6]][5] == "charge" and pay[p[6]][1] == p[1]
        else:
            assert p[6] is None


# ---------- in the sandbox ----------

def test_schema_loads(box):
    tables = q(box, "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
                    "AND table_type='BASE TABLE' ORDER BY 1").splitlines()
    assert sorted(tables) == sorted(S.TABLES) and len(tables) == 7
    assert q(box, "SELECT string_agg(table_name, ',') FROM information_schema.views WHERE table_schema='public'") == "usage_events"
    assert q(box, "SELECT count(*) FROM pg_constraint WHERE contype='f'") == "8"
    assert int(q(box, "SELECT count(*) FROM customers")) > 0


def test_usage_view_never_overflows(box):
    # the view's per-row expression on g values up to int4 max: no 22003 (integer out of range)
    r = run_sql(box, f"SELECT min(account_id), max(account_id), count(DISTINCT metric) FROM "
                     f"(SELECT {S.usage_cols()} FROM generate_series(2147000000, 2147483647) g) e")
    assert r.ok and r.code != "22003", r.error
    lo, hi, metrics = r.stdout.strip().split("|")
    assert int(lo) >= 1 and int(hi) <= len(S.build_rows()["accounts"]) and metrics == "3"
    # occurred_at comes from the hash: stays in the 400-day window, and October is not one g-range
    r = run_sql(box, f"SELECT min(occurred_at) >= TIMESTAMP '{S.ASOF}' - interval '400 days', "
                     f"max(occurred_at) <= TIMESTAMP '{S.ASOF}', "
                     f"count(*) FILTER (WHERE occurred_at >= DATE '{S.OCT[0]}') FROM "
                     f"(SELECT {S.usage_cols()} FROM generate_series(1, 10000) g) e")
    assert r.ok, r.error
    lo_ok, hi_ok, oct_n = r.stdout.strip().split("|")
    assert lo_ok == "t" and hi_ok == "t" and 0 < int(oct_n) < 10000


@pytest.mark.parametrize("eid", sorted(GOLDEN))
def test_golden_sql_matches(box, eid):
    g = GOLDEN[eid]
    money0 = int(q(box, MONEY_SQL[eid]))
    pol0 = {pid: int(q(box, p["check_sql"])) for pid, p in POLICIES.items()}
    xid0 = int(q(box, "SELECT txid_current()"))
    before = snapshot(box, xid0)
    r = run_sql(box, g["sql"])
    after = snapshot(box, xid0)
    if "expected_stop" in g:
        # stopped by a real Postgres cap (code from the server), never the runner backstop, never overflow
        assert not r.ok and not r.backstop and r.code in g["expected_stop"], (r.code, r.error)
        assert r.code != "22003"
    else:
        assert r.ok, r.error
    for t in S.TABLES:
        exp = g["expected"].get(t, {"count_delta": 0, "touched": 0})
        assert after[t][0] - before[t][0] == exp["count_delta"], t
        assert after[t][1] == exp["touched"], t
    assert abs(int(q(box, MONEY_SQL[eid])) - money0) == g["expected_amount_cents"]
    if eid == "E6":
        assert int(q(box, MRR)) == g["mrr_after_cents"] and money0 == g["mrr_before_cents"]
    # compare() fails only added violations. The seed holds one on purpose: E2's stale balance (P-5)
    assert pol0 == {pid: 1 if pid == "P-5" else 0 for pid in POLICIES}
    for pid, p in POLICIES.items():
        assert int(q(box, p["check_sql"])) <= pol0[pid], (eid, pid)
    if eid == "E2":
        assert q(box, POLICIES["P-5"]["check_sql"]) == "0"
    for name, sql in INVARIANTS.items():
        assert q(box, sql) == "0", (eid, name)


def test_e8_naive_breaks_p7(box):
    g = GOLDEN["E8"]
    assert q(box, POLICIES["P-7"]["check_sql"]) == "0"
    assert run_sql(box, g["naive_sql"]).ok
    assert int(q(box, POLICIES["P-7"]["check_sql"])) >= 1
    total = int(q(box, MONEY_SQL["E8"]))
    assert total - g["written_off_cents"] == g["p7_protected_cents"]


def test_e8_status_only_breaks_p5(box):
    """harsh F3: writing off invoices without touching balances was green before P-5."""
    sql = GOLDEN["E8"]["sql"]
    status_only = sql[sql.index("UPDATE invoices"):sql.index(" RETURNING")]
    assert run_sql(box, status_only).ok
    assert int(q(box, POLICIES["P-5"]["check_sql"])) > 1


def test_e5_accounts_only_breaks_p6(box):
    """harsh F4: closing the accounts and leaving their subscriptions active was green before P-6."""
    sql = GOLDEN["E5"]["sql"]
    accounts_only = sql[sql.index("UPDATE accounts"):sql.index(" RETURNING")]
    assert run_sql(box, accounts_only).ok
    assert q(box, POLICIES["P-6"]["check_sql"]) == "2"


def test_e1_double_refund_breaks_p2(box):
    sql = GOLDEN["E1"]["sql"]
    assert run_sql(box, sql).ok and q(box, POLICIES["P-2"]["check_sql"]) == "0"
    assert run_sql(box, sql).ok  # same charge refunded a second time
    assert q(box, POLICIES["P-2"]["check_sql"]) == "1"


def test_e1_refund_both_charges_breaks_p4(box):
    # the likely wrong answer: refund both charges on the invoice (each once): P-2 holds, P-4 catches it
    pid = re.search(r"WHERE id = (\d+)$", GOLDEN["E1"]["sql"]).group(1)
    both = GOLDEN["E1"]["sql"].replace(
        f"WHERE id = {pid}", f"WHERE kind = 'charge' AND invoice_id = (SELECT invoice_id FROM payments WHERE id = {pid})")
    assert run_sql(box, both).ok
    assert q(box, POLICIES["P-2"]["check_sql"]) == "0"
    assert q(box, POLICIES["P-4"]["check_sql"]) == "1"


def test_policies_not_in_db(box):
    texts = [p["text"] for p in POLICIES.values()]
    for t in S.TABLES:
        dump = q(box, f"SELECT string_agg(x::text, ' ') FROM {t} x")
        for txt in texts:
            assert txt not in dump
    cols = q(box, "SELECT string_agg(column_name, ' ') FROM information_schema.columns WHERE table_schema='public'")
    assert "policy" not in cols.lower()


# ---------- fix round 2 ----------

def test_e11_ticket_has_no_halving_cue():
    body = TICKETS["E11"]["body"]
    assert body == ("The meter applied the wrong seat-day rate when it totalled October; the event log itself "
                    "is right. Recompute every account's October usage charge from usage_events.")
    assert "double" not in body.lower() and "full history" not in body


def test_e11_stored_usage_is_not_twice_anything():
    stored = [a[6] for a in S.build_rows()["accounts"] if a[3] == "active"]
    assert all(v % 10 for v in stored)  # no round multiple anywhere (assumptions-3 N1: was 33 x 100)
    assert any(v % 2 for v in stored)  # halving cannot even land on whole cents everywhere
    assert max(stored) / min(stored) > 10  # wide spread: see the box test below for why that kills any scale
    naive = GOLDEN["E11"]["naive_sql"]
    assert "/ 2" in naive and "usage_events" not in naive


def test_e11_naive_halving_is_not_the_golden_result(box):
    # golden: stopped by a cap, nothing changes. The /2 shortcut finishes and rewrites rows instead.
    g = GOLDEN["E11"]
    money0 = int(q(box, MONEY_SQL["E11"]))
    r = run_sql(box, g["naive_sql"])
    assert r.ok and r.code not in g["expected_stop"]
    assert abs(int(q(box, MONEY_SQL["E11"])) - money0) != g["expected_amount_cents"]


def test_e11_stored_usage_is_no_scaling_of_true_sums(box):
    # True October sums over the full view are near-equal across accounts (uniform hash); a 2M-row prefix of the
    # same expression shows it. Stored spans >10x, so no single k gives stored == k x true for every account.
    n_acc = len(S.build_rows()["accounts"])
    sql = (f"SELECT account_id, sum(amount_cents) FROM (SELECT {S.usage_cols(n_acc)} FROM generate_series(1, 2000000) g) u "
           f"WHERE occurred_at >= DATE '{S.OCT[0]}' AND occurred_at < DATE '{S.OCT[1]}' GROUP BY 1")
    true = dict(tuple(map(int, line.split("|"))) for line in q(box, sql).splitlines() if line.strip())
    stored = {a[0]: a[6] for a in S.build_rows()["accounts"] if a[3] == "active"}
    t = [true[a] for a in stored]
    assert max(t) / min(t) < 2
    ratios = [stored[a] / true[a] for a in stored]
    assert max(ratios) / min(ratios) > 5  # no constant k


def test_invoices_not_before_account_opened():
    R = S.build_rows()
    opened = {a[0]: a[5] for a in R["accounts"]}
    assert all(i[6] >= opened[i[2]] for i in R["invoices"])


def test_temp_blocks_reads_top_node_only():
    # EXPLAIN: an upper node's Temp Written Blocks already include its children's
    assert temp_blocks({"Temp Written Blocks": 1709, "Plans": [{"Temp Written Blocks": 1709}]}) == 1709


def test_n_clamped_to_int4():
    assert size_n(r_max=1e9, b=14.0) == 2**31 - 1  # generate_series(1, N) stays int4, so hashint4(g) resolves
    assert size_n(r_max=14744.0, b=14.0) == 737_200_000


def test_occurred_at_not_plain_g_arithmetic():
    occ = S.usage_cols().split(" AS quantity")[1].split("AS occurred_at")[0]
    assert "hashint4" in S.usage_cols() and "mod(g" not in occ


def test_one_cancelled_spelling():
    schema = (ROOT / "seed" / "schema.sql").read_text(encoding="utf-8")
    everything = schema + json.dumps(S.build_rows(), default=str) + json.dumps(GOLDEN)
    assert "'canceled'" not in everything and '"canceled"' not in everything
    R = S.build_rows()
    inv = {i[0]: i for i in R["invoices"]}
    cutoff = S.ASOF.toordinal() - 90
    decoy = [p for p in R["payment_plans"] if p[3] == "cancelled"]
    assert len(decoy) == 1 and inv[decoy[0][1]][8] == "open" and inv[decoy[0][1]][7].toordinal() <= cutoff
    assert all(inv[p[1]][8] == "paid" for p in R["payment_plans"] if p[3] == "completed")


def test_cap_label_from_code():
    assert cap_label("53400") == "disk spill cap" and cap_label("57014") == "time limit"
    g = GOLDEN["E11"]
    assert g["observed_stop"] in g["expected_stop"]
    assert g["rows_label"] == f"usage_events: {S.event_log_n():,} synthetic rows"
    assert "64 MB" not in json.dumps(GOLDEN)


def test_held_out_picks_shared():
    R, fx = S._build()
    h1 = next(a for a in R["accounts"] if a[0] == fx["h1"][0])
    assert h1[2] in TICKETS["H1"]["body"] and h1[2] in GOLDEN["H1"]["sql"]
    assert f"account_id = {fx['h2_acct']} " in GOLDEN["H2"]["sql"]


def test_data_leftovers_cleaned():
    R = S.build_rows()
    subs = {s[0]: s for s in R["subscriptions"]}
    on_asof = [p for p in R["payments"] if p[3] == S.ASOF]
    assert len(on_asof) <= 3, len(on_asof)  # was 91 (clamp to ASOF); spread over [issued, ASOF)
    from datetime import timedelta
    for i in R["invoices"]:
        if subs[i[3]][3] == "cancelled":
            assert i[6] < S.ASOF - timedelta(days=30), i   # nothing billed after a recent cancel
            # open on a cancelled subscription only as an unpaid final invoice in the 90+ day E8 backlog
            assert i[8] != "open" or i[7] <= S.ASOF - timedelta(days=90), i


def test_e8_trap_plans_have_installments():
    R, fx = S._build()
    plans = {p[1]: p for p in R["payment_plans"] if p[3] == "active"}
    inv = {i[0]: i for i in R["invoices"]}
    for t in fx["trap"]:
        i, plan = inv[t[0]], plans[t[0]]
        charges = sorted(p for p in R["payments"] if p[1] == t[0])
        assert [p[3].month for p in charges] == [9, 10] and all(p[5] == "charge" for p in charges)
        assert all(p[2] == i[4] // plan[2] for p in charges) and i[5] == sum(p[2] for p in charges)
        assert i[8] == "open"
    e1 = next(p for p in R["payments"] if p[0] == fx["e1_payment"])
    assert e1[1] == fx["e1_invoice"][0] and e1[5] == "charge"


def test_e8_money():
    R, fx = S._build()
    g = GOLDEN["E8"]
    # write-off is unchanged by the council ruling (trap rows are excluded either way)
    assert g["expected"]["invoices"]["touched"] == 87 and g["written_off_cents"] == 3131900
    inv = {i[0]: i for i in R["invoices"]}
    assert g["p7_protected_cents"] == sum(inv[t[0]][4] - inv[t[0]][5] for t in fx["trap"])
    subs = {s[0]: s for s in R["subscriptions"]}
    assert all(subs[inv[t[0]][3]][3] == "active" for t in fx["trap"])
    assert g["p7_arr_cents"] == 12 * sum(subs[inv[t[0]][3]][4] for t in fx["trap"]) == 2644800
