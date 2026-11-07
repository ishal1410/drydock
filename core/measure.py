"""Measure what SQL actually changed: per table, per primary key, per *_cents column. Deterministic code only."""
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from sandbox.container import RunBox
from sandbox.runner import psql, run_sql
from seed.seed import TABLES

POLICIES = yaml.safe_load((Path(__file__).resolve().parent.parent / "policy" / "policies.yaml")
                          .read_text(encoding="utf-8"))
SAMPLE_ROWS = 5  # D8: before -> after table shows up to 5 changed rows per table
# column_summary lists each before->after pair while a column has <= SAMPLE_ROWS distinct pairs (the same D8 bound
# as the sample, so the summary never has more pair lines than the table beside it has rows). Past that, a numeric
# column whose every row moved by one ratio (within whole-cent rounding) reads "×0.9 each ×68", so the ratio the
# model and the judge need survives; anything else reads "N distinct changes ×M".
# Headline money line, exactly one per changed table (MoneyDelta.headline): the *_cents column the SQL wrote (the
# largest |delta| if it wrote several), else, on a status change, the table's amount column below. accounts has none
# on purpose: closing an account moves no money by itself; the cancelled subscriptions carry it (E5 golden 9800).
AMOUNT_COLUMN = {"invoices": "amount_cents", "subscriptions": "monthly_cents", "payments": "amount_cents",
                 "credits": "amount_cents"}


class SchemaChanged(RuntimeError):
    """A seed table vanished between snapshots (DDL); the caller shows it as a red failure, never a crash."""


@dataclass(frozen=True)
class MoneyDelta:
    before_cents: int | None  # None when direction == "mixed": charges + refunds summed by stored sign mean nothing
    after_cents: int | None
    delta_cents: int | None
    column_changed: bool  # False = the SQL never wrote this column; before == after = value of the changed rows
    rows: int             # how many changed rows the sums cover
    scope_label: str      # "across the 68 changed rows" / "value of the 87 changed rows"; never a table total
    null_rows: int = 0    # rows with a NULL here, before or after (summed as 0, counted so the UI can flag them)
    direction: str | None = None  # payments only: "out" = refunds, "in" = charges, "mixed"; stored sign untouched
    headline: bool = False  # the one line per table the panel shows big; the rest go in the detail table


@dataclass(frozen=True)
class PolicyResult:
    count: int   # violating rows; 0 = policy holds
    rows: list   # detail_sql rows (P-7: customer, owed_cents, arr_cents); [] when the policy has no detail_sql


@dataclass(frozen=True)
class Effect:
    rows_by_table: dict    # {table: {"count_delta": int, "touched": int}}, changed tables only (golden.json shape)
    changed_pks: dict      # {table: {"inserted": [pk], "updated": [pk], "deleted": [pk]}}
    sample: dict           # {table: [{"pk", "op", "before", "after", "changed": [col]}]} <= SAMPLE_ROWS each
    column_summary: list   # ["invoices.status: open→written_off ×87", ...]
    money_cents_by_column: dict  # {"table.col": MoneyDelta} over changed pks only
    table_rows: dict = field(default_factory=dict)  # {table: row count before the change}

    def changed_count(self, table: str) -> int:
        return sum(len(v) for v in self.changed_pks.get(table, {}).values())


def usd(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def snapshot_hashes(box: RunBox) -> dict:
    """{table: {pk: (xmin, row)}} for every seed table, read by the runner's admin role (trusted code, not model SQL).

    Change signal = xmin: any UPDATE writes a new row version, even one that sets the same value, so the count
    matches the Postgres command tag and golden.json "touched" (which test_seed measures by xmin).
    Assumes every seed table has a unique integer `id` (seed/schema.sql: all `id serial PRIMARY KEY`): a non-int id
    raises ValueError, a duplicate id (json_object_agg would collapse it) raises RuntimeError.
    """
    p = psql(box.id, box.sock_admin, SNAPSHOT_SQL)
    if p.returncode != 0:
        err = p.stderr.strip()
        raise (SchemaChanged if "does not exist" in err else RuntimeError)(f"snapshot failed: {err}")
    return parse_snapshot(p.stdout)


# ponytail: whole-table export; fine for the synthetic seed (thousands of rows), stream if tables grow
SNAPSHOT_SQL = "SELECT json_build_object({})".format(", ".join(
    f"'{t}', (SELECT json_build_array(count(*), coalesce(json_object_agg(id, json_build_array(xmin::text, "
    f"row_to_json(t))), '{{}}')) FROM {t} t)" for t in TABLES))


def parse_snapshot(stdout: str) -> dict:
    """SNAPSHOT_SQL output -> {table: {pk: (xmin, row)}}. Last line, so the apply can run it after DML in one session."""
    raw = json.loads(stdout.strip().splitlines()[-1])
    out = {}
    for t in TABLES:
        n, rows = raw[t]
        if len(rows) != n:
            raise RuntimeError(f"{t}: {n} rows but {len(rows)} distinct ids")
        out[t] = {int(pk): (x, row) for pk, (x, row) in rows.items()}
    return out


def checksum_by_table(box: RunBox) -> dict:
    """{table: sha256 of its rows by pk}. Content only (no xmin), so a restore or a reset to the same data gives the
    same value. Lets the UI show which tables an apply changed and that every other table stayed identical."""
    snap = snapshot_hashes(box)
    return {t: hashlib.sha256(json.dumps(sorted((pk, row) for pk, (_, row) in snap[t].items()),
                                         sort_keys=True).encode("utf-8")).hexdigest() for t in TABLES}


def combine(by_table: dict) -> str:
    """Whole-DB checksum from the per-table ones (one snapshot export gives both)."""
    return hashlib.sha256(json.dumps(by_table, sort_keys=True).encode("utf-8")).hexdigest()


def checksum(box: RunBox) -> str:
    """Live-DB checksum: sha256 over every seed table's content checksum."""
    return combine(checksum_by_table(box))


def _direction(rows: list) -> str | None:
    # only payments carries `kind`; a refund row is stored positive but is money going out
    kinds = {r.get("kind") for r in rows if "kind" in r}
    if not kinds:
        return None
    return {frozenset({"refund"}): "out", frozenset({"charge"}): "in"}.get(frozenset(kinds), "mixed")


def _ratio_line(t: str, c: str, cnt: Counter) -> str | None:
    xs = [(x, y) for (x, y), n in cnt.items() for _ in range(n)]
    if not all(isinstance(x, int) and isinstance(y, int) for x, y in xs) or not sum(x for x, _ in xs):
        return None
    r = round(sum(y for _, y in xs) / sum(x for x, _ in xs), 4)
    if all(abs(y - x * r) <= 0.5 for x, y in xs):  # 0.5 = each row rounded to a whole cent
        return f"{t}.{c}: ×{r:g} each ×{len(xs)}"
    return None


def _added_line(t: str, sign: str, verb: str, rows: list) -> str:
    kinds = {r.get("kind") for r in rows if "kind" in r}
    line = f"{t}: {sign}{len(rows)} {verb}" + (f" ({next(iter(kinds))})" if len(kinds) == 1 else "")
    cents = sorted({c for r in rows for c in r if c.endswith("_cents")})
    return line + "".join(f" · {c} {usd(sum(r.get(c) or 0 for r in rows))}" for c in cents)


def diff(before: dict, after: dict) -> Effect:
    rows, pks, sample, summary, money = {}, {}, {}, [], {}
    for t in TABLES:
        b, a = before[t], after[t]
        ins = sorted(a.keys() - b.keys())
        dele = sorted(b.keys() - a.keys())
        upd = sorted(k for k in a.keys() & b.keys() if a[k][0] != b[k][0])
        if not (ins or dele or upd):
            continue
        rows[t] = {"count_delta": len(a) - len(b), "touched": len(ins) + len(upd)}
        pks[t] = {"inserted": ins, "updated": upd, "deleted": dele}
        changed = sorted([(k, "inserted") for k in ins] + [(k, "updated") for k in upd] + [(k, "deleted") for k in dele])
        sample[t] = []
        for k, op in changed[:SAMPLE_ROWS]:
            rb, ra = b[k][1] if k in b else None, a[k][1] if k in a else None
            cols = sorted((rb or {}).keys() | (ra or {}).keys())
            sample[t].append({"pk": k, "op": op, "before": rb, "after": ra,
                              "changed": [c for c in cols if (rb or {}).get(c) != (ra or {}).get(c)]})
        pairs, same = {}, 0
        for k in upd:
            diffs = [(c, b[k][1].get(c), v) for c, v in a[k][1].items() if b[k][1].get(c) != v]
            same += not diffs
            for c, x, y in diffs:
                pairs.setdefault(c, Counter())[(x, y)] += 1
        for c, cnt in sorted(pairs.items()):
            if len(cnt) <= SAMPLE_ROWS:
                summary += [f"{t}.{c}: {x}→{y} ×{n}" for (x, y), n in cnt.most_common()]
            else:
                summary.append(_ratio_line(t, c, cnt) or f"{t}.{c}: {len(cnt)} distinct changes ×{sum(cnt.values())}")
        if same:  # rows written with identical values still count toward the measured total (xmin)
            summary.append(f"{t}: rewritten, unchanged ×{same}")
        if ins:
            summary.append(_added_line(t, "+", "inserted", [a[k][1] for k in ins]))
        if dele:
            summary.append(_added_line(t, "−", "deleted", [b[k][1] for k in dele]))
        status_rows = [k for k in upd if b[k][1].get("status") != a[k][1].get("status")]
        cents = sorted({c for k in ins + upd for c in a[k][1]} | {c for k in dele + upd for c in b[k][1]})
        tmoney = {}
        for c in (c for c in cents if c.endswith("_cents")):
            if ins or dele or any(b[k][1].get(c) != a[k][1].get(c) for k in upd):
                bk, ak, label, changed_col = upd + dele, upd + ins, "across the {} changed row{}", True
            elif status_rows:  # column untouched but the rows' status moved: show what those rows are worth
                bk, ak, label, changed_col = status_rows, status_rows, "value of the {} changed row{}", False
            else:
                continue  # untouched column on rows whose status did not move: noise, hidden
            nulls = {k for k in bk if b[k][1].get(c) is None} | {k for k in ak if a[k][1].get(c) is None}
            bs = sum(b[k][1].get(c) or 0 for k in bk)  # inserted rows count 0 before
            as_ = sum(a[k][1].get(c) or 0 for k in ak)  # deleted rows count 0 after
            n = len(set(bk) | set(ak))
            d = _direction([b[k][1] for k in bk] + [a[k][1] for k in ak])
            if d == "mixed":
                bs = as_ = None
            tmoney[c] = MoneyDelta(bs, as_, None if bs is None else as_ - bs, changed_col, n,
                                   label.format(n, "" if n == 1 else "s"), len(nulls), d)
        written = [c for c, m in tmoney.items() if m.column_changed]
        top = (max(written, key=lambda c: (abs(tmoney[c].delta_cents or 0), c)) if written
               else AMOUNT_COLUMN.get(t) if AMOUNT_COLUMN.get(t) in tmoney else None)
        for c, m in tmoney.items():
            money[f"{t}.{c}"] = replace(m, headline=c == top)
    return Effect(rows, pks, sample, summary, money, {t: len(before[t]) for t in TABLES})


def one_statement(sql: str) -> str:
    """Model check / scope text: one statement, trailing ';' dropped (models often add one). Any other ';' is
    rejected, so the text cannot close the read-only wrapper and run a second statement."""
    sql = sql.strip().rstrip(";").rstrip()
    if ";" in sql:
        raise ValueError("must be one statement (no ';')")
    return sql


def run_read_only(box: RunBox, sql: str):
    """Model-written SQL that must only observe: one statement in a READ ONLY transaction, always rolled back.
    Writes (DML, data-modifying CTEs, nextval) fail with 25006 instead of changing the copy."""
    # newlines: a trailing "-- note" ends at its line and cannot comment out the wrapper (audit-1105 #2)
    return run_sql(box, f"BEGIN TRANSACTION READ ONLY;\n{one_statement(sql)}\n;\nROLLBACK")


def scope_pks(box: RunBox, scope_rule: dict) -> set:
    """Pre-state pks matching the model's scope rule. The WHERE is model text: executor role, read-only."""
    if scope_rule["table"] not in TABLES:
        raise ValueError(f"unknown table {scope_rule['table']!r}")
    r = run_read_only(box, f"SELECT id FROM {scope_rule['table']} WHERE ({one_statement(scope_rule['where'])}\n)")
    if r.code == "25006":
        raise RuntimeError(f"scope rule tried to write (read-only transaction): {r.error}")
    if not r.ok:
        raise RuntimeError(f"scope rule failed: {r.error}")
    lines = r.stdout.split()
    if not all(re.fullmatch(r"\d+", x) for x in lines):
        raise RuntimeError("scope rule returned non-pk output")
    return {int(x) for x in lines}


def policy_results(box: RunBox) -> dict:
    """{policy id: PolicyResult} in the current state. Run it before and after the SQL: compare() fails a policy
    only when this change added violations."""
    out = {}
    for p in POLICIES:
        r = run_sql(box, p["check_sql"])
        if not r.ok:
            raise RuntimeError(f"{p['id']} check failed: {r.error}")
        detail = []
        if p.get("detail_sql"):
            d = run_sql(box, f"SELECT coalesce(json_agg(row_to_json(v)), '[]') FROM ({p['detail_sql']}) v")
            if not d.ok:
                raise RuntimeError(f"{p['id']} detail failed: {d.error}")
            detail = json.loads(d.stdout)
        out[p["id"]] = PolicyResult(int(r.stdout.strip()), detail)
    return out


def run_checks(box: RunBox, checks) -> dict:
    """{check name: bool}. psql -At prints booleans as t/f; only "t" is True, so an erroring check is False.
    Checks are model text: read-only transaction, so a check that writes errors (False) and changes nothing."""
    out = {}
    for c in checks:
        r = run_read_only(box, c.sql)
        out[c.name] = r.ok and r.stdout.strip() == "t"
    return out
