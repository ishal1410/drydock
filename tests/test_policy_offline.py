"""harsh F3, F4 and the single-pass note "approval class ignores money". Docker-free.

The policy checks themselves are SQL and run only in tests/test_seed.py (Docker). Here the same rules are restated in
Python over seed.seed.build_rows(), to pin that the shipped wrong answers add violations and the goldens add none.
That proves the rules and the data agree; it does not prove the SQL text matches them.
"""
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import yaml

from core.contract import CLASS_A_MAX_CENTS, Verdict, approval
from core.measure import Effect, MoneyDelta
from seed.seed import ASOF, build_rows

ROOT = Path(__file__).resolve().parent.parent
POLICIES = {p["id"]: p for p in yaml.safe_load((ROOT / "policy" / "policies.yaml").read_text(encoding="utf-8"))}
GOLDEN = json.loads((ROOT / "seed" / "golden.json").read_text(encoding="utf-8"))
R = build_rows()
# column positions (seed/schema.sql order)
A_ID, A_CUST, A_STATUS, A_BAL = 0, 1, 3, 4
I_ID, I_ACCT, I_AMT, I_PAID, I_DUE, I_STATUS = 0, 2, 4, 5, 7, 8
S_ACCT, S_STATUS = 1, 3


def p5(accounts, invoices):
    """P-5: balance = what is still owed on the account's open invoices."""
    owed = {}
    for i in invoices:
        if i[I_STATUS] == "open":
            owed[i[I_ACCT]] = owed.get(i[I_ACCT], 0) + i[I_AMT] - i[I_PAID]
    return sum(1 for a in accounts if a[A_BAL] != owed.get(a[A_ID], 0))


def p6(accounts, subs):
    """P-6: a closed account has no active subscription."""
    closed = {a[A_ID] for a in accounts if a[A_STATUS] == "closed"}
    return sum(1 for s in subs if s[S_ACCT] in closed and s[S_STATUS] == "active")


def setcol(row, i, v):
    return tuple(v if n == i else x for n, x in enumerate(row))


def test_policies_exist_with_plain_text():
    assert POLICIES["P-5"]["text"] == "An account's balance equals what is still owed on its open invoices."
    assert POLICIES["P-6"]["text"] == "A closed account has no active subscription."


def test_f3_e8_status_only_write_off_leaves_balances_stale_and_the_golden_fixes_them():
    cut = ASOF - timedelta(days=90)
    plans = {p[1] for p in R["payment_plans"] if p[3] == "active"}
    safe = {i[I_ID] for i in R["invoices"] if i[I_STATUS] == "open" and i[I_DUE] <= cut and i[I_ID] not in plans}
    inv = [setcol(i, I_STATUS, "written_off") if i[I_ID] in safe else i for i in R["invoices"]]
    before = p5(R["accounts"], R["invoices"])
    assert before == 1  # E2's seeded bug, the only one
    assert p5(R["accounts"], inv) > before  # the old golden: green under P-1..P-7, balances wrong
    owed = {}
    for i in R["invoices"]:
        if i[I_ID] in safe:
            owed[i[I_ACCT]] = owed.get(i[I_ACCT], 0) + i[I_AMT] - i[I_PAID]
    acc = [setcol(a, A_BAL, a[A_BAL] - owed[a[A_ID]]) if a[A_ID] in owed else a for a in R["accounts"]]
    assert p5(acc, inv) <= before
    g = GOLDEN["E8"]
    assert "UPDATE accounts" in g["sql"] and g["expected"]["accounts"] == {"count_delta": 0, "touched": len(owed)}
    assert g["expected"]["invoices"]["touched"] == len(safe) == 87


def test_f4_e5_closing_accounts_without_cancelling_subscriptions_is_caught():
    dup = sorted((a for a in R["accounts"] if a[A_CUST] == 201), key=lambda a: (a[5], a[A_ID]))[1:]
    ids = {a[A_ID] for a in dup}
    acc = [setcol(a, A_STATUS, "closed") if a[A_ID] in ids else a for a in R["accounts"]]
    assert p6(R["accounts"], R["subscriptions"]) == 0
    assert p6(acc, R["subscriptions"]) == 2  # the faithful reading of "close the two extra accounts"
    subs = [setcol(s, S_STATUS, "cancelled") if s[S_ACCT] in ids else s for s in R["subscriptions"]]
    assert p6(acc, subs) == 0  # the golden


def _trace(money, rows=1):
    eff = Effect({"invoices": {"count_delta": 0, "touched": rows}},
                 {"invoices": {"inserted": [], "updated": list(range(rows)), "deleted": []}}, {}, [],
                 {"invoices.amount_cents": money}, {})
    return SimpleNamespace(effect=eff, verdicts=[Verdict(True, [])], revisions=0)


def test_single_pass_note_money_raises_the_class():
    """One row of any value was class A. A change worth more than CLASS_A_MAX_CENTS now needs an engineer."""
    small = MoneyDelta(0, 100300, 100300, True, 1, "across 1 changed row", headline=True)  # E1's $1,003.00
    assert approval(Verdict(True, []), _trace(small), "ticket")[0] == "A"
    big = MoneyDelta(0, CLASS_A_MAX_CENTS + 1, CLASS_A_MAX_CENTS + 1, True, 1, "across 1 changed row", headline=True)
    cls, why = approval(Verdict(True, []), _trace(big), "ticket")
    assert cls == "B" and "over the ops limit" in why
    untouched = MoneyDelta(10_000_000, 10_000_000, 0, False, 1, "value of 1 changed row", headline=True)
    assert approval(Verdict(True, []), _trace(untouched), "ticket")[0] == "B"  # a write-off moves the value it marks


def test_eval_counts_one_write_off_once():
    """E8's golden moves $31,319.00 of invoices and the same $31,319.00 off balances: one amount, two tables."""
    from eval.run_eval import money_moved
    eff = Effect({}, {}, {}, [], {"invoices.amount_cents": MoneyDelta(3131900, 3131900, 0, False, 87, "v", headline=True),
                                  "accounts.balance_cents": MoneyDelta(4000000, 868100, -3131900, True, 75, "a",
                                                                       headline=True)}, {})
    assert money_moved(eff) == GOLDEN["E8"]["expected_amount_cents"] == 3131900
