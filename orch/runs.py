"""Per-run state, abuse caps and the dry-run pipeline behind the API (plan Task 6, D3, D4, D15, LC1).

The orchestrator never connects to a database: every DB step is a sandbox.* call (docker exec into a container) or an
orch/apply.py call. Approve/undo/reset/task end go through orch/apply.py only (pin 3). Single process only: RUNS,
BUSY and the rate-limit counters are module globals, like orch/apply.py's LOCK/RUNS (task-7-assumptions "single
orchestrator process").
"""
import hashlib
import importlib.util
import ipaddress
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi.encoders import jsonable_encoder

from core.contract import approval
from core.measure import checksum, one_statement
from llm.client import ModelError, ModelUnavailable, env_int, providers, restore_tokens_today, tokens_today
from orch import apply
from sandbox.container import destroy, docker, start_run_container, sweep_orphans
from sandbox.runner import STOPPED_BY, cap_label
from sandbox.selftest import self_test

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "eval"
# plan N5: the score is only ever read from here; a reduced eval's scope travels with it, so it never reads as the gate
EVAL_LINE = re.compile(r"(\d+)/(\d+) correct vs hand-written goldens\*{0,2}(?: \(reduced: ([^)]+)\))?")
TICKETS = {t["id"]: t for t in json.loads((ROOT / "seed" / "tickets.json").read_text(encoding="utf-8")) if t.get("demo")}
MAX_TEXT = 2000           # D4: 2,000-char limit
TRUSTED_PROXIES = {"127.0.0.1", "::1"}  # Caddy shares the host network (deploy/docker-compose.yml)
RATE_MAX, RATE_WINDOW_S = 5, 60  # plan Task 6 test_rate_limit: 6th POST/min/IP -> 429
# GUESSED DEFAULTS. No source fixes these numbers; set them from the account's real limits. All in .env.example.
DAILY_CAP = env_int("DRYDOCK_DAILY_RUN_CAP", 200)          # all visitors, runs per UTC day
DAILY_TOKENS = env_int("DRYDOCK_DAILY_TOKENS", 150000)     # all visitors; no new run past this. Below a 200K/day key
TEXT_TOKEN_PCT = env_int("DRYDOCK_TEXT_TOKEN_PCT", 50)     # free text stops at this share, so listed tickets still run
IP_DAILY_RUNS = env_int("DRYDOCK_IP_DAILY_RUNS", 10)       # one IP, runs per UTC day
IP_DAILY_TEXT_RUNS = env_int("DRYDOCK_IP_DAILY_TEXT_RUNS", 5)  # one IP, free-text runs per UTC day
IP_TOKEN_PCT = env_int("DRYDOCK_IP_TOKEN_PCT", 34)         # one IP, share of the daily tokens (harsh F10)
GET_RATE_MAX = env_int("DRYDOCK_GET_RATE_MAX", 240)        # one IP, API reads per minute; the UI polls about 40 a minute
MAX_RUNS = env_int("DRYDOCK_MAX_RUNS", 200)                # runs kept in memory; receipts on disk stay
REAP_S = 30               # task-7-assumptions #8: reap_idle on a 30 s loop
ALLOWED_ROLES = {"A": {"ops", "engineer"}, "B": {"engineer"}}  # D15; C and no-op: nobody

RUNS: dict = {}           # run_id -> server-side run state (sql, sha, effect never come from a request)
HITS: dict = {}           # client ip -> deque of POST timestamps
GET_HITS: dict = {}       # same, for API reads: its own bucket, so polling never uses up a visitor's POSTs
LIVE_SUM: dict = {}       # {"key": (live id, ledger_mark), "value": checksum}; every live change adds a ledger row
EVICTED = {"rows": 0}     # ledger rows dropped by _evict, so ledger_mark only ever grows
DAY = {"day": None, "count": 0, "ips": {}, "tok": {}}  # ips: client ip -> [runs, free-text runs]; tok: ip -> tokens
BUSY = threading.Lock()   # D4: one concurrent sandbox; a second request gets the busy card, no queue
_hits_lock = threading.Lock()


class ApiError(Exception):
    """Rendered as {"card", "message"} (web/src/api.js)."""

    def __init__(self, status: int, card: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.card, self.message, self.headers = status, card, message, headers


def _preflight() -> None:
    """Known-down model: the card before a container starts or another visitor's open task ends."""
    if importlib.util.find_spec("llm.agent") is None:
        raise ModelUnavailable(detail="llm/agent.py not built yet (Task 5)")
    providers()  # no key, or a key with no base URL: refuses here too


preflight = _preflight


def docker_up() -> bool:
    """Can this process reach the Docker daemon? Every sandbox and the live-db need it (e2e M1)."""
    try:
        return docker("version", "--format", "{{.Server.Version}}", timeout=5).returncode == 0
    except Exception:  # noqa: BLE001 - no docker CLI, a timeout: all mean no sandbox
        return False


def _seam(ticket: dict, box):
    """The one agent seam: Task 5 lands llm/agent.py run_agent(ticket, box) -> RunTrace and it is used from here.
    No cache, no replay (plan S1)."""
    from llm.agent import run_agent as real
    return real(ticket, box)


run_agent = _seam


def client_ip(xff: str | None, peer: str | None) -> str:
    """The limit key. Only Caddy on loopback may name a client: last X-Forwarded-For entry = the address Caddy saw (a
    client-sent XFF only adds entries before it). Any other peer is the client, whatever it sends. IPv6 keys on the /64."""
    ip = (xff.split(",")[-1].strip() if xff and peer in TRUSTED_PROXIES else peer or "unknown")[:64]
    try:
        return ipaddress.ip_network((ip, 64), strict=False).compressed if ipaddress.ip_address(ip).version == 6 else ip
    except ValueError:
        return ip


def rate_limit(ip: str, now: float | None = None, *, hits: dict = HITS, limit: int | None = None) -> None:
    now = time.monotonic() if now is None else now
    limit = RATE_MAX if limit is None else limit
    with _hits_lock:
        for k in [k for k, q in hits.items() if q and now - q[-1] > RATE_WINDOW_S]:  # ponytail: O(ips) sweep
            del hits[k]
        q = hits.setdefault(ip, deque())
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) >= limit:
            raise ApiError(429, "rate_limited", "Too many requests, try again in a minute", {"Retry-After": "60"})
        q.append(now)


def read_limit(ip: str) -> None:
    rate_limit(ip, hits=GET_HITS, limit=GET_RATE_MAX)


def _count_daily(ip: str, is_text: bool) -> None:
    today = datetime.now(timezone.utc).date()
    if DAY["day"] != today:
        DAY.update(day=today, count=0, ips={}, tok={})
    used = tokens_today()
    if DAY["count"] >= DAILY_CAP or used >= DAILY_TOKENS:
        raise ApiError(429, "daily_cap", "Daily demo limit reached, try again tomorrow")
    if is_text and used >= DAILY_TOKENS * TEXT_TOKEN_PCT // 100:
        raise ApiError(429, "daily_cap", "Free-text runs are used up for today. The listed tickets still work")
    if DAY.setdefault("tok", {}).get(ip, 0) >= DAILY_TOKENS * IP_TOKEN_PCT // 100:
        raise ApiError(429, "rate_limited", "You have used your share of today's demo, try again tomorrow")
    runs, texts = DAY["ips"].get(ip, (0, 0))
    if runs >= IP_DAILY_RUNS or (is_text and texts >= IP_DAILY_TEXT_RUNS):
        raise ApiError(429, "rate_limited", "You have used today's demo runs, try again tomorrow")
    DAY["count"] += 1
    DAY["ips"][ip] = [runs + 1, texts + is_text]
    _save_usage()


def _refund(ip: str, is_text: bool) -> None:
    """A run that cost nothing (the model never answered) does not use up the visitor's day (audit B23)."""
    DAY["count"] = max(0, DAY["count"] - 1)
    runs, texts = DAY["ips"].get(ip, (1, is_text))
    DAY["ips"][ip] = [max(0, runs - 1), max(0, texts - is_text)]


def _usage_file() -> Path:
    # harsh F6: a receipt route serves RECEIPTS_DIR/<id>.json, and "usage" is a valid id. A run id can't hold "/", so a
    # file one folder down is never a receipt path. It holds visitors' IPs.
    return apply.RECEIPTS_DIR / "private" / "usage.json"


def _save_usage() -> None:
    """A restart must not hand back a budget the provider has already counted."""
    try:
        _usage_file().parent.mkdir(parents=True, exist_ok=True)
        tmp = _usage_file().with_suffix(".tmp")
        tmp.write_text(json.dumps({"day": str(DAY["day"]), "count": DAY["count"], "ips": DAY["ips"],
                                   "tok": DAY.get("tok", {}), "tokens": tokens_today()}), encoding="utf-8")
        tmp.replace(_usage_file())
    except OSError:
        log.exception("could not save usage")


def load_usage() -> None:
    """At process start. A missing, unreadable or yesterday's file means a fresh day."""
    try:
        u = json.loads(_usage_file().read_text(encoding="utf-8"))
        today = datetime.now(timezone.utc).date()
        if u["day"] == str(today):
            DAY.update(day=today, count=int(u["count"]), ips=dict(u["ips"]), tok=dict(u.get("tok", {})))
            restore_tokens_today(int(u["tokens"]))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError, TypeError):
        log.exception("usage file unreadable, starting from zero")


def measure_live() -> str:
    return checksum(apply.live())


def _cached_seed() -> str | None:
    """Cached only: a public GET never starts a container (audit S5). The reaper fills it, and retries until it can."""
    return apply._seed_checksum


seed_sum = _cached_seed


def ledger_mark() -> int:
    """Rows ever added. The length alone would not move when one row goes in as an evicted run's row comes out."""
    return len(apply.LEDGER) + EVICTED["rows"]


def live_sum() -> str | None:
    """Live checksum for a public GET: re-measured only after the live-db moved, so a page load costs no docker exec.
    Never starts a live-db and never queues behind a reset: GETs would fill the thread pool (audit S5)."""
    if apply._live is None:
        return None
    if not apply.LOCK.acquire(blocking=False):
        return LIVE_SUM.get("value")  # ponytail: last known during a reset, seconds stale at most
    try:
        key = (apply.live().id, ledger_mark())
        if LIVE_SUM.get("key") != key:
            LIVE_SUM.update(key=key, value=measure_live())
        return LIVE_SUM["value"]
    except Exception:  # noqa: BLE001 - Docker down: the inbox still renders
        log.exception("live checksum unavailable")
        return None
    finally:
        apply.LOCK.release()


def _fresh_live() -> str | None:
    try:
        return measure_live()
    except Exception:  # noqa: BLE001
        log.exception("live checksum unavailable")
        return None


def eval_score() -> dict | None:
    """Newest eval/results-*.md only; a partial newest file has no score line, so nothing is shown (N5)."""
    files = sorted(EVAL_DIR.glob("results-*.md"))
    m = EVAL_LINE.search(files[-1].read_text(encoding="utf-8")) if files else None
    return {"correct": int(m[1]), "total": int(m[2]), "scope": m[3], "file": files[-1].name} if m else None


def inbox() -> dict:
    try:
        seed = seed_sum()
    except Exception:  # noqa: BLE001
        log.exception("seed checksum unavailable")
        seed = None
    # hidden: Task 8 startup_guard hides E11 when its margin is too thin; not built yet
    return {"tickets": list(TICKETS.values()), "hidden": [], "eval": eval_score(), "live_checksum": live_sum(),
            "seed_checksum": seed}


def create_run(ticket_id: str | None, text: str | None, ip: str = "unknown") -> str:
    """Refusals answer here; the dry run itself takes minutes (a slow model, pacing), so it runs in a thread and the
    browser polls GET /api/runs/{id}. A reload or a dropped connection no longer loses the result."""
    try:
        preflight()
    except ModelError as e:
        log.warning("model unavailable: %s", e.detail)
        raise ApiError(503, "model_unavailable", e.public) from None
    if not docker_up():  # before the day's count: a run that can't start costs the visitor nothing
        log.warning("docker unreachable: refusing a run")
        raise ApiError(503, "sandbox_down", "The sandbox host is down, so no run can start. Nothing ran.")
    if not BUSY.acquire(blocking=False):
        raise ApiError(503, "busy", "Busy, try in a moment", {"Retry-After": "10"})
    try:
        is_text = ticket_id is None
        _count_daily(ip, is_text)
        run_id = uuid.uuid4().hex
        ticket = TICKETS[ticket_id] if ticket_id else {"id": None, "requester": "You", "subject": "Free-text ticket",
                                                       "body": text}
        RUNS[run_id] = {"run_id": run_id, "ticket": ticket, "source_kind": "text" if is_text else "ticket",
                        "created_at": datetime.now(timezone.utc), "status": "running", "ip": ip}
        _evict()
        spawn(lambda: _work(run_id))
        return run_id
    except BaseException:
        BUSY.release()
        raise


def _spawn(fn) -> None:
    threading.Thread(target=fn, daemon=True, name="dry-run").start()  # killed mid-run: the reaper sweeps the orphan box


spawn = _spawn


def _work(run_id: str) -> None:
    """Owns BUSY from here on. Every outcome ends in a status the poll can read; details only in the log."""
    run = RUNS[run_id]
    spent = tokens_today()
    try:
        _dry_run(run)
        run["status"] = "done"  # last: a poll that reads done gets the whole view, class C already ended
    except ModelError as e:  # pin 2: the card shows e.public; e.detail goes to the log only
        log.warning("model unavailable (run %s): %s", run_id, e.detail)
        run.update(status="unavailable", card="model_unavailable", message=e.public)
        if tokens_today() == spent:
            _refund(run["ip"], run["source_kind"] == "text")
    except Exception:  # noqa: BLE001 - nothing above the thread would catch it
        log.exception("run %s failed", run_id)
        run.update(status="unavailable", card="error", message="Something went wrong, try again")
        if tokens_today() == spent:  # e2e M1: a sandbox failure before any model call costs the visitor nothing
            _refund(run["ip"], run["source_kind"] == "text")
    finally:
        tok = DAY.setdefault("tok", {})
        tok[run["ip"]] = tok.get(run["ip"], 0) + max(0, tokens_today() - spent)
        _save_usage()
        BUSY.release()


def _dry_run(run: dict) -> None:
    run_id, ticket, source_kind = run["run_id"], run["ticket"], run["source_kind"]
    apply.next_run(run_id)  # pin 1: ends older open runs before this run's dry run
    live_before = _fresh_live()
    box = start_run_container()
    try:
        chips = self_test(box)  # every run (D9.2 supporting chips)
        trace = run_agent(ticket, box)
    finally:
        destroyed_at = _destroy(box)
    sql = getattr(trace, "sql", None)  # final SQL of the green attempt (Task 5 interface, flagged)
    cls, reason = approval(trace.verdict, trace, source_kind)  # D15, computed here, never sent by the client
    if cls in ALLOWED_ROLES:
        try:  # judge precondition 3: SQL that apply's one_statement would refuse never reaches Approve
            if not sql:
                raise ValueError("no SQL in the trace")
            one_statement(sql)
        except ValueError as e:
            cls, reason = "C", f"apply would refuse this SQL: {e}"
    run.update(trace=trace, chips=chips, approval_class=cls, approval_reason=reason, sql=sql,
               sha=hashlib.sha256(sql.encode("utf-8")).hexdigest() if sql else None,
               box_id=box.id, box_created_at=box.created_at, destroyed_at=destroyed_at,
               live_before=live_before, live_after=_fresh_live())
    for b in _boxes(run):
        apply.LEDGER.append({"run_id": run_id, "step": "dry_run", "container_id": b["id"],
                             "created_at": b["created_at"], "destroyed_at": b["destroyed_at"]})
    if cls == "C":  # LC1 trigger: final class C ends the task
        _end(run_id, "class_c")


def _evict() -> None:
    """Bound memory. Oldest ended runs go first, then oldest unapplied; an applied run is the live state, so it stays."""
    extra = len(RUNS) - MAX_RUNS
    if extra <= 0:
        return
    states = {r: task_state(r) for r in list(RUNS)}
    gone = sorted((r for r, st in states.items() if st not in ("applied", "running")),
                  key=lambda r: states[r] != "ended")[:extra]
    with apply.LOCK:
        for r in gone:
            del RUNS[r]
            apply.RUNS.pop(r, None)
        kept = [row for row in apply.LEDGER if row.get("run_id") not in gone]
        EVICTED["rows"] += len(apply.LEDGER) - len(kept)
        apply.LEDGER[:] = kept


def get(run_id: str) -> dict:
    if not isinstance(run_id, str) or not apply.RUN_ID_RE.fullmatch(run_id) or run_id not in RUNS:
        raise ApiError(404, "invalid", "No such run")
    return RUNS[run_id]


def _finished(run_id: str) -> dict:
    run = get(run_id)
    if run["status"] != "done":
        raise ApiError(409, "not_ready", run.get("message") or "This run has not finished yet")
    return run


def task_state(run_id: str) -> str:
    status = RUNS.get(run_id, {}).get("status")
    if status == "running":
        return "running"
    if status == "unavailable":
        return "ended"
    st = apply.RUNS.get(run_id, {})
    return "ended" if st.get("ended") else "applied" if st.get("applied_at") else "ready"


def _with_money(effect) -> None:
    """UI shape: money[] = each MoneyDelta plus its "table.column" key (money_cents_by_column is kept for the eval)."""
    if isinstance(effect, dict) and "money_cents_by_column" in effect:
        effect["money"] = [{**m, "key": k} for k, m in effect["money_cents_by_column"].items()]


def _trace_view(trace) -> dict:
    t = jsonable_encoder(trace)
    _with_money(t.get("effect"))
    for s in t.get("steps", []):
        # pin 6 keeps Step.tokens an int for the eval; the UI wants {in, out}, which needs tokens_in/tokens_out
        s["tokens"] = {"in": s["tokens_in"], "out": s["tokens_out"]} if "tokens_in" in s else None
        if isinstance(s.get("payload"), dict):
            _with_money(s["payload"].get("effect"))
    return t


def _stat(step: dict, key: str):
    """A statement field from a step payload: flat, or under "result" (pin 6: RunResult or {"result": RunResult})."""
    p = step.get("payload")
    if not isinstance(p, dict):
        return None
    if p.get(key) is not None:
        return p[key]
    return p["result"].get(key) if isinstance(p.get("result"), dict) else None


def _boxes(run: dict) -> list:
    """Every sandbox of this run: the caller's, then each revise's own (B9)."""
    return [{"id": run["box_id"], "created_at": run["box_created_at"], "destroyed_at": run["destroyed_at"]},
            *getattr(run["trace"], "boxes", [])]


def _strip(run: dict, steps: list) -> list:
    """One row per model-written statement. Memory stays null until the runner measures it (Task 8)."""
    return [{"step": s.get("n"), "what": {"lookup": "lookup", "run": "sql"}[s["name"]], "role": "executor",
             "container_id": _stat(s, "container_id") or run["box_id"], "code": _stat(s, "code"),
             "stopped_by": _stat(s, "stopped_by") or STOPPED_BY.get(_stat(s, "code")),
             **{k: _stat(s, k) for k in ("elapsed_ms", "cap_ms", "mem_peak_bytes", "mem_cap_bytes")}}
            for s in steps if s.get("name") in ("lookup", "run")]


def _stopped(run: dict, steps: list) -> dict | None:
    """The red card, only when the run really ended that way: red, or green but locked because a cap fired (B3)."""
    if run["trace"].verdict.green and not getattr(run["trace"], "capped", None):
        return None
    for s in reversed(steps):  # the last stop is the one that ended the run
        code = _stat(s, "code")
        if s.get("name") in ("lookup", "run") and cap_label(code):  # a real SQLSTATE only, never a guess
            box_id = _stat(s, "container_id") or run["box_id"]
            return {"cap_label": cap_label(code), "code": code, "started_at": s.get("started_at"),
                    "stopped_at": _stat(s, "stopped_at"), "elapsed_ms": _stat(s, "elapsed_ms"),
                    "container_id": box_id,
                    "destroyed_at": next((b["destroyed_at"] for b in _boxes(run) if b["id"] == box_id), None),
                    "rows_label": None,  # Task 8 knows the usage_events row count
                    "live_checksum_before": run["live_before"], "live_checksum_after": run["live_after"]}
    return None


def _ledger(run: dict, steps: list) -> list:
    """D10 rows from what was really used. Task 10 owns the full wording and the receipt-store row."""
    rows = []
    vm = os.environ.get("DRYDOCK_VM_ID")  # set on the VM (Task 13); a laptop run never claims a Vultr VM
    if vm:
        rows.append({"service": "Vultr Cloud Compute VM", "detail": f"{vm}: orchestrator, run state, receipts volume"})
    model = [s for s in steps if s.get("model_id")]
    if model:
        tokens = [s["tokens"] for s in model if s.get("tokens")]
        hosts = sorted({str(s.get("service") or "").split(",")[0].strip() or "unknown" for s in model})
        rows.append({"service": f"Model provider: {', '.join(hosts)}",
                     "detail": f"{', '.join(sorted({s['model_id'] for s in model}))}, "
                               f"{sum(t['in'] for t in tokens):,} tokens in / {sum(t['out'] for t in tokens):,} out, "
                               f"{sum(s.get('ms') or 0 for s in model):,} ms"})
    rows.append({"service": "Sandbox container", "detail": "; ".join(
        f"{b['id']}, executor role, created {b['created_at'].isoformat()}, "
        + (f"destroyed {b['destroyed_at'].isoformat()}" if b["destroyed_at"] else "destroy not confirmed")
        for b in _boxes(run))})
    applied = [r for r in apply.ledger(run["run_id"]) if r.get("step") == "apply"]
    rows.append({"service": "Live-db container",
                 "detail": f"{applied[-1]['container_id']}, executor role, same flags" if applied
                 else "untouched until Approve, same flags and role"})
    return rows


def view(run_id: str) -> dict:
    run = get(run_id)
    if run["status"] != "done":  # running: poll again. unavailable: card and message; no trace, nothing to approve
        return {k: run[k] for k in ("run_id", "ticket", "source_kind", "status", "created_at", "card", "message")
                if k in run} | {"task_state": task_state(run_id)}
    trace = _trace_view(run["trace"])
    steps = trace.get("steps", [])
    return {"run_id": run_id, "ticket": run["ticket"], "source_kind": run["source_kind"],
            "status": "done",
            "task_state": task_state(run_id), "created_at": run["created_at"], "trace": trace,
            "verdict": run["trace"].verdict,
            "approval_class": {"class": run["approval_class"], "reason": run["approval_reason"]},
            "sql_sha256": run["sha"], "chips": run["chips"], "strip": _strip(run, steps),
            "destroyed_at": run["destroyed_at"], "stopped": _stopped(run, steps), "ledger": _ledger(run, steps),
            "audit": apply.ledger(run_id), "live_checksum": run["live_after"],
            "explanation": getattr(run["trace"], "explanation", None), "reply": getattr(run["trace"], "reply", None)}


def approve(run_id: str, role: str) -> dict:
    run = _finished(run_id)
    cls = run["approval_class"]
    if cls not in ALLOWED_ROLES:
        raise ApiError(409, "not_approvable", f"class {cls} cannot be approved: {run['approval_reason']}")
    if role not in ALLOWED_ROLES[cls]:
        raise ApiError(403, "role_not_allowed", f"class {cls} needs an engineer to approve")
    # D1a guards: sql, sha and effect are the dry run's, from server state
    r = apply.approve(run_id, run["sql"], run["sha"], run["trace"].effect, cls, role)
    if not r.executed:
        raise ApiError(409, "refused", r.reason)
    if not r.ok:  # pin 3: reason as is. Non-2xx because the UI renders every 2xx as "applied"
        raise ApiError(409, "apply_failed", r.reason)
    rec = apply.receipt(run_id)
    flags = rec["caps"]["container_flags"]
    return {"apply": {"role": rec["role"], "container_id": rec["container_id"], "sql_sha256": rec["sql_sha256"],
                      "approved_sha256": run["sha"], "effect_match": rec["effect_match"],
                      "rows_by_table": rec["rows_by_table"], "started_at": rec["started_at"],
                      "ended_at": rec["ended_at"],
                      "caps": {"statement_timeout": rec["caps"]["statement_timeout"],
                               "temp_file_limit": rec["caps"]["temp_file_limit"],
                               "memory": flags[flags.index("--memory") + 1],
                               "network": flags[flags.index("--network") + 1]}},
            "live_checksum_before": rec["live_checksum_before"], "live_checksum_after": rec["live_checksum_after"],
            "receipt": rec}


def _destroy(box):
    """destroyed_at, or None when not confirmed. Never raises: must not replace the card travelling through finally (B22)."""
    try:
        return destroy(box)
    except Exception:  # noqa: BLE001
        log.exception("could not destroy %s", box.id)
        return None


def _end(run_id: str, reason: str):
    try:
        return apply.end_task(run_id, reason)
    except Exception:  # noqa: BLE001 - a reset failure is logged; the next live() starts a fresh live-db
        log.exception("task end (%s) for %s failed", reason, run_id)
        return None


def end(run_id: str, reason: str):
    """close / reject: an LC1 task end. Already ended -> the last reset's facts, no second reset (no free Docker churn)."""
    _finished(run_id)
    if task_state(run_id) == "ended":
        last = next((r for r in reversed(apply.ledger(run_id)) if r.get("step") == "task_end"), {})
        return {k: last.get(k) for k in ("destroyed_at", "reset_at", "new_live_id", "checksum_ok")}
    return apply.end_task(run_id, reason)


def undo(run_id: str):
    _finished(run_id)
    try:
        return apply.undo(run_id)
    except RuntimeError as e:
        if str(e).startswith("nothing to undo"):
            raise ApiError(409, "nothing_to_undo", str(e)) from None
        raise


def reaper(stop: threading.Event, interval: float = REAP_S) -> None:
    """Background loop: remove what a killed process left (B11), warm the seed box + live-db once, then end idle applied
    tasks (LC1 idle_10min)."""
    try:
        gone = sweep_orphans()
        if gone:
            log.warning("removed orphan sandboxes: %s", gone)
    except Exception:  # noqa: BLE001
        log.exception("orphan sweep failed")
    try:
        apply.warm()
    except Exception:  # noqa: BLE001
        log.exception("warm-up failed; the first reset pays the container start")
    while not stop.wait(interval):
        if apply._seed_checksum is None:  # Docker was not ready at start: the inbox stays null until this works
            try:
                apply.warm()
            except Exception:  # noqa: BLE001
                log.exception("warm-up retry failed")
        try:
            apply.reap_idle()
        except Exception:  # noqa: BLE001
            log.exception("reap_idle failed")
