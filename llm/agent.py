"""Change contract agent loop (plan D8, Task 5): read -> lookups -> contract (locked) -> SQL -> run -> compare ->
(revise, up to 2) -> explain. The model only ever talks through llm.client.chat. No cache, no replay (plan S1).

Tool calling and response_format have never run against a real endpoint. Both are asked for first; if the endpoint
rejects either with a 4xx, the run drops that one parameter and carries on with JSON in the message content, which the
prompt already describes (RunTrace.fallbacks records it). A model that ignores them is read the same way.
"""
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from core.contract import (
    COMMENT,
    MAX_REVISIONS,
    POLICY_TEXT,
    READ_LEAD,
    RUN_LEAD,
    Contract,
    Failure,
    Verdict,
    approval,
    compare,
    parse_contract,
    refused,
    schema_changed_verdict,
)
from core.measure import (
    Effect,
    SchemaChanged,
    diff,
    one_statement,
    policy_results,
    run_checks,
    run_read_only,
    scope_pks,
    snapshot_hashes,
    usd,
)
from llm.client import ModelError, ModelRequestError, RunBudget, chat
from sandbox.container import STATEMENT_TIMEOUT, destroy, start_run_container
from sandbox.runner import STOPPED_BY, cap_label, run_sql
from seed.seed import ASOF

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PROMPT = dict(re.findall(r"^=== (\w+) ===\n(.*?)(?=^=== |\Z)", (ROOT / "llm" / "prompt.txt").read_text(encoding="utf-8"),
                         re.S | re.M))
MAX_LOOKUPS = 3  # D8: "Lookups (1-3)", per phase
CAP_MS = int(STATEMENT_TIMEOUT.rstrip("s")) * 1000
CHECK_AUTHOR = {"check": "AI-written"}  # N4, default "DryDock-checked"; exported for Task 12
USAGE_RATED = "usage_events.amount_cents is the rated amount, so no rate is needed."

LOOKUP_TOOL = {"type": "function", "function": {
    "name": "lookup", "description": "Run a read-only SELECT of the rows themselves. Returns only their count and the "
                                     "sum of each numeric column, never row values.",
    "parameters": {"type": "object", "properties": {"select_sql": {"type": "string"}}, "required": ["select_sql"]}}}
POLICY_TOOL = {"type": "function", "function": {
    "name": "lookup_policy", "description": "Read the text of a policy a failure named.",
    "parameters": {"type": "object", "properties": {"policy_id": {"type": "string"}}, "required": ["policy_id"]}}}
SQL_SCHEMA = {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
EXPLAIN_SCHEMA = {"type": "object", "properties": {"explanation": {"type": "string"}, "reply": {"type": "string"}},
                  "required": ["explanation", "reply"]}
# LOOKUP_SQL's only output is numbers; _numbers() re-checks that in code, so a lookup cannot carry row text back.
# Ids are never summed: live E1 read sum(invoice_id) = 202 over two rows of invoice 101 as an invoice id.
LOOKUP_SQL = """WITH q AS (
{sql}
)
SELECT json_build_object('count', (SELECT count(*) FROM q), 'sums', (SELECT coalesce(json_object_agg(k, s), '{{}}')
FROM (SELECT e.key AS k, sum(e.value::numeric) AS s FROM q, jsonb_each_text(to_jsonb(q)) e
WHERE jsonb_typeof(to_jsonb(q) -> e.key) = 'number' AND e.key <> 'id' AND e.key NOT LIKE '%\\_id'
GROUP BY e.key) x))"""
EMPTY_EFFECT = Effect({}, {}, {}, [], {})
DOLLARS = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
# a bare number: not inside an identifier or date (INV-10101, P-7, 2026-08-03), not a piece of a $ amount
NUMBER = re.compile(r"(?<![\w$.,-])\d[\d,]*(?:\.\d+)?(?![\w-])")
CLAUSE_END = re.compile(r"\b(?:WHERE|FROM|RETURNING)\b", re.I)
UPDATE_SET = re.compile(r"\bUPDATE\s+[\w.\"]+(?:\s+(?:AS\s+)?\w+)?\s+SET\b", re.I)
THINK = re.compile(r"<think>.*?</think>", re.S)


@dataclass
class Step:
    n: int
    name: str
    actor: str  # model | sandbox | drydock
    payload: dict
    service: str    # what really did this step; never claims Vultr or the VM unless true
    started_at: datetime
    ms: int | None = None
    tokens: int | None = None      # pin 6: tokens_in + tokens_out, the eval's number
    model_id: str | None = None


@dataclass
class ModelStep(Step):  # a separate type so non-model steps have no tokens_in key (orch/runs.py _trace_view tests it)
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class RunTrace:
    steps: list = field(default_factory=list)
    verdict: Verdict | None = None
    explanation: str = ""
    reply: str = ""
    effect: Effect = EMPTY_EFFECT       # the last measured Effect (never None: the eval reads it on every path)
    verdicts: list = field(default_factory=list)  # one per compare; a rejected contract or SQL has no compare
    revisions: int = 0
    contract_sha: str | None = None     # v1's; each contract step payload holds its own sha256 and locked_at
    contract_locked_at: datetime | None = None
    sql: str | None = None              # the last SQL that ran, normalised by one_statement (what Approve applies)
    approval_class: str | None = None
    approval_reason: str | None = None
    fallbacks: list = field(default_factory=list)  # names of provider parameters dropped after a 4xx
    capped: str | None = None           # the cap label that stopped a lookup or the SQL: the run can never be approved
    boxes: list = field(default_factory=list)  # revise boxes: {id, created_at, destroyed_at or None if not confirmed}


def author(kind: str) -> str:
    return CHECK_AUTHOR.get(kind, "DryDock-checked")


def contract_sha(contract: Contract) -> str:
    return hashlib.sha256(json.dumps(contract.model_dump(mode="json"), sort_keys=True).encode("utf-8")).hexdigest()


def _numbers(stdout: str) -> dict | None:
    try:
        d = json.loads(stdout.strip().splitlines()[-1])
        count, sums = d["count"], d["sums"]
    except (ValueError, IndexError, KeyError, TypeError):
        return None
    good = isinstance(count, int) and isinstance(sums, dict) and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in sums.values())
    return {"count": count, "sums": sums} if good else None


def lookup(box, select_sql: str):
    """(RunResult, {count, sums} or None). Plan D8: executor role, read-only, numbers only."""
    r = run_read_only(box, LOOKUP_SQL.format(sql=one_statement(select_sql)))
    return r, _numbers(r.stdout) if r.ok else None


def set_columns(sql: str) -> list:
    """Column names in every UPDATE ... SET list of the SQL (M1), literals and comments ignored."""
    text, cols = COMMENT.sub(" ", sql), []
    for m in UPDATE_SET.finditer(text):
        rest, depth, start, parts = text[m.end():], 0, 0, []
        for i, ch in enumerate(rest):
            depth += (ch == "(") - (ch == ")")
            if depth < 0 or (depth == 0 and CLAUSE_END.match(rest, i)):
                parts.append(rest[start:i])
                break
            if depth == 0 and ch == ",":
                parts.append(rest[start:i])
                start = i + 1
        else:
            parts.append(rest[start:])
        for p in parts:
            cols += re.findall(r"\w+", p.partition("=")[0])
    return cols


def _unchecked(contract: Contract, sql: str) -> list:
    """M1: names of checks that mention no column the SQL's SET list writes. No SET list (INSERT, DELETE): no rule."""
    cols = set_columns(sql)
    return [c.name for c in contract.checks if cols and not any(
        re.search(rf"\b{re.escape(col)}\b", COMMENT.sub(" ", c.sql), re.I) for col in cols)]


def _first_line(err: str | None) -> str:
    """Postgres's own message. DETAIL lines can hold row values, which never go to the model (plan D8)."""
    return (err or "").strip().splitlines()[0] if (err or "").strip() else "no error text"


def _mentions(sql: str, table: str) -> bool:
    """N1: does this lookup read the table? Any mention outside literals and comments counts, so FROM public.t, a JOIN,
    a comma join and a subquery all back the range alike (B8). The count must still fall in the range."""
    return bool(re.search(rf"\b{re.escape(table)}\b", COMMENT.sub(" ", sql), re.I))


def _flat(contract: Contract) -> dict:
    d = contract.model_dump(mode="json")
    out = {"tables": d["tables"], "scope_rule.table": d["scope_rule"]["table"], "scope_rule.where": d["scope_rule"]["where"]}
    out.update({f"expected_rows.{t}": r for t, r in d["expected_rows"].items()})
    out.update({f"checks.{c['name']}": c["sql"] for c in d["checks"]})
    return out


def _contract_diff(old: Contract, new: Contract) -> list:
    a, b, out = _flat(old), _flat(new), []
    for k in [*a, *(k for k in b if k not in a)]:
        if a.get(k) != b.get(k):
            out += ([f"- {k}: {a[k]}"] if k in a else []) + ([f"+ {k}: {b[k]}"] if k in b else [])
    return out


def _json_in(text: str):
    """The JSON object in a reply, with or without fences, reasoning tags or prose around it. None if there is none."""
    text = THINK.sub("", text)
    start, end = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[start:end + 1], strict=False) if start >= 0 else None  # strict=False: raw newlines
    except ValueError:
        return None


def _calls(reply) -> list:
    """Tool requests as (id or None, name, args or None). Native tool_calls (pin 9: content is null), else JSON in content."""
    native = reply.message.get("tool_calls") or []
    if native:
        out = []
        for c in native:
            fn = c.get("function") or {}
            args = fn.get("arguments")
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except ValueError:
                args = None
            out.append((c.get("id"), fn.get("name"), args if isinstance(args, dict) else None))
        return out
    d = _json_in(reply.text)
    return [(None, d["tool"], d)] if isinstance(d, dict) and d.get("tool") else []


def _inference_label(host: str, model_id: str) -> str:
    """Names the host that really answered (Reply.host), so a fallback or a changed base URL never claims Vultr."""
    return f"{'Vultr Serverless Inference' if 'vultr' in host else host or 'model provider'}, {model_id}"


def _on_vm() -> bool:
    return bool(os.environ.get("DRYDOCK_VM_ID"))  # same switch as orch/runs.py _ledger: a laptop never claims the VM


def _measured_facts(trace: RunTrace, ticket: dict, cap: str | None) -> dict:
    """The only thing the explain call sees: counts, the code-computed column summary, money and check results.
    Sums of policy detail rows, never the rows (plan D8 "aggregates only to the model")."""
    e, v = trace.effect, trace.verdict
    earlier = {}
    for verdict in trace.verdicts:
        for f in verdict.failures:
            if f.kind == "policy":
                pid = f.detail.split(":")[0]
                keys = sorted({k for r in f.rows or [] for k in r if k.endswith("_cents")})
                earlier[pid] = {"violating_rows": f.measured, **{k: usd(sum(r.get(k) or 0 for r in f.rows))
                                                                 for k in keys}}
    return {"ticket": ticket.get("body"), "verdict": "green" if v.green else "red", "revisions_used": trace.revisions,
            "rows_by_table": e.rows_by_table, "column_summary": e.column_summary,
            "money": {k: {"before": None if m.before_cents is None else usd(m.before_cents),
                          "after": None if m.after_cents is None else usd(m.after_cents),
                          "delta": None if m.delta_cents is None else usd(m.delta_cents),
                          "rows": m.rows, "label": m.scope_label} for k, m in e.money_cents_by_column.items()},
            "failures": [f.detail for f in v.failures], "warnings": v.warnings, "policy_failures_seen": earlier,
            "stopped_by": cap}


def _money_set(trace: RunTrace) -> set:
    """Cents a reply may quote: a measured value or a sum of one measured column (plan L645). ponytail: per-field
    totals only, not every subset sum; widen it if a real reply quotes a partial sum."""
    money = set()
    for m in trace.effect.money_cents_by_column.values():
        money |= {abs(x) for x in (m.before_cents, m.after_cents, m.delta_cents) if x is not None}
    for v in trace.verdicts:
        for f in v.failures:
            totals = {}
            for r in f.rows or []:
                for k, x in r.items():
                    if k.endswith("_cents") and isinstance(x, int):
                        money.add(abs(x))
                        totals[k] = totals.get(k, 0) + x
            money |= {abs(x) for x in totals.values()}
    return money


def _untraced(text: str, money: set, numbers: set) -> list:
    bad = [m for m in DOLLARS.findall(text) if round(float(m.replace(",", "")) * 100) not in money]
    bad += [m for m in NUMBER.findall(text) if float(m.replace(",", "")) not in numbers]
    return bad


class _Run:
    def __init__(self, ticket: dict, transport, budget=None):
        self.ticket, self.transport, self.t = ticket, transport, RunTrace()
        self.budget = budget or RunBudget()
        self.dropped, self.pending, self.last_tick = set(), None, None
        self.last_model, self.last_host, self.last_sql = None, "", None
        schema = (ROOT / "seed" / "schema.sql").read_text(encoding="utf-8")
        system = PROMPT["SYSTEM"].replace("{asof}", ASOF.isoformat()).replace("{schema}", schema)
        self.msgs = [{"role": "system", "content": system}]
        self.policies, self.version, self.contract = set(), 0, None
        self.read_policies = set()  # rule texts the model fetched; compact() restates them on every later revise

    # ---- clock and steps ----
    def tick(self) -> datetime:
        """Strictly increasing UTC: Windows' clock can return one value twice, and the lock must precede the SQL."""
        now = datetime.now(timezone.utc)
        self.last_tick = now if self.last_tick is None or now > self.last_tick else self.last_tick + timedelta(microseconds=1)
        return self.last_tick

    def add(self, name, actor, payload, service, started, ms=None):
        self.t.steps.append(Step(len(self.t.steps) + 1, name, actor, payload, service, started, ms))

    def model_step(self, name, payload, started):
        # no call since the last step (a second policy call in one reply, a failed explain): the step cost nothing
        p = self.pending or {"in": 0, "out": 0, "ms": 0, "model": self.last_model, "host": self.last_host}
        self.pending = None
        self.t.steps.append(ModelStep(len(self.t.steps) + 1, name, "model", payload, _inference_label(p["host"], p["model"]), started,
                                      p["ms"], p["in"] + p["out"], p["model"], p["in"], p["out"]))

    def sandbox_label(self, box) -> str:
        return f"sandbox container {box.id}" + (" on the Vultr VM" if _on_vm() else "")

    def drydock_label(self) -> str:
        return "orchestrator on the Vultr Cloud Compute VM" if _on_vm() else "orchestrator"

    # ---- model calls ----
    def ask(self, messages, tools=None, schema=None, name="reply"):
        """One chat call. A 4xx on a shape parameter drops it for the rest of the run and retries once without it.
        ModelUnavailable and ModelTruncated pass through untouched (pin 8)."""
        kw = {}
        if tools and "tools" not in self.dropped:
            kw["tools"] = tools
        if schema and "response_format" not in self.dropped:
            kw["response_format"] = {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}
        try:
            reply = chat(messages, transport=self.transport, budget=self.budget, **kw)
        except ModelRequestError as e:
            if not kw:
                raise
            self.dropped |= set(kw)
            self.t.fallbacks.append(f"dropped {', '.join(sorted(kw))}")  # parameter names only: this reaches the browser
            log.warning("provider refused %s: %s", sorted(kw), e.detail)
            reply = chat(messages, transport=self.transport, budget=self.budget)
        # usage of a call with no step of its own (a lookup ask, a "DONE") rolls into the next model step, so the
        # eval's tokens per run count every call. ponytail: that step's ms then spans those calls too.
        p = self.pending or {"in": 0, "out": 0, "ms": 0}
        self.pending = {"in": p["in"] + reply.tokens_in, "out": p["out"] + reply.tokens_out, "ms": p["ms"] + reply.ms,
                        "model": reply.model_id, "host": reply.host}
        self.last_model, self.last_host = reply.model_id, reply.host
        return reply

    def say(self, content):
        self.msgs.append({"role": "user", "content": content})

    def heard(self, reply):
        m = {"role": "assistant", "content": reply.text}
        if reply.message.get("tool_calls"):  # only the standard keys: providers differ on what else they return
            m["tool_calls"] = reply.message["tool_calls"]
        self.msgs.append(m)

    # ---- steps ----
    def read(self):
        started = self.tick()
        self.say(f"{PROMPT['READ']}\nTicket from {self.ticket.get('requester', 'a user')}: {self.ticket.get('subject', '')}\n"
                 f"{self.ticket.get('body', '')}")
        # the read needs neither the schema nor the examples: the one call that can go without them
        reply = self.ask([{"role": "system", "content": PROMPT["READ_SYSTEM"].strip()}, self.msgs[-1]])
        self.heard(reply)
        self.model_step("read", {"text": THINK.sub("", reply.text).strip()}, started)

    def run_lookup(self, box, args) -> dict:
        self.budget.check()
        started = self.tick()
        q = (args or {}).get("select_sql")
        p = {"sql": q, "result": None, "ok": False, "code": None, "container_id": box.id,
             "stopped_by": None, "cap_label": None, "stopped_at": None, "elapsed_ms": 0, "cap_ms": CAP_MS, "error": None}
        t0 = time.perf_counter()
        try:
            if not isinstance(q, str):
                raise ValueError("lookup needs select_sql")
            why = refused(q, READ_LEAD)
            if why:
                raise ValueError(f"lookup refused: {why}")
            r, nums = lookup(box, q)
            p.update(ok=r.ok, code=r.code, stopped_by=STOPPED_BY.get(r.code), cap_label=cap_label(r.code),
                     result=nums, error=_first_line(r.error) if not r.ok else None)
            if r.ok and nums is None:
                p.update(ok=False, error="lookup returned something other than numbers: refused")
            if cap_label(r.code):
                p["stopped_at"] = self.tick()
                self.t.capped = cap_label(r.code)
        except ValueError as e:  # pin 4: one_statement before anything reaches the sandbox
            p["error"] = str(e)
        p["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
        self.add("lookup", "sandbox", p, self.sandbox_label(box), started, p["elapsed_ms"])
        if p["cap_label"]:
            return {"error": f"stopped by {p['cap_label']}"}
        return p["result"] if p["ok"] else {"error": p["error"]}

    def run_policy(self, args) -> dict:
        pid = (args or {}).get("policy_id")
        started = self.tick()
        if isinstance(pid, str) and pid in self.policies:  # plan D8: only after a check has named that policy
            out = {"policy_id": pid, "text": POLICY_TEXT[pid]}
            self.read_policies.add(pid)
        else:
            out = {"policy_id": pid, "error": "no failure named that policy"}
        self.model_step("lookup_policy", out, started)
        return out

    def tools_phase(self, box, tools, opening):
        """The model asks, the sandbox answers, until it stops asking or the lookup budget (D8: 1-3) is spent."""
        self.say(opening)
        used, found = 0, False
        for _ in range(MAX_LOOKUPS + len(self.policies) + 1):
            reply = self.ask(self.msgs, tools=tools)
            calls = _calls(reply)
            self.heard(reply)
            if not calls:
                return
            for call_id, name, args in calls:
                if args is None:
                    out = {"error": "tool arguments were not valid JSON"}
                elif name == "lookup" and used < MAX_LOOKUPS:
                    used += 1
                    out = self.run_lookup(box, args)
                    found = found or "error" not in out
                elif name == "lookup_policy":
                    out = self.run_policy(args)
                elif name == "lookup":
                    out = {"error": f"lookup limit reached ({MAX_LOOKUPS})"}
                else:
                    out = {"error": f"unknown tool {name!r}"}
                body = json.dumps(out, default=str)
                if call_id:
                    self.msgs.append({"role": "tool", "tool_call_id": call_id, "content": body})
                else:
                    self.say(f"Result of {name}: {body}")
            # A good lookup goes straight to the contract (no "DONE" round trip), unless a named policy is still unread:
            # live E8 lost the P-7 exclusion when a row lookup ended the phase before the model read the rule.
            if (found or used >= MAX_LOOKUPS) and self.policies <= self.read_policies:
                return

    def ask_contract(self):
        """-> (Contract, None) locked, or (None, error text) as a contract step the scorer counts invalid (pin 6)."""
        started = self.tick()
        self.version += 1
        self.say(PROMPT["CONTRACT"] + ("\n" + PROMPT["EXAMPLES"] if self.version == 1 else ""))  # dropped by compact()
        reply = self.ask(self.msgs, schema=Contract.model_json_schema(), name="change_contract")
        self.heard(reply)
        d = _json_in(reply.text)
        try:
            if d is None:
                raise ValueError("reply held no JSON object")
            contract = parse_contract(json.dumps(d))
        except (ValidationError, ValueError) as e:
            err = f"contract rejected: {e}"
            self.model_step("contract", {"version": self.version, "error": err}, started)
            return None, err
        sha, locked = contract_sha(contract), self.tick()
        p = {"version": self.version, "contract": contract.model_dump(mode="json"), "sha256": sha, "locked_at": locked}
        if self.contract is not None:
            p["diff"] = _contract_diff(self.contract, contract)
        self.model_step("contract", p, started)
        self.contract = contract
        if self.t.contract_sha is None:
            self.t.contract_sha, self.t.contract_locked_at = sha, locked
        return contract, None

    def ask_sql(self) -> str:
        started = self.tick()
        self.say(PROMPT["SQL"])
        reply = self.ask(self.msgs, schema=SQL_SCHEMA, name="sql")
        self.heard(reply)
        d = _json_in(reply.text)
        if isinstance(d, dict) and isinstance(d.get("sql"), str):
            q = d["sql"]
        else:
            q = re.sub(r"^```\w*\s*|\s*```$", "", THINK.sub("", reply.text).strip())
        self.model_step("sql", {"sql": q}, started)
        self.last_sql = q
        return q

    def measure(self, box, contract, q, attempt) -> list:
        """One attempt on one fresh box (plan L625). -> feedback for the revise, [] when green."""
        t = self.t
        self.budget.check()
        before = snapshot_hashes(box)
        try:
            scope = scope_pks(box, contract.scope_rule.model_dump())
        except RuntimeError as e:
            if not str(e).startswith("scope rule"):  # a failed backstop kill: the box is gone, let the caller see it
                raise
            return self.rejected(f"contract rejected: {_first_line(str(e))}")
        pol_before = policy_results(box)
        started, t0 = self.tick(), time.perf_counter()
        r = run_sql(box, q, tag=True)
        elapsed = round((time.perf_counter() - t0) * 1000)
        cap = cap_label(r.code)
        t.capped = cap or t.capped
        tag = r.command_tag if r.ok else "ERROR"
        t.sql = q
        self.add("run", "sandbox", {
            "command_tag": tag, "elapsed_ms": elapsed, "cap_ms": CAP_MS, "ok": r.ok, "stopped_by": STOPPED_BY.get(r.code),
            "code": r.code, "cap_label": cap, "stopped_at": self.tick() if cap else None,
            "error": _first_line(r.error) if not r.ok else None, "denied": r.denied, "container_id": box.id,
            # S4 + B3 of the security audit: no stdout and no DETAIL lines in run state or in the browser; .code stays (pin 6)
            "result": replace(r, stdout="", error=_first_line(r.error) if r.error else None)},
            self.sandbox_label(box), started, elapsed)
        started, t0 = self.tick(), time.perf_counter()
        checks, pol_after = {}, {}
        try:
            after = snapshot_hashes(box)
        except SchemaChanged as e:  # a seed table vanished: no Effect exists, named red (kind ddl), never a crash
            v = schema_changed_verdict(e)
        else:
            t.effect = diff(before, after)
            checks = run_checks(box, contract.checks)
            pol_after = policy_results(box)
            v = compare(contract, t.effect, scope, checks, pol_after, cap=cap, policy_before=pol_before, sql=q)
            if not r.ok and not cap:  # a statement that failed is never green, even if 0 rows were promised
                v = replace(v, green=False, failures=[Failure("check", f"SQL failed: {_first_line(r.error)}"), *v.failures])
            v = replace(v, warnings=v.warnings + self.unbacked(contract))
        t.verdicts.append(v)
        t.verdict = v
        lines = self.lines(contract, v, scope, checks, pol_before, pol_after, cap)
        self.add("compare", "drydock", {"attempt": attempt, "green": v.green, "command_tag": tag, "warnings": v.warnings,
                                        "failures": v.failures, "lines": lines, "effect": t.effect},
                 self.drydock_label(), started, round((time.perf_counter() - t0) * 1000))
        self.policies |= {f.detail.split(":")[0] for f in v.failures if f.kind == "policy"}
        return [f.detail for f in v.failures]

    def rejected(self, err: str) -> list:
        """A contract or SQL refused before it ran: it costs a revision (plan M1); red if no revision is left."""
        self.t.verdict = Verdict(False, [Failure("check", err)])
        self.t.effect = EMPTY_EFFECT  # bugs B21: this attempt changed nothing; explain must not read an earlier one's effect
        return [err]

    # ---- N1: lookup -> promise -> measured ----
    def lookups_for(self, table: str) -> list:
        return [s.payload["result"]["count"] for s in self.t.steps
                if s.name == "lookup" and s.payload["result"] and _mentions(s.payload["sql"], table)]

    def unbacked(self, contract: Contract) -> list:
        return [f"range not backed by a lookup: {t} [{lo},{hi}]" for t, (lo, hi) in contract.expected_rows.items()
                if not any(lo <= n <= hi for n in self.lookups_for(t))]

    def lines(self, contract, v, scope, checks, pol_before, pol_after, cap) -> list:
        """The compare panel: one entry per contract line and always-on check, each tagged with who wrote it (N4)."""
        e, fails = self.t.effect, v.failures

        def line(kind, ok, title, detail=None, **kw):
            return {"kind": kind, "ok": ok, "title": title, "detail": detail, "author": author(kind), "lookup": None,
                    "promised": None, "measured": None, "sql": None, "before": None, "after": None, **kw}

        out = []
        for t in contract.tables:
            lo, hi = contract.expected_rows[t]
            m, seen = e.changed_count(t), self.lookups_for(t)
            n = seen[-1] if seen else None  # latest lookup of this table before this contract's lock (N1)
            out.append(line("row_count", lo <= m <= hi, f"{t} rows changed",
                            f"lookup {n if n is not None else 'none'} → promised [{lo},{hi}] → measured {m}",
                            lookup=n, promised=[lo, hi], measured=m))
        out.append(line("tables", not any(f.kind == "tables" for f in fails), "Only declared tables changed",
                        ", ".join(e.rows_by_table) or "no table changed"))
        pks = e.changed_pks.get(contract.scope_rule.table, {})
        changed = set(pks.get("updated", []) + pks.get("deleted", []))
        out.append(line("scope", not any(f.kind == "scope" for f in fails), "Every change inside scope_rule",
                        f"{len(changed & scope)} of {len(changed)} changed rows match"))
        for c in contract.checks:
            out.append(line("check", checks.get(c.name) is True, c.name, sql=c.sql))
        for pid, r in sorted(pol_after.items()):
            b = pol_before[pid].count
            out.append(line("policy", r.count <= b, pid, POLICY_TEXT.get(pid), before=b, after=r.count))
        if cap:
            out.append(line("cap", False, "Statement stopped by a cap", f"stopped by {cap}"))
        out += [line(f.kind, False, f.detail, f.detail) for f in fails
                if f.kind == "ddl" or f.detail.startswith("SQL failed")]
        return out

    # ---- one attempt ----
    def compact(self) -> str:
        """Before a revise: drop the last attempt's chatter, which every later call would resend, and restate what the
        model still needs in one block. System prompt, ticket and restatement stay."""
        self.msgs = self.msgs[:3]
        lookups = [{"sql": s.payload["sql"], "result": s.payload["result"] or s.payload["error"]}
                   for s in self.t.steps if s.name == "lookup"]
        contract = self.contract.model_dump_json() if self.contract else "none accepted"
        rules = "".join(f"Policy {p} (you read it): {POLICY_TEXT[p]}\n" for p in sorted(self.read_policies))
        return (f"Your last attempt, for reference.\nLookups: {json.dumps(lookups, default=str)}\nContract: {contract}\n"
                f"SQL: {self.last_sql or 'none'}\n{rules}\n")

    def attempt(self, box, attempt, feedback) -> list:
        """-> [] when green, else the failure texts for the next revise."""
        if attempt == 1:
            self.tools_phase(box, [LOOKUP_TOOL], PROMPT["LOOKUP"])
        else:
            self.add("revise", "drydock", {"fed_back": feedback}, self.drydock_label(), self.tick(), 0)
            tools = [LOOKUP_TOOL, POLICY_TOOL] if self.policies else [LOOKUP_TOOL]
            hint = PROMPT["POLICY_HINT"].strip() if self.policies else ""
            self.tools_phase(box, tools, self.compact() + PROMPT["REVISE"].replace("{failures}", "\n".join(feedback))
                             .replace("{policy_hint}", hint))
        contract, err = self.ask_contract()
        if err:
            return self.rejected(err)
        why = next(filter(None, [refused(contract.scope_rule.where), *(refused(c.sql, READ_LEAD) for c in contract.checks)]), None)
        if why:
            return self.rejected(f"contract rejected: {why}")
        raw = self.ask_sql()
        try:
            q = one_statement(raw)  # pin 4: Approve runs the same function, so a dry run can't pass what apply refuses
        except ValueError as e:
            return self.rejected(f"SQL rejected: {e}")
        why = refused(q, RUN_LEAD)
        if why:
            return self.rejected(f"SQL rejected: {why}. One UPDATE, INSERT or DELETE only.")
        unchecked = _unchecked(contract, q)
        if unchecked:  # M1: a check that can't see the change proves nothing
            return self.rejected(f"contract rejected: checks {unchecked} name none of the changed columns "
                                 f"{sorted(set(set_columns(q)))}")
        return self.measure(box, contract, q, attempt)

    # ---- explain ----
    def explain(self):
        t, started = self.t, self.tick()
        facts = _measured_facts(t, self.ticket, t.capped)
        blob = json.dumps(facts, ensure_ascii=False, default=str)
        money = _money_set(t)
        numbers = {float(n.replace(",", "")) for n in NUMBER.findall(blob)}
        msgs = [{"role": "system", "content": PROMPT["EXPLAIN"]}, {"role": "user", "content": blob}]
        needs_usage = "usage_events" in f"{t.sql or ''} {self.ticket.get('body') or ''}"
        rejected, written = [], False
        for _ in range(2):  # one re-ask, then code writes it: an untraceable number never reaches the screen
            try:
                reply = self.ask(msgs, schema=EXPLAIN_SCHEMA, name="explanation")
            except ModelError as e:  # the measured result stands: losing the prose must not lose the run
                log.warning("explain failed, code writes it: %s", e.detail)
                rejected.append("model unavailable")
                break
            d = _json_in(reply.text)
            ok = isinstance(d, dict) and isinstance(d.get("explanation"), str) and isinstance(d.get("reply"), str)
            problem = "reply must be JSON with explanation and reply"
            if ok:
                bad = _untraced(d["explanation"], money, numbers) + _untraced(d["reply"], money, numbers)
                problem = f"numbers not in the summary: {bad}" if bad else None
                if not problem and t.capped and t.capped not in d["explanation"]:
                    problem = f"explanation must name the cap: {t.capped}"
                if not problem and needs_usage and "usage_events.amount_cents" not in d["explanation"]:
                    problem = "explanation must say usage_events.amount_cents is the rated amount"
            if not problem:
                t.explanation, t.reply = d["explanation"], d["reply"]
                written = True
                break
            rejected.append(problem)
            msgs += [{"role": "assistant", "content": reply.text},
                     {"role": "user", "content": f"Rejected: {problem}. Copy numbers from the summary exactly."}]
        if not written:
            t.explanation, t.reply = self.code_text(needs_usage)
        p = {"text": t.explanation, "reply": t.reply}
        if rejected:
            p["rejected"] = rejected
        self.model_step("explain", p, started)

    def code_text(self, needs_usage: bool) -> tuple:
        """Measured facts only, used when the model's text failed the number check twice. No money is typed here."""
        t = self.t
        measured = "; ".join(t.effect.column_summary) or "no rows changed"
        if t.verdict.green and not t.capped:
            return f"Measured on the sandbox copy: {measured}.", f"Done on a sandbox copy, waiting for approval: {measured}."
        why = "; ".join(f.detail for f in t.verdict.failures) or f"a lookup was stopped by {t.capped}"
        usage = f" {USAGE_RATED}" if needs_usage else ""
        batched = " The work is larger than the per-run cap and needs a batched job." if t.capped else ""
        return (f"Not approved: {why}. Nothing was applied.{usage}{batched}",
                f"This could not be completed safely: {why}.{batched}")


def _destroy_quietly(box):
    """destroyed_at, or None when it could not be confirmed: then nothing on screen may claim it."""
    try:
        return destroy(box)
    except Exception:  # noqa: BLE001 - must not replace the ModelUnavailable travelling through the finally (pin 8)
        log.exception("could not destroy %s", box.id)
        return None


def run_agent(ticket: dict, box, *, transport=None, budget=None) -> RunTrace:
    """Runs one ticket to a verdict, explanation and approval class.

    Fresh box per attempt (plan L625, pin 5 option (a)): `box` is used for attempt 1, then destroyed before a revise
    starts; the caller still destroys it (eval/run_eval.py and orch/runs.py both do). Each revise attempt starts its own box and destroys it, so a revise
    never measures on top of an earlier attempt. A cap ends the loop (audit-1107 B6): no revise can narrow a capped job
    into an approvable partial change. ModelUnavailable, ModelRequestError and ModelTruncated escape (pin 8).
    source_kind: a ticket with an id is a card, anything else is free text. transport is the client test hook.
    """
    run = _Run(ticket, transport, budget)
    run.read()
    feedback = []
    for attempt in range(1, MAX_REVISIONS + 2):
        run.budget.check()
        if attempt == 2:  # bugs B10: one sandbox at a time. The caller's destroy afterwards is a no-op on a gone box
            _destroy_quietly(box)
        current = box if attempt == 1 else start_run_container()
        try:
            run.t.revisions = attempt - 1
            feedback = run.attempt(current, attempt, feedback)
        finally:
            if current is not box:
                run.t.boxes.append({"id": current.id, "created_at": current.created_at,
                                    "destroyed_at": _destroy_quietly(current)})
        if not feedback or run.t.capped:
            break
    run.explain()
    run.t.approval_class, run.t.approval_reason = approval(run.t.verdict, run.t, "ticket" if ticket.get("id") else "text")
    return run.t
