"""Approve / Undo / task end for the live demo DB (plan D1a, lifecycle LC1).

The orchestrator never runs model SQL itself: every DB step below is a sandbox.runner / sandbox.container call.
Task 6 wires these to POST /api/runs/{id}/approve|undo|close.

Many runs can be open at once, because each dry run measured a seed copy and live stays seed until someone applies
(audit-1107 S3). One applied change is live at a time: `held` refuses the others, a reset ends only the applied run.
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.measure import checksum, checksum_by_table, combine
from sandbox.container import RunBox, destroy, recreate_live, start_run_container
from sandbox.runner import (
    CAPS,
    ApplyResult,
    apply_sql,
    dump_data,
    ledger_row,
    now,
    restore_data,
)

# LC1 six triggers + "apply_failed" (a rolled-back, failed or errored apply also ends its task: judge G2)
TASK_END_REASONS = {"reject", "class_c", "undo", "close", "idle_10min", "next_run", "apply_failed"}
NO_RESET = {"reject", "class_c", "close"}  # on a run that never applied: live is untouched, so nothing to reset
APPROVABLE = {"A", "B"}  # D15: class C is locked; "no-op" has nothing to apply
IDLE_S = 600             # LC1: task ends 10 min after the apply if nothing else ended it
RECEIPTS_DIR = Path(os.environ.get("DRYDOCK_RECEIPTS", "receipts"))  # orchestrator volume on the VM

LOCK = threading.RLock()  # the one-sandbox lock (D4): approve, undo and every reset run under it
RUNS: dict = {}           # run_id -> {"result", "snapshot", "before", "applied_at", "ended"}
# run ids become receipt file names: server-generated only, but checked anyway (picked: 64 chars, [A-Za-z0-9_-])
RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
LEDGER: list = []
log = logging.getLogger(__name__)
_live: RunBox | None = None
_seed_checksum: str | None = None


@dataclass
class UndoResult:
    checksum: str          # live checksum after the restore
    matches_before: bool   # == the pre-apply checksum (UI shows a green "= before", not two hashes)
    task_end: "TaskEnd"


@dataclass
class TaskEnd:
    destroyed_at: datetime | None
    reset_at: datetime | None  # None: no reset ran (the run never applied), so none is claimed
    new_live_id: str | None
    checksum_ok: bool | None


def seed_checksum(refresh: bool = False) -> str:
    """Checksum of a fresh copy of the seed template: the value every reset must reproduce."""
    global _seed_checksum
    if refresh or _seed_checksum is None:
        box = start_run_container()
        try:
            _seed_checksum = checksum(box)
        finally:
            destroy(box)
    return _seed_checksum


def warm() -> None:
    """Call at process start: pays the seed-box and live-db container starts before the first Undo/reset on camera."""
    seed_checksum()
    live()


def live() -> RunBox:
    global _live
    with LOCK:
        if _live is None:
            _live = recreate_live()
        return _live


def ledger(run_id) -> list:
    return [r for r in LEDGER if r.get("run_id") == run_id]


def _refuse(reason: str) -> ApplyResult:
    t = now()
    return ApplyResult(False, None, False, reason, t, t, executed=False)


def approve(run_id, sql: str, approved_sha: str, approved_effect, approval_class: str,
            approver_role: str) -> ApplyResult:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        return _refuse(f"refused: invalid run_id {run_id!r}")
    with LOCK:
        run = RUNS.setdefault(run_id, {})
        if run.get("ended"):  # also covers a stale ok result after Undo or a reset
            return _refuse("refused: task ended, so this run can no longer apply; start a new run")
        if run.get("result"):  # idempotent: a second Approve returns the first apply, never applies again
            return run["result"]
        if approval_class not in APPROVABLE:
            return _refuse(f"refused: class {approval_class} cannot be approved")
        # every dry run measures a seed copy; the only way live leaves seed is another run's apply that is still open.
        # Checked here, not only in next_run, so a caller that skips next_run still cannot apply on newer state (A1).
        held = [rid for rid, o in RUNS.items() if rid != run_id and o.get("applied_at") and not o.get("ended")]
        if held:
            return _refuse("refused: another visitor's change is live; try again in a few minutes or start a new run")
        box = live()
        started = now()
        try:
            before = checksum_by_table(box)
            snapshot = dump_data(box)
            r = apply_sql(box, sql, approved_sha, approved_effect)
        except Exception as e:
            log.exception("apply of %s failed", run_id)  # the ledger reaches the browser: the exception text does not
            LEDGER.append(ledger_row("apply", box, sql, False, None, started, now(), run_id,
                                     f"apply error: {type(e).__name__}"))
            _end_after_error(run_id)
            raise
        if not r.executed:  # refused before execution: nothing ran, nothing to log or reset
            return r
        LEDGER.append(ledger_row("apply", box, sql, r.ok, r.code, r.started_at, r.ended_at, run_id, r.reason))
        if not r.ok:  # rolled back, failed or left prepared: end the task, live-db back to seed
            _end_after_error(run_id)  # a reset failure must not hide r (E11's "stopped by ..." beat): review-1105 A2
            return r
        try:  # receipt first: no applied state without a receipt
            _write_receipt(run_id, box, sql, r, approval_class, approver_role, before, checksum_by_table(box))
        except Exception:
            _end_after_error(run_id)  # drop the un-receipted change
            raise
        run.update(result=r, snapshot=snapshot, before=combine(before), applied_at=r.ended_at)
        return r


def _end_after_error(run_id) -> None:
    """end_task inside an except: a reset failure is logged, never raised over the original apply error."""
    try:
        end_task(run_id, "apply_failed")
    except Exception:  # noqa: BLE001
        log.exception("reset after failed apply of %s also failed", run_id)


def next_run(run_id) -> TaskEnd | None:
    """LC1 trigger next_run: Task 6 calls this when run_id starts (before its dry run). An applied change ends, so the
    new run's measurement matches live. A visitor's run that never applied stays open: live is still the seed it measured
    (audit-1107 S3), and `held` in approve stops two changes stacking (audit-1105 #1). None when nothing ended."""
    with LOCK:
        applied = [rid for rid, r in RUNS.items() if rid != run_id and r.get("applied_at") and not r.get("ended")]
        end = end_task(applied[-1], "next_run") if applied else None
        RUNS.setdefault(run_id, {})
        return end


def undo(run_id) -> UndoResult:
    """Restore the pre-apply dump, report the restored checksum, then end the task (Undo is an LC1 trigger)."""
    with LOCK:
        run = RUNS.get(run_id) or {}
        if not run.get("snapshot") or run.get("ended"):
            raise RuntimeError("nothing to undo: task closed, environment reset to seed")
        box = live()
        restore_data(box, run["snapshot"])
        restored = checksum(box)
        same = restored == run["before"]
        LEDGER.append({"run_id": run_id, "step": "undo", "container_id": box.id, "at": now(), "checksum": restored,
                       "matches_before": same})
        return UndoResult(restored, same, end_task(run_id, "undo"))


def end_task(run_id, reason: str) -> TaskEnd:
    if reason not in TASK_END_REASONS:
        raise ValueError(f"unknown task-end reason {reason!r}")
    with LOCK:
        run = RUNS.setdefault(run_id, {})
        if reason in NO_RESET and not run.get("applied_at"):  # only this run ends, no container churn, no reset claimed
            run["ended"] = True
            end = TaskEnd(None, None, _live.id if _live else None, None)
            LEDGER.append({"run_id": run_id, "step": "task_end", "reason": reason, "reset_at": None, "destroyed_at": None,
                           "new_live_id": end.new_live_id, "checksum_ok": None})
            return end
    return _reset(run_id, reason)


def demo_reset() -> TaskEnd:
    """Tests and operators only: no route calls this (audit-1107 security B5). Ends every open run."""
    return _reset(None, "demo_reset", everyone=True)


def reap_idle(now_ts: float | None = None) -> list[TaskEnd]:
    """Call periodically: ends every applied task idle for IDLE_S (trigger idle_10min)."""
    now_ts = time.time() if now_ts is None else now_ts
    with LOCK:
        due = [rid for rid, r in RUNS.items()
               if r.get("applied_at") and not r.get("ended") and now_ts - r["applied_at"].timestamp() > IDLE_S]
        return [end_task(rid, "idle_10min") for rid in due]


def _receipt_path(run_id) -> Path:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"invalid run_id {run_id!r}")
    return RECEIPTS_DIR / f"{run_id}.json"


def receipt(run_id) -> dict:
    return json.loads(_receipt_path(run_id).read_text(encoding="utf-8"))


def shutdown() -> None:
    """Remove the live-db (tests, process exit)."""
    global _live
    with LOCK:
        if _live is not None:
            destroy(_live)
            _live = None


def _reset(run_id, reason: str, everyone: bool = False) -> TaskEnd:
    global _live
    with LOCK:
        # runs end first: a docker step that raises below must not leave runs open on a gone live-db (audit-1105 #3)
        if run_id is not None:
            RUNS.setdefault(run_id, {})  # an end before any approve still blocks a later approve
        for rid, r in RUNS.items():  # a run that never applied stays valid: live is back to the seed it measured
            if everyone or rid == run_id or r.get("applied_at"):
                r["ended"] = True
                r.pop("snapshot", None)  # nothing to undo after task end
        old, _live = _live, None  # before destroy: if it raises, live() recreates, never reuses old (review-1105 H2)
        destroyed_at = destroy(old) if old else None
        _live = recreate_live()
        ok = checksum(_live) == seed_checksum()
        end = TaskEnd(destroyed_at, now(), _live.id, ok)
        LEDGER.append({"run_id": run_id, "step": "task_end", "reason": reason, "old_live_id": old and old.id,
                       "destroyed_at": destroyed_at, "reset_at": end.reset_at, "new_live_id": end.new_live_id,
                       "checksum_ok": ok})
        if not ok:  # never leave a non-seed live-db in place: the next live() starts a fresh one
            destroy(_live)
            _live = None
            raise RuntimeError(f"live-db reset: checksum != seed checksum ({end.new_live_id} destroyed)")
        return end


def _write_receipt(run_id, box, sql, r: ApplyResult, approval_class, approver_role, before: dict,
                   after: dict) -> None:
    # D1a guard 3 fields + checksums; Task 10 adds contract, counts, checks, model id, tokens.
    body = {"run_id": run_id, "sql": sql, "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            "role": "executor", "caps": CAPS,
            "container_id": box.id, "effect_match": True, "approval_class": approval_class,
            "approver_role_asserted": approver_role, "live_checksum_before": combine(before),
            "live_checksum_after": combine(after), "checksum_by_table_before": before,
            "checksum_by_table_after": after,
            "rows_by_table": r.effect.rows_by_table,
            "started_at": r.started_at.isoformat(), "ended_at": r.ended_at.isoformat()}
    body["receipt_sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
    path = _receipt_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
