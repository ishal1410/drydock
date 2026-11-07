"""python -m eval.run_eval --model <model id> --runs 3  ->  eval/results-<UTC stamp>-<model>.md (plan §2, Task 4).

Each ticket runs through llm.agent.run_agent (Task 5) on a fresh per-run container and is scored against
seed/golden.json: row counts AND money moved (expected_amount_cents). Gate: >= 5/6 demo tickets in every run, E11 cap
fired in every run, >= 3 runs. Held-out H1-H3 are scored and reported separately, never part of the gate.
A ModelUnavailable mid-eval stops it and writes results-...-INCOMPLETE.md (no score line). Reads .env.

Free tiers cannot pay for the full gate in a day, so `--tickets E8,E11 --runs 1` runs a reduced eval. It prints exactly
what ran and says GATE: NOT EVALUATED: the gate is never scaled down to fit a quota. --deadline-s applies per run.
"""
import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.contract import MAX_REVISIONS, approval
from llm.client import ModelUnavailable, load_env
from sandbox.runner import STOPPED_BY, cap_label

ROOT = Path(__file__).resolve().parent.parent
DEMO = ["E1", "E2", "E5", "E6", "E8", "E11"]
HELD = ["H1", "H2", "H3"]
GATE_MIN, GATE_RUNS = 5, 3


def load_goldens() -> dict:
    return json.loads((ROOT / "seed" / "golden.json").read_text(encoding="utf-8"))


def load_tickets() -> dict:
    return {t["id"]: t for t in json.loads((ROOT / "seed" / "tickets.json").read_text(encoding="utf-8"))}


def _code(payload):
    """SQLSTATE from a step payload: the RunResult itself or a dict holding it under "result"."""
    if isinstance(payload, dict):
        payload = payload.get("result")
    return getattr(payload, "code", None)


def money_moved(effect) -> int | None:
    """Money the change moved, in the expected_amount_cents sense: per changed table, the headline money line's
    |delta| if the SQL wrote that column, else the value of the changed rows (a status move: E5, E8). The largest
    table's figure, not the sum: E8 writes off $31,319.00 of invoices and takes the same $31,319.00 off balances,
    which is one amount seen from two tables, not two."""
    total = 0
    for m in effect.money_cents_by_column.values():
        if m.headline:
            v = abs(m.delta_cents) if m.column_changed and m.delta_cents is not None else m.before_cents
            if v is None:  # "mixed" payments direction: no meaningful sum
                return None
            total = max(total, v)
    return total


def _tokens(st) -> int:
    t = getattr(st, "tokens", None)  # Task 5 Step.tokens (plan L625) = tokens_in + tokens_out of the call
    return t if t is not None else (getattr(st, "tokens_in", 0) or 0) + (getattr(st, "tokens_out", 0) or 0)


def _names_cap(code, text) -> bool:
    """plan §2 "explanation correct": it names the cap that fired, under any of its names."""
    text = (text or "").lower()
    return bool(code) and any(n and n.lower() in text for n in (cap_label(code), STOPPED_BY.get(code), code))


def score(tid: str, golden: dict, trace) -> dict:
    steps = list(trace.steps)
    contracts = [s for s in steps if s.name == "contract"]
    first = trace.verdicts[0] if trace.verdicts else None
    s = {
        "revisions": trace.revisions,
        # ponytail: Task 5 interface assumption: an unparseable contract step carries payload {"error": ...}
        "contracts": len(contracts),
        "contracts_valid": sum(1 for c in contracts if not (isinstance(c.payload, dict) and "error" in c.payload)),
        "p7_first": (any(f.kind == "policy" and f.detail.startswith("P-7") for f in first.failures)
                     if tid == "E8" and first else None),
        # the agent's own action being stopped (plan L58): only "run" steps count, never a lookup (M6)
        "cap": next((c for c in (_code(st.payload) for st in steps if st.name == "run") if c in ("53400", "57014")),
                    None),
        "tokens": sum(_tokens(st) for st in steps),
        "ms": [st.ms for st in steps if getattr(st, "model_id", None) and st.ms is not None],
        "model_ids": sorted({st.model_id for st in steps if getattr(st, "model_id", None)}),
        "amount": None,
    }
    if "expected_stop" in golden:  # E11: cap fired, approval class C, explanation names the cap
        s["explained"] = _names_cap(s["cap"], trace.explanation)
        s["passed"] = (s["cap"] in golden["expected_stop"]
                       and approval(trace.verdict, trace, "ticket")[0] == "C" and s["explained"])
    else:  # green after <= 2 revisions, measured rows AND money == golden (green implies contract consistent)
        s["amount"] = money_moved(trace.effect)
        s["passed"] = (trace.verdict.green and trace.revisions <= MAX_REVISIONS
                       and trace.effect.rows_by_table == golden["expected"]
                       and s["amount"] == golden["expected_amount_cents"])
    return s


def gate(runs: list, cap_fired: list) -> bool:
    """runs: one {ticket id: passed} per run; cap_fired: one bool per run (plan §2: "E11 cap fires 3/3")."""
    return (len(runs) >= GATE_RUNS and all(sum(r[t] for t in DEMO) >= GATE_MIN for r in runs)
            and len(cap_fired) == len(runs) and all(cap_fired))


def is_reduced(res: dict) -> bool:
    return bool(res.get("tickets")) or len(res["runs"]) < GATE_RUNS


def gate_result(res: dict) -> bool | None:
    """True or False for a full eval, None when it never ran the gate's tickets and runs."""
    if res.get("aborted") or is_reduced(res):
        return None
    return gate([{t: r[t]["passed"] for t in r} for r in res["runs"]], [r["E11"]["cap"] is not None for r in res["runs"]])


def run(model: str, runs: int, agent=None, start=None, stop=None, tickets=None) -> dict:
    """tickets: a subset of ticket ids (reduced eval); None = all of DEMO + HELD."""
    unknown = sorted(set(tickets or []) - set(DEMO + HELD))
    if unknown:
        raise ValueError(f"unknown tickets {unknown}")
    if agent is None:
        from llm.agent import run_agent as agent  # Task 5
    if start is None:
        from sandbox.container import destroy as stop, start_run_container as start
    os.environ["DRYDOCK_MODEL"] = model
    goldens, all_tickets = load_goldens(), load_tickets()
    chosen = tickets or DEMO + HELD
    out, aborted = [], None
    for i in range(1, runs + 1):
        r = {}
        out.append(r)
        for tid in chosen:
            box = start()
            try:
                r[tid] = score(tid, goldens[tid], agent(all_tickets[tid], box))
            except ModelUnavailable as e:  # a dead endpoint is not a model failure: stop, keep what was scored
                aborted = f"model unavailable at run {i} ticket {tid}: {e.detail or e}"
            except Exception as e:  # an agent crash, a 4xx or a cut-off reply is a shown fail; the eval keeps going
                r[tid] = {"passed": False, "error": f"{type(e).__name__}: {getattr(e, 'detail', '') or e}",
                          "revisions": None, "contracts": 0, "contracts_valid": 0, "p7_first": None, "cap": None,
                          "tokens": 0, "ms": [], "model_ids": [], "amount": None}
            finally:
                stop(box)
            if aborted:
                break
        if aborted:
            break
    prompt = ROOT / "llm" / "prompt.txt"
    sha = hashlib.sha256(prompt.read_bytes()).hexdigest()[:12] if prompt.exists() else "n/a (no llm/prompt.txt)"
    return {"model": model, "prompt_sha": sha, "runs": out, "aborted": aborted, "tickets": tickets}


def _pct(xs, q):
    xs = sorted(xs)
    return xs[max(0, math.ceil(q * len(xs)) - 1)] if xs else None


def _table(runs) -> list:
    lines = ["| run | ticket | pass | revisions | contracts valid | tokens | amount cents | cap | error |",
             "|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(runs, 1):
        for t, s in r.items():
            amount = "" if s.get("amount") is None else s["amount"]
            lines.append(f"| {i} | {t} | {'PASS' if s['passed'] else 'fail'} | {s['revisions']} | "
                         f"{s['contracts_valid']}/{s['contracts']} | {s['tokens']} | {amount} | {s['cap'] or ''} | "
                         f"{s.get('error', '')} |")
    return lines


def write_results(res: dict, out_dir=ROOT / "eval") -> Path:
    runs = res["runs"]
    now = datetime.now(timezone.utc)
    served = sorted({m for r in runs for s in r.values() for m in s.get("model_ids", [])})
    head = [f"# DryDock eval {now.strftime('%Y-%m-%dT%H:%M:%SZ')}", "",
            f"- model requested: `{res['model']}`",
            f"- served model id: {', '.join(f'`{m}`' for m in served) or 'n/a (no model step)'}",
            f"- prompt sha256: `{res['prompt_sha']}`"]
    # timestamp + model: a later run or another candidate never overwrites this file (H4)
    name = f"results-{now.strftime('%Y%m%dT%H%M%SZ')}-{re.sub(r'[^A-Za-z0-9._-]', '_', res['model'])}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if res.get("aborted"):  # partial: no "correct vs hand-written goldens" line, so N5 can never read it as a score
        path = out_dir / f"{name}-INCOMPLETE.md"
        path.write_text("\n".join(head + [f"- GATE: INCOMPLETE ({res['aborted']})", ""] + _table(runs)) + "\n",
                        encoding="utf-8")
        return path
    passed = [{t: r[t]["passed"] for t in r} for r in runs]
    caps = [r["E11"]["cap"] for r in runs if "E11" in r]
    verdict = gate_result(res)
    reduced = is_reduced(res)
    ran = res.get("tickets") or DEMO + HELD
    all_s = [s for r in runs for s in r.values()]
    ms = [m for s in all_s for m in s["ms"]]
    nc = sum(s["contracts"] for s in all_s)
    p7 = [r["E8"]["p7_first"] for r in runs if "E8" in r and r["E8"]["p7_first"] is not None]
    cut = sum(str(s.get("error", "")).startswith("ModelTruncated") for s in all_s)
    latency = f"{_pct(ms, 0.5)} / {_pct(ms, 0.95)}" if ms else "n/a (no model steps)"
    if reduced:
        score_lines = [f"- **{sum(sum(p.values()) for p in passed)}/{len(ran) * len(runs)} correct vs hand-written goldens**"
                       f" (reduced: {', '.join(ran)} x{len(runs)})",
                       f"- GATE: NOT EVALUATED (needs all {len(DEMO)} demo tickets x{GATE_RUNS} runs)"]
    else:
        score_lines = [
            f"- **{sum(p[t] for p in passed for t in DEMO)}/{len(DEMO) * len(runs)} correct vs hand-written goldens** "
            "(demo tickets)",
            f"- held-out (H1-H3, not in gate): {sum(p[t] for p in passed for t in HELD)}/{len(HELD) * len(runs)}",
            f"- GATE: {'PASS' if verdict else 'FAIL'} (>= {GATE_MIN}/{len(DEMO)} each run, E11 cap every run, "
            f">= {GATE_RUNS} runs)",
            f"- per-run demo passes: {[sum(p[t] for t in DEMO) for p in passed]}"]
    e8_lines = [f"- E8 first-attempt P-7 hit: {sum(p7)}/{len(p7)}"] if "E8" in ran else []
    e11_lines = [f"- E11 cap fired: {sum(c is not None for c in caps)}/{len(caps)} "
                 f"({', '.join(f'{c} {cap_label(c)}' if c else 'none' for c in caps)})",
                 f"- E11 explanation names the cap: {sum(bool(r['E11'].get('explained')) for r in runs)}/{len(runs)}"
                 ] if "E11" in ran else []
    lines = head + [
        f"- runs: {len(runs)}",
        *score_lines,
        f"- contract-valid rate: {sum(s['contracts_valid'] for s in all_s)}/{nc}",
        *e8_lines,
        *e11_lines,
        f"- cut-off replies (finish_reason=length): {cut}",
        f"- latency per model call p50/p95 ms: {latency}",
        f"- tokens per run: {[sum(s['tokens'] for s in r.values()) for r in runs]}",
        "",
    ] + _table(runs)
    path = out_dir / f"{name}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("DRYDOCK_MODEL"), required=not os.environ.get("DRYDOCK_MODEL"))
    ap.add_argument("--runs", type=int, default=GATE_RUNS)
    ap.add_argument("--tickets", help="comma-separated subset, e.g. E8,E11: a reduced eval, the gate is not evaluated")
    ap.add_argument("--deadline-s", type=int, default=900, help="wall clock per run; pacing to a low TPM is slow")
    a = ap.parse_args(argv)
    os.environ["DRYDOCK_RUN_DEADLINE_S"] = str(a.deadline_s)
    res = run(a.model, a.runs, tickets=a.tickets.split(",") if a.tickets else None)
    path = write_results(res)
    if res["aborted"]:
        print(f"{path}\nGATE: INCOMPLETE ({res['aborted']})")
        return 1
    ok = gate_result(res)
    print(f"{path}\nGATE: {'NOT EVALUATED (reduced run)' if ok is None else 'PASS' if ok else 'FAIL'}")
    return 0 if ok is not False else 2


if __name__ == "__main__":
    load_env()
    sys.exit(main())
