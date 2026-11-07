"""Change contract schema, deterministic compare (D8) and approval classes (D15)."""
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.measure import POLICIES, Effect, SchemaChanged, one_statement
from seed.seed import TABLES

POLICY_TEXT = {p["id"]: p["text"] for p in POLICIES}
MAX_REVISIONS = 2      # D8 step 7: revise <= 2
CLASS_A_MAX_ROWS = 50  # D15: class A = measured changed rows <= 50
# Picked, no source: the most money one ops approval may move. E1's $1,003.00 refund stays A. Single-pass note:
# one row of any value used to be class A.
CLASS_A_MAX_CENTS = 250_000


class ScopeRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str
    where: str = Field(min_length=1)

    @field_validator("where")
    @classmethod
    def single(cls, v):
        return one_statement(v)  # ValueError -> ValidationError, fed back to the model as a contract error


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    sql: str = Field(min_length=1)

    @field_validator("sql")
    @classmethod
    def reads_data(cls, v):
        # a check with no FROM ("SELECT true") is a constant: it passes whatever the SQL did (judge HIGH 1)
        # "from" in a comment, string or "quoted identifier" is not a read (review-1105 A5)
        if not re.search(r"\bfrom\b", QUOTED_IDENT.sub(" ", COMMENT.sub(" ", v)), re.I):
            raise ValueError("check sql must read a table (no FROM = constant check)")
        return one_statement(v)  # measure.run_checks also runs it in a read-only transaction


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tables: list[str] = Field(min_length=1)
    expected_rows: dict[str, tuple[int, int]]
    scope_rule: ScopeRule
    checks: list[Check] = Field(min_length=2, max_length=4)  # D8: checks[2-4]

    @field_validator("tables")
    @classmethod
    def known_tables(cls, v):
        bad = [t for t in v if t not in TABLES]
        if bad:
            raise ValueError(f"unknown tables {bad}")
        return v

    @field_validator("expected_rows")
    @classmethod
    def ranges(cls, v):
        bad = {t: r for t, r in v.items() if not 0 <= r[0] <= r[1]}
        if bad:
            raise ValueError(f"need 0 <= min <= max, got {bad}")
        return v

    @model_validator(mode="after")
    def consistent(self):
        if set(self.expected_rows) != set(self.tables):
            raise ValueError("expected_rows must give one [min, max] per listed table")
        if self.scope_rule.table not in self.tables:
            raise ValueError("scope_rule.table must be one of tables")
        names = [c.name for c in self.checks]
        if len(set(names)) != len(names):  # check_results is keyed by name: a duplicate would hide a failing check
            raise ValueError(f"duplicate check names {sorted({n for n in names if names.count(n) > 1})}")
        return self


def parse_contract(text: str) -> Contract:
    """Raises pydantic.ValidationError naming every bad field."""
    return Contract.model_validate_json(text)


# Task 8 filter. Allow-list by leading keyword, so a statement-leading SET, RESET, ALTER, DO, COPY, CALL or PREPARE never
# reaches the sandbox. DROP/CREATE/TRUNCATE stay allowed only so compare() names them red (kind ddl); the executor has no
# rights for them. FORBIDDEN applies anywhere; quotes are removed so "set_config"(...) is caught. ponytail: a U&"..."
# identifier could dodge this; the backstop kill and the REVOKEs in sandbox/template.py hold regardless.
READ_LEAD = re.compile(r"\s*\(*\s*(SELECT|WITH)\b", re.I)
RUN_LEAD = re.compile(r"\s*(UPDATE|INSERT|DELETE|MERGE|WITH|DROP|CREATE|TRUNCATE)\b", re.I)
FORBIDDEN = re.compile(r"\b(set_config|pg_sleep\w*|lo_\w+|loread|lowrite)\s*\(", re.I)


def refused(sql: str, lead=None) -> str | None:
    """Why this model-written SQL may not run, or None. Literals and comments are blanked first (COMMENT)."""
    text = COMMENT.sub(" ", sql).replace('"', "")
    m = FORBIDDEN.search(text)
    if m:
        return f"{m[1]} is not allowed"
    if lead is not None and not lead.match(text):
        return "this statement type is not allowed here"
    return None


# statement-start DDL; the executor has no DDL rights (sandbox/template.py grants DML only), so this names the attempt
# Comments are blanked first so "-- note\nDROP ..." and "/* x */ DROP ..." still start a statement with DROP.
# ponytail: DDL inside DO $$ EXECUTE '...' $$ is not seen here; the executor's missing rights still deny it.
DDL = re.compile(r"(^|;)\s*(DROP|ALTER|CREATE|TRUNCATE)\b", re.I)
# Single-quoted literals are blanked in the same left-to-right pass (audit-1105 #5/#6): "'a--b'" is not a comment,
# "'promo; drop later'" is not DDL. ponytail: E'\'' escapes and $$ bodies are not parsed; add if a golden needs them.
QUOTED_IDENT = re.compile(r'"(?:[^"]|"")*"')  # reads_data only; the DDL scan does not need it
COMMENT = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.S)


@dataclass(frozen=True)
class Failure:
    kind: str  # tables | row_count | scope | check | policy | cap | ddl
    detail: str
    promised: tuple | None = None        # row_count: contract (min, max)
    measured: int | None = None          # row_count: changed rows; policy: violations after the SQL
    before: int | None = None            # policy: violations before the SQL
    out_of_scope_ids: list | None = None  # scope: every changed pk outside scope_rule
    rows: list | None = None             # policy: detail rows (customer, owed_cents, arr_cents)
    text: str | None = None              # policy: the plain-English rule from policies.yaml (panel header)


@dataclass(frozen=True)
class Verdict:
    green: bool
    failures: list = field(default_factory=list)
    warnings: list = field(default_factory=list)  # not red, but blocks class A (e.g. a scope rule that scopes nothing)


def compare(contract: Contract, effect: Effect, scope_pks: set, check_results: dict, policy_results: dict,
            cap: str | None = None, policy_before: dict | None = None, sql: str | None = None) -> Verdict:
    """check_results {name: bool} (measure.run_checks); policy_results / policy_before {id: PolicyResult} after /
    before the SQL; cap = sandbox.runner.cap_label(code) if a Postgres cap stopped the SQL (the brief lists kind
    "cap" but no input for it); sql = the measured SQL, scanned for DDL."""
    f, warn = [], []
    if cap:
        f.append(Failure("cap", f"stopped by {cap}"))
    if sql and DDL.search(COMMENT.sub(" ", sql)):
        f.append(Failure("ddl", "schema change (DROP/ALTER/CREATE/TRUNCATE) is not a data fix"))
    extra = sorted(set(effect.rows_by_table) - set(contract.tables))
    if extra:
        f.append(Failure("tables", f"changed tables outside the contract: {', '.join(extra)}"))
    for t in contract.tables:
        lo, hi = contract.expected_rows[t]
        n = effect.changed_count(t)
        if not lo <= n <= hi:
            f.append(Failure("row_count", f"{t}: contract {lo}-{hi} rows, measured {n}", promised=(lo, hi),
                             measured=n))
    st = contract.scope_rule.table
    pks = effect.changed_pks.get(st, {})
    out = sorted(set(pks.get("updated", []) + pks.get("deleted", [])) - scope_pks)
    if out:
        # 10 ids in the text keeps the fed-back line short; the full list is in out_of_scope_ids
        shown = ", ".join(map(str, out[:10])) + (" …" if len(out) > 10 else "")
        f.append(Failure("scope", f"{st}: {len(out)} changed rows outside scope_rule (ids {shown})",
                         out_of_scope_ids=out))
    total = effect.table_rows.get(st, 0)
    if total and len(scope_pks) >= total:
        # not red: a whole-table re-rate (E11) is legitimate, but such a scope rule proves nothing about scope
        warn.append(f"scope_rule matches every row of {st} ({len(scope_pks)}/{total}): it does not limit the change")
    elif len(scope_pks) > contract.expected_rows[st][1]:
        # e.g. "id <> 1": a scope wider than the promised max lets extra rows change without a scope failure
        warn.append(f"scope_rule matches {len(scope_pks)} rows of {st}, more rows than the contract's max "
                    f"{contract.expected_rows[st][1]}: it does not limit the change to the promise")
    for c in contract.checks:
        if check_results.get(c.name) is not True:  # a missing result is a failure, never a pass
            f.append(Failure("check", f"{c.name} did not return true"))
    for pid, r in sorted(policy_results.items()):
        # no before-state given = 0 before (fail closed); a violation that already existed is not this change's
        b = policy_before[pid].count if policy_before and pid in policy_before else 0
        if r.count > b:
            f.append(Failure("policy", f"{pid}: violations {b} → {r.count}", measured=r.count, before=b,
                             rows=r.rows, text=POLICY_TEXT.get(pid)))
    return Verdict(not f, f, warn)


def schema_changed_verdict(err: SchemaChanged) -> Verdict:
    """Contract for callers: measure.snapshot_hashes raises SchemaChanged when a seed table vanished; compare() never
    sees it (no Effect exists), so the caller maps it here to a named red verdict (class C), never a crash."""
    return Verdict(False, [Failure("ddl", f"schema changed during the run: {err}")])


def approval(verdict: Verdict, trace, source_kind: str) -> tuple[str, str]:
    """(class, reason). trace needs .effect (final Effect), .verdicts (every attempt's Verdict), .revisions (int).
    source_kind: "ticket" (card) or "text" (free text). Class is A/B/C, or "no-op" when nothing changed."""
    if not verdict.green:
        return "C", "red verdict: " + ", ".join(sorted({x.kind for x in verdict.failures}))
    capped = getattr(trace, "capped", None)
    if capped:  # B3: a green result after a cap fired covers only part of the work: never approvable
        return "C", f"stopped by {capped}: larger than the per-run cap, needs a batched job"
    if trace.revisions >= MAX_REVISIONS:
        return "C", f"{trace.revisions} revisions used: too many retries to trust a green run"
    eff = trace.effect
    rows = sum(eff.changed_count(t) for t in eff.rows_by_table)
    if rows == 0:
        return "no-op", "nothing changed: nothing to approve"
    why = []
    if len(eff.rows_by_table) != 1:
        why.append(f"{len(eff.rows_by_table)} tables changed")
    if rows > CLASS_A_MAX_ROWS:
        why.append(f"{rows} rows > {CLASS_A_MAX_ROWS}")
    cents = _money_moved(eff)
    if cents > CLASS_A_MAX_CENTS:
        why.append(f"${cents / 100:,.2f} moved, over the ops limit of ${CLASS_A_MAX_CENTS / 100:,.2f}")
    if any(x.kind == "policy" for v in trace.verdicts for x in v.failures):
        why.append("a policy failed in an earlier attempt")
    if trace.revisions:
        why.append(f"{trace.revisions} revision used")
    if source_kind != "ticket":
        why.append("free-text request")
    if verdict.warnings:
        why += verdict.warnings
    return ("B", "; ".join(why)) if why else ("A", f"one table, {rows} row{'' if rows == 1 else 's'}, first attempt, ticket card")


def _money_moved(eff) -> int:
    """Largest money figure the change moves: the delta of a column it wrote, or the value of rows whose money column
    it did not write (a write-off marks the whole amount). Mixed charge/refund columns have no sum, so they count 0."""
    out = 0
    for m in (getattr(eff, "money_cents_by_column", None) or {}).values():
        v = m.delta_cents if m.column_changed else m.after_cents
        out = max(out, abs(v or 0))
    return out


def approval_class(verdict: Verdict, trace, source_kind: str) -> str:
    return approval(verdict, trace, source_kind)[0]
