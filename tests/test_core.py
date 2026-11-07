"""Task 3: measure + compare engine. Real Docker, no mocks; expected numbers come from seed/golden.json."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.contract import (Check, Contract, Failure, Verdict, approval, approval_class, compare, parse_contract,
                           schema_changed_verdict)
from core.measure import SchemaChanged, diff, policy_results, run_checks, scope_pks, snapshot_hashes
from sandbox.container import destroy, start_run_container
from sandbox.runner import psql, run_sql
from sandbox.template import refresh_template
from seed import seed as S

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads((ROOT / "seed" / "golden.json").read_text(encoding="utf-8"))
OK = {"c1": True, "c2": True}  # both default checks returned true
E8_SCOPE = {"table": "invoices", "where": "status = 'open' AND due_on <= DATE '2026-11-01' - 90"}


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


def measure(box, sql):
    before = snapshot_hashes(box)
    r = run_sql(box, sql)
    assert r.ok, r.error
    return diff(before, snapshot_hashes(box))


def contract(tables, expected_rows, scope_rule, checks=None):
    return Contract.model_validate({
        "tables": tables, "expected_rows": expected_rows, "scope_rule": scope_rule,
        "checks": checks or [{"name": "c1", "sql": "SELECT count(*) >= 0 FROM invoices"},
                              {"name": "c2", "sql": "SELECT count(*) >= 0 FROM accounts"}]})


def kinds(verdict):
    return {f.kind for f in verdict.failures}


def test_diff_counts(box):
    assert measure(box, GOLDEN["E1"]["sql"]).rows_by_table == GOLDEN["E1"]["expected"]


def test_noop(box):
    eff = measure(box, "UPDATE invoices SET status = 'void' WHERE false")
    assert eff.rows_by_table == {} and eff.money_cents_by_column == {}
    scope = {"table": "invoices", "where": "false"}
    assert compare(contract(["invoices"], {"invoices": [0, 5]}, scope), eff, set(), OK, {}).green
    v = compare(contract(["invoices"], {"invoices": [1, 5]}, scope), eff, set(), OK, {})
    assert not v.green and kinds(v) == {"row_count"}
    v = compare(contract(["invoices"], {"invoices": [0, 5]}, scope), eff, set(), OK, {}, cap="disk spill cap")
    assert not v.green and kinds(v) == {"cap"}


def test_tables_outside_contract(box):
    eff = measure(box, GOLDEN["E1"]["sql"])
    c = contract(["invoices"], {"invoices": [0, 5]}, {"table": "invoices", "where": "false"})
    v = compare(c, eff, set(), OK, {})
    assert not v.green and "tables" in kinds(v)
    assert "payments" in next(f.detail for f in v.failures if f.kind == "tables")


def test_row_count_out_of_range(box):
    sql = GOLDEN["E2"]["sql"]
    no_where = sql[:sql.rindex(" WHERE id = 7")]
    assert no_where != sql
    n_accounts = int(q(box, "SELECT count(*) FROM accounts"))
    scope_rule = {"table": "accounts", "where": "id = 7"}
    pks = scope_pks(box, scope_rule)
    assert pks == {7}
    eff = measure(box, no_where)
    assert eff.rows_by_table["accounts"]["touched"] == n_accounts == 263
    v = compare(contract(["accounts"], {"accounts": [1, 1]}, scope_rule), eff, pks, OK, {})
    assert not v.green and kinds(v) == {"row_count", "scope"}
    assert f"measured {n_accounts}" in next(f.detail for f in v.failures if f.kind == "row_count")


def test_scope_rule(box):
    scope_rule = {"table": "accounts", "where": "id = 7"}
    pks = scope_pks(box, scope_rule)
    eff = measure(box, "UPDATE accounts SET balance_cents = balance_cents + 1 WHERE id IN (7, 8)")
    v = compare(contract(["accounts"], {"accounts": [1, 2]}, scope_rule), eff, pks, OK, {})
    assert not v.green and kinds(v) == {"scope"}
    assert "8" in v.failures[0].detail


def test_policy_p7(box):
    # range is wide on purpose (0..touched*2) so only the policy line decides the verdict
    n = GOLDEN["E8"]["expected"]["invoices"]["touched"]
    c = contract(["invoices"], {"invoices": [0, n * 2]}, E8_SCOPE)
    pks = scope_pks(box, E8_SCOPE)
    eff = measure(box, GOLDEN["E8"]["naive_sql"])
    v = compare(c, eff, pks, OK, policy_results(box))
    assert not v.green and kinds(v) == {"policy"}
    assert v.failures[0].detail.startswith("P-7")


def test_policy_p7_golden_green(box):
    n = GOLDEN["E8"]["expected"]["invoices"]["touched"]
    c = contract(["invoices"], {"invoices": [n, n]}, E8_SCOPE)
    pks = scope_pks(box, E8_SCOPE)
    eff = measure(box, GOLDEN["E8"]["sql"])
    v = compare(c, eff, pks, OK, policy_results(box))
    assert v.green and v.failures == []


def test_sample_bounded(box):
    eff = measure(box, GOLDEN["E8"]["sql"])
    rows = eff.sample["invoices"]
    assert len(rows) == 5 < eff.rows_by_table["invoices"]["touched"]
    for s in rows:
        assert s["op"] == "updated" and s["changed"] == ["status"]
        assert s["before"]["status"] == "open" and s["after"]["status"] == "written_off"
        real = json.loads(q(box, f"SELECT row_to_json(t) FROM invoices t WHERE id = {s['pk']}"))
        assert s["after"] == real


def test_column_summary(box):
    eff = measure(box, GOLDEN["E8"]["sql"])
    n = eff.rows_by_table["invoices"]["touched"]
    assert n == GOLDEN["E8"]["expected"]["invoices"]["touched"]
    assert eff.column_summary == [f"invoices.status: open→written_off ×{n}"]


def test_money_e6_mrr(box):
    g = GOLDEN["E6"]
    # the brief's spec sums over changed pks only, so before/after cover the 68 changed rows;
    # the golden MRR totals also include unchanged active subscriptions, so only the delta must match them
    changed = ("SELECT coalesce(sum(monthly_cents), 0) FROM subscriptions "
               "WHERE plan = 'enterprise' AND status = 'active'")
    before_changed = int(q(box, changed))
    eff = measure(box, g["sql"])
    m = eff.money_cents_by_column["subscriptions.monthly_cents"]
    assert m.delta_cents == g["mrr_after_cents"] - g["mrr_before_cents"] == -g["expected_amount_cents"]
    assert m.before_cents == before_changed and m.after_cents == int(q(box, changed))
    assert m.after_cents - m.before_cents == m.delta_cents
    assert set(eff.money_cents_by_column) == {"subscriptions.monthly_cents"}


def test_money_e1_insert(box):
    eff = measure(box, GOLDEN["E1"]["sql"])
    m = eff.money_cents_by_column["payments.amount_cents"]
    assert (m.before_cents, m.after_cents, m.delta_cents) == (0, GOLDEN["E1"]["expected_amount_cents"],
                                                              GOLDEN["E1"]["expected_amount_cents"])


def test_contract_schema():
    with pytest.raises(ValidationError) as e:
        parse_contract('{"tables": ["invoices"], "expected_rows": {"invoices": [5, 1]}, "checks": []}')
    msg = str(e.value)
    assert "scope_rule" in msg and "expected_rows" in msg and "checks" in msg
    with pytest.raises(ValidationError, match="tables"):
        parse_contract('{"tables": ["nope"], "expected_rows": {"nope": [0, 1]}, '
                       '"scope_rule": {"table": "nope", "where": "true"}, '
                       '"checks": [{"name": "a", "sql": "SELECT true"}, {"name": "b", "sql": "SELECT true"}]}')
    with pytest.raises(ValidationError):
        parse_contract("not json")
    c = parse_contract(json.dumps({"tables": ["invoices"], "expected_rows": {"invoices": [1, 2]},
                                   "scope_rule": E8_SCOPE,
                                   "checks": [{"name": "a", "sql": "SELECT true FROM invoices"},
                                              {"name": "b", "sql": "SELECT 1 FROM invoices"}]}))
    assert c.expected_rows["invoices"] == (1, 2)


def _effect(table, n):
    before = {t: {} for t in S.TABLES}
    after = {t: {} for t in S.TABLES}
    before[table] = {i: ("1", {"id": i, "status": "open"}) for i in range(1, n + 1)}
    after[table] = {i: ("2", {"id": i, "status": "written_off"}) for i in range(1, n + 1)}
    return diff(before, after)


def test_approval_class():
    green, p7 = Verdict(True, []), Verdict(False, [Failure("policy", "P-7: 3 violating rows")])
    small = _effect("invoices", 1)
    t = lambda eff, verdicts, revisions: SimpleNamespace(effect=eff, verdicts=verdicts, revisions=revisions)
    assert approval_class(green, t(small, [green], 0), "ticket") == "A"
    assert approval_class(green, t(_effect("invoices", 50), [green], 0), "ticket") == "A"
    assert approval_class(green, t(_effect("invoices", 51), [green], 0), "ticket") == "B"
    # E8 after its P-7 revise
    e8 = _effect("invoices", GOLDEN["E8"]["expected"]["invoices"]["touched"])
    assert approval_class(green, t(e8, [p7, green], 1), "ticket") == "B"
    assert approval_class(green, t(small, [p7, green], 1), "ticket") == "B"
    assert approval_class(green, t(small, [green], 0), "text") == "B"
    assert approval_class(p7, t(small, [p7], 0), "ticket") == "C"
    assert approval_class(green, t(small, [p7, p7, green], 2), "ticket") == "C"
    two = diff({**{x: {} for x in S.TABLES}}, {**{x: {} for x in S.TABLES},
                                              "credits": {1: ("9", {"id": 1})}, "payments": {1: ("9", {"id": 1})}})
    assert approval_class(green, t(two, [green], 0), "ticket") == "B"


# ---- fix round 1 (task-3-fixlist.md) ----
def usd(c):
    return f"${c / 100:,.2f}"


def e2_no_where():
    sql = GOLDEN["E2"]["sql"]
    return sql[:sql.rindex(" WHERE id = 7")]


def n_accounts(box):
    return int(q(box, "SELECT count(*) FROM accounts"))


def test_p7_detail_rows(box):  # fix 1
    g = GOLDEN["E8"]
    measure(box, g["naive_sql"])
    p7 = policy_results(box)["P-7"]
    assert p7.count == len(p7.rows) > 0
    assert sum(r["owed_cents"] for r in p7.rows) == g["p7_protected_cents"]
    assert sum(r["arr_cents"] for r in p7.rows) == g["p7_arr_cents"]
    assert all(r["customer"] for r in p7.rows)


def test_money_e8_unchanged_column(box):  # fix 2
    g = GOLDEN["E8"]
    eff = measure(box, g["sql"])
    n = g["expected"]["invoices"]["touched"]
    m = eff.money_cents_by_column["invoices.amount_cents"]
    assert m.column_changed is False and m.rows == n
    assert m.before_cents == m.after_cents == g["written_off_cents"] and m.delta_cents == 0
    assert m.scope_label == f"value of the {n} changed rows" and m.null_rows == 0


def test_money_unchanged_hidden_without_status(box):  # fix 2: E2 usage line hidden
    eff = measure(box, e2_no_where())
    assert "accounts.usage_cents_oct" not in eff.money_cents_by_column
    assert eff.money_cents_by_column["accounts.balance_cents"].column_changed is True


def test_money_e1_refund_direction(box):  # fix 2: refund = money out, stored data unchanged
    m = measure(box, GOLDEN["E1"]["sql"]).money_cents_by_column["payments.amount_cents"]
    assert m.direction == "out" and m.delta_cents == GOLDEN["E1"]["expected_amount_cents"] > 0


def test_money_null_counted():  # fix 2: NULL money is flagged, not folded into 0 silently
    before = {t: {} for t in S.TABLES}
    after = {t: {} for t in S.TABLES}
    before["credits"] = {1: ("1", {"id": 1, "amount_cents": None})}
    after["credits"] = {1: ("2", {"id": 1, "amount_cents": 500})}
    m = diff(before, after).money_cents_by_column["credits.amount_cents"]
    assert m.null_rows == 1 and m.delta_cents == 500


def test_money_e6_ratio_and_label(box):  # fix 3
    g = GOLDEN["E6"]
    eff = measure(box, g["sql"])
    m = eff.money_cents_by_column["subscriptions.monthly_cents"]
    n = g["expected"]["subscriptions"]["touched"]
    assert m.after_cents * 10 == m.before_cents * 9  # measured ratio == the ticket's promised 10%
    assert m.scope_label == f"across the {n} changed rows" and "MRR" not in m.scope_label


def test_failure_structured(box):  # fix 4
    scope_rule = {"table": "accounts", "where": "id = 7"}
    pks = scope_pks(box, scope_rule)
    n = n_accounts(box)
    eff = measure(box, e2_no_where())
    v = compare(contract(["accounts"], {"accounts": [1, 1]}, scope_rule), eff, pks, OK, {})
    rc = next(f for f in v.failures if f.kind == "row_count")
    assert rc.promised == (1, 1) and rc.measured == n
    sc = next(f for f in v.failures if f.kind == "scope")
    assert 7 not in sc.out_of_scope_ids and len(sc.out_of_scope_ids) == n - 1


def test_same_value_rewrite_counted(box):  # fix 5
    n = n_accounts(box)
    eff = measure(box, e2_no_where())
    unchanged = [x for x in eff.column_summary if x.startswith("accounts: rewritten, unchanged ×")]
    assert len(unchanged) == 1
    total = sum(int(x.rsplit("×", 1)[1]) for x in eff.column_summary
                if x.startswith("accounts.balance_cents") or x.startswith("accounts: rewritten"))
    assert total == n == eff.rows_by_table["accounts"]["touched"]


def test_insert_delete_summary(box):  # fix 6
    eff = measure(box, GOLDEN["E1"]["sql"])
    assert eff.column_summary == [
        f"payments: +1 inserted (refund) · amount_cents {usd(GOLDEN['E1']['expected_amount_cents'])}"]
    eff = measure(box, GOLDEN["H2"]["sql"])
    assert eff.column_summary == [f"credits: −1 deleted · amount_cents {usd(GOLDEN['H2']['expected_amount_cents'])}"]


def test_unique_check_names():  # fix 7
    with pytest.raises(ValidationError, match="duplicate"):
        contract(["invoices"], {"invoices": [0, 1]}, E8_SCOPE,
                 [{"name": "a", "sql": "SELECT true FROM invoices"}, {"name": "a", "sql": "SELECT false FROM invoices"}])


def test_policy_before_after(box):  # fix 8
    # a violation that already exists before the change must not turn an unrelated change red
    q(box, "UPDATE invoices SET paid_cents = amount_cents + 1 WHERE id = (SELECT min(id) FROM invoices)")
    before = policy_results(box)
    assert before["P-3"].count >= 1
    eff = measure(box, GOLDEN["E1"]["sql"])
    c = contract(["payments"], {"payments": [1, 1]}, {"table": "payments", "where": "false"})
    after = policy_results(box)
    assert not compare(c, eff, set(), OK, after).green  # no before = fail closed (before counts as 0)
    assert compare(c, eff, set(), OK, after, policy_before=before).green


def test_policy_added_violation_reports_both(box):  # fix 8
    n = GOLDEN["E8"]["expected"]["invoices"]["touched"]
    pks = scope_pks(box, E8_SCOPE)
    before = policy_results(box)
    eff = measure(box, GOLDEN["E8"]["naive_sql"])
    after = policy_results(box)
    v = compare(contract(["invoices"], {"invoices": [0, n * 2]}, E8_SCOPE), eff, pks, OK, after, policy_before=before)
    f = next(x for x in v.failures if x.kind == "policy")
    assert (f.before, f.measured) == (before["P-7"].count, after["P-7"].count) and f.measured > f.before
    assert f.rows == after["P-7"].rows and f.detail.startswith("P-7")


def test_collapse_keeps_ratio(box):  # fix 9
    g = GOLDEN["E6"]
    n = g["expected"]["subscriptions"]["touched"]
    eff = measure(box, g["sql"])
    assert eff.column_summary == [f"subscriptions.monthly_cents: ×0.9 each ×{n}"]


def test_ddl_named_failure(box):  # fix 10
    eff = measure(box, "SELECT 1")
    c = contract(["invoices"], {"invoices": [0, 5]}, {"table": "invoices", "where": "false"})
    for sql in ("DROP TABLE credits", "alter table credits add column x int", "CREATE TABLE z (a int)"):
        assert not run_sql(box, sql).ok  # the executor has no DDL rights
        v = compare(c, eff, set(), OK, {}, sql=sql)
        assert not v.green and "ddl" in kinds(v)
    assert compare(c, eff, set(), OK, {}, sql="UPDATE invoices SET status = 'void' WHERE false").green
    assert psql(box.id, box.sock_admin, "DROP TABLE credits").returncode == 0  # admin, disposable box
    with pytest.raises(SchemaChanged, match="credits"):
        snapshot_hashes(box)


def test_scope_true_flagged(box):  # fix 10
    n = n_accounts(box)
    scope_rule = {"table": "accounts", "where": "true"}
    pks = scope_pks(box, scope_rule)
    eff = measure(box, e2_no_where())
    v = compare(contract(["accounts"], {"accounts": [n, n]}, scope_rule), eff, pks, OK, {})
    assert v.green and any("every row" in w for w in v.warnings)
    tr = SimpleNamespace(effect=_effect("accounts", 1), verdicts=[v], revisions=0)
    assert approval_class(v, tr, "ticket") == "B"


def test_noop_and_c_reason():  # fix 10
    green = Verdict(True, [])
    empty = diff({t: {} for t in S.TABLES}, {t: {} for t in S.TABLES})
    t = lambda eff, revisions: SimpleNamespace(effect=eff, verdicts=[green], revisions=revisions)
    assert approval_class(green, t(empty, 0), "ticket") == "no-op"
    cls, reason = approval(green, t(_effect("invoices", 1), 2), "ticket")
    assert cls == "C" and "2 revisions" in reason
    assert approval(Verdict(False, [Failure("cap", "x")]), t(empty, 0), "ticket")[0] == "C"


def test_run_checks_booleans(box):  # fix 11
    checks = [Check(name="yes", sql="SELECT count(*) > 0 FROM invoices"),
              Check(name="no", sql="SELECT count(*) < 0 FROM invoices"),
              Check(name="err", sql="SELECT nope FROM invoices")]
    assert run_checks(box, checks) == {"yes": True, "no": False, "err": False}


def test_constant_check_rejected():  # fix 11
    with pytest.raises(ValidationError, match="FROM"):
        contract(["invoices"], {"invoices": [0, 1]}, E8_SCOPE,
                 [{"name": "a", "sql": "SELECT true"}, {"name": "b", "sql": "SELECT 1 FROM invoices"}])


# ---- fix round 2 (task-3-*-2.md reviews) ----
SEQ = "SELECT last_value FROM credits_id_seq"


def test_check_with_write_is_red_and_changes_nothing(box):  # r2 fix 1
    n, seq = q(box, "SELECT count(*) FROM credits"), q(box, SEQ)
    checks = [Check(name="cte_delete", sql="WITH d AS (DELETE FROM credits RETURNING 1) SELECT count(*) >= 0 FROM d"),
              Check(name="nextval", sql="SELECT nextval('credits_id_seq') > 0 FROM invoices LIMIT 1")]
    res = run_checks(box, checks)
    assert res == {"cte_delete": False, "nextval": False}
    assert (q(box, "SELECT count(*) FROM credits"), q(box, SEQ)) == (n, seq)
    c = contract(["invoices"], {"invoices": [0, 5]}, {"table": "invoices", "where": "false"},
                 [c.model_dump() for c in checks])
    v = compare(c, measure(box, "SELECT 1"), set(), res, {})
    assert not v.green and kinds(v) == {"check"}


def test_multi_statement_check_and_scope_rejected():  # r2 fix 1
    with pytest.raises(ValidationError, match="one statement"):
        Check(name="a", sql="DELETE FROM credits; SELECT true FROM invoices")
    assert Check(name="a", sql="SELECT true FROM invoices;").sql == "SELECT true FROM invoices"  # trailing ; ok
    with pytest.raises(ValidationError, match="one statement"):
        contract(["invoices"], {"invoices": [0, 1]}, {"table": "invoices", "where": "true); DELETE FROM credits; (true"})


def test_scope_rule_with_write_rejected(box):  # r2 fix 1
    seq = q(box, SEQ)
    with pytest.raises(RuntimeError, match="read-only"):
        scope_pks(box, {"table": "credits", "where": "nextval('credits_id_seq') > 0"})
    with pytest.raises(ValueError, match="one statement"):
        scope_pks(box, {"table": "credits", "where": "true); DELETE FROM credits; (true"})
    assert q(box, SEQ) == seq


def headlines(eff):
    return {k: m for k, m in eff.money_cents_by_column.items() if m.headline}


def test_headline_e8(box):  # r2 fix 2
    g = GOLDEN["E8"]
    eff = measure(box, g["sql"])
    h = headlines(eff)
    assert list(h) == ["invoices.amount_cents"] and h["invoices.amount_cents"].before_cents == g["written_off_cents"]
    assert eff.money_cents_by_column["invoices.paid_cents"].headline is False


def test_headline_e5(box):  # r2 fix 2
    g = GOLDEN["E5"]
    eff = measure(box, g["sql"])
    h = headlines(eff)
    assert h["subscriptions.monthly_cents"].before_cents == g["expected_amount_cents"]
    for t in eff.rows_by_table:  # at most one headline per changed table
        assert sum(k.startswith(t + ".") for k in h) <= 1


def test_headline_changed_column(box):  # r2 fix 2: the column the SQL wrote wins
    assert list(headlines(measure(box, GOLDEN["E6"]["sql"]))) == ["subscriptions.monthly_cents"]
    assert list(headlines(measure(box, e2_no_where()))) == ["accounts.balance_cents"]


def test_scope_wider_than_promise_warned(box):  # r2 fix 3
    scope_rule = {"table": "accounts", "where": "id <> 1"}
    pks = scope_pks(box, scope_rule)
    eff = measure(box, GOLDEN["E2"]["sql"])
    v = compare(contract(["accounts"], {"accounts": [1, 1]}, scope_rule), eff, pks, OK, {})
    assert v.green and any("more rows than the contract" in w for w in v.warnings)
    tight = scope_pks(box, {"table": "accounts", "where": "id = 7"})
    assert compare(contract(["accounts"], {"accounts": [1, 1]}, {"table": "accounts", "where": "id = 7"}),
                   eff, tight, OK, {}).warnings == []


def test_policy_failure_plain_text(box):  # r2 fix 4
    n = GOLDEN["E8"]["expected"]["invoices"]["touched"]
    before = policy_results(box)
    eff = measure(box, GOLDEN["E8"]["naive_sql"])
    v = compare(contract(["invoices"], {"invoices": [0, n * 2]}, E8_SCOPE), eff, scope_pks(box, E8_SCOPE), OK,
                policy_results(box), policy_before=before)
    f = next(x for x in v.failures if x.kind == "policy")
    assert f.text == "Invoices on an active payment plan are never written off."


def test_ddl_after_comment_flagged():  # r2 fix 5
    eff = diff({t: {} for t in S.TABLES}, {t: {} for t in S.TABLES})
    c = contract(["invoices"], {"invoices": [0, 5]}, {"table": "invoices", "where": "false"})
    for sql in ("-- note\nDROP TABLE credits", "/* x */ DROP TABLE credits", "/* a\nb */ -- c\n  create temp table z (a int)"):
        assert "ddl" in kinds(compare(c, eff, set(), OK, {}, sql=sql)), sql


def test_schema_changed_becomes_red_failure(box):  # r2 fix 5
    assert psql(box.id, box.sock_admin, "DROP TABLE credits").returncode == 0
    try:
        snapshot_hashes(box)
        raise AssertionError("expected SchemaChanged")
    except SchemaChanged as e:
        v = schema_changed_verdict(e)
    assert not v.green and kinds(v) == {"ddl"} and "credits" in v.failures[0].detail
    empty = diff({t: {} for t in S.TABLES}, {t: {} for t in S.TABLES})
    assert approval(v, SimpleNamespace(effect=empty, verdicts=[v], revisions=0), "ticket")[0] == "C"


def _one(table, b_row, a_row):
    before, after = {t: {} for t in S.TABLES}, {t: {} for t in S.TABLES}
    before[table] = {1: ("1", b_row)} if b_row else {}
    after[table] = {1: ("2", a_row)} if a_row else {}
    return diff(before, after)


def test_singular_and_null_once():  # r2 fix 6
    m = _one("credits", {"id": 1, "amount_cents": None}, {"id": 1, "amount_cents": None, "x": 1}).money_cents_by_column
    assert m == {}  # untouched column, no status move: hidden
    m = _one("credits", {"id": 1, "amount_cents": None, "status": "a"},
             {"id": 1, "amount_cents": None, "status": "b"}).money_cents_by_column["credits.amount_cents"]
    assert m.null_rows == 1 and m.scope_label == "value of the 1 changed row"


def test_mixed_direction_no_sum():  # r2 fix 6
    before, after = {t: {} for t in S.TABLES}, {t: {} for t in S.TABLES}
    after["payments"] = {1: ("2", {"id": 1, "kind": "charge", "amount_cents": 1000}),
                         2: ("2", {"id": 2, "kind": "refund", "amount_cents": 1000})}
    m = diff(before, after).money_cents_by_column["payments.amount_cents"]
    assert m.direction == "mixed" and m.rows == 2
    assert (m.before_cents, m.after_cents, m.delta_cents) == (None, None, None)


# ---- audit-1105 (.superpowers/sdd/audit-1105-bugs.md) ----
def test_trailing_comment_in_check_and_scope(box):  # audit #2: wrappers survive a trailing "-- note"
    checks = [Check(name="yes", sql="SELECT count(*) > 0 FROM invoices -- every invoice")]
    assert run_checks(box, checks) == {"yes": True}
    plain = scope_pks(box, E8_SCOPE)
    assert plain and scope_pks(box, {**E8_SCOPE, "where": E8_SCOPE["where"] + " -- aged 90 days"}) == plain


def test_constant_check_hidden_from_rejected():  # audit #5: "from" in a comment or string is not a table read
    for sql in ("SELECT true -- from", "SELECT 'from' = 'from'", "SELECT true /* from */",
                'SELECT true AS "from"', 'SELECT 1 "from"', 'SELECT 1 "a""from"'):  # review-1105 A5
        with pytest.raises(ValidationError, match="FROM"):
            Check(name="a", sql=sql)


def test_ddl_scan_ignores_literals():  # audit #6
    eff = diff({t: {} for t in S.TABLES}, {t: {} for t in S.TABLES})
    c = contract(["invoices"], {"invoices": [0, 5]}, {"table": "invoices", "where": "false"})
    assert "ddl" not in kinds(compare(c, eff, set(), OK, {}, sql="UPDATE credits SET reason = 'promo; drop later'"))
    # "--" inside a literal is not a comment: it must not hide the DROP after it
    assert "ddl" in kinds(compare(c, eff, set(), OK, {}, sql="UPDATE credits SET reason = 'a--b'; DROP TABLE credits"))


def test_postgres_image_pinned_by_digest():  # audit-1105 mistakes #2: hashint4 values need one exact image
    import yaml
    from sandbox.container import IMAGE
    svc = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["live-db"]
    assert "@sha256:" in IMAGE and svc["image"] == IMAGE


def test_eval_money_moved_matches_golden_amounts():  # review-1105 H1: the eval scores money, measured on real SQL
    from eval.run_eval import money_moved
    for tid, g in GOLDEN.items():
        if "expected_stop" in g:
            continue
        b = start_run_container()
        try:
            assert money_moved(measure(b, g["sql"])) == g["expected_amount_cents"], tid
        finally:
            destroy(b)
