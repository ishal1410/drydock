"""Size the usage_events view (E11) so it is stopped by a real cap. `python -m seed.size_event_log`.

(File and JSON keep their Task 2 names, size_event_log / event_log_size.json; the view is usage_events.)

Step 6 measure (g_max, r_max, b) -> Step 7 N = ceil(10 * T * r_max) -> Step 8 gate (E11 stopped 3/3,
template checksum unchanged 3/3). Writes seed/event_log_size.json, which seed.build_sql() reads.
Run on the Vultr VM for the real numbers; a laptop run is provisional.
"""
import json
import math
import platform
import re
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

from sandbox.container import IMAGE, STATEMENT_TIMEOUT, TEMPLATE_VOLUME, destroy, docker, start_run_container
from sandbox.runner import run_sql
from sandbox.template import TEMP_FILE_LIMIT, refresh_template
from seed.seed import DEFAULT_N, HERE, build_sql, goldens

T_MS = int(STATEMENT_TIMEOUT.rstrip("s")) * 1000
L_BYTES = int(re.match(r"\d+", TEMP_FILE_LIMIT).group()) * 1024 * 1024
CAP_CODES = ("57014", "53400")  # query_canceled (statement_timeout), configuration_limit_exceeded (temp_file_limit)
BLOCK = 8192


INT4_MAX = 2**31 - 1


def size_n(r_max: float, b: float) -> int:
    """Step 7: N = ceil(10 x T x r_max), at least 3 x L / b, at most int4 max: past that generate_series(1, N)
    resolves to the bigint overload and hashint4(g) has none (assumptions-2 N2). The stop is byte-bound at
    ~L / b rows, so the clamp never removes it."""
    n = math.ceil(10 * T_MS * r_max)
    if b > 0:
        n = max(n, math.ceil(3 * L_BYTES / b))
    return min(n, INT4_MAX)


def seed_template(n: int) -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "seed.sql"
        p.write_text(build_sql(n), encoding="utf-8")
        refresh_template(p)


def template_checksum() -> str:
    cmd = "cd /d && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum"
    p = docker("run", "--rm", "--network", "none", "--entrypoint", "bash", "-v", f"{TEMPLATE_VOLUME}:/d:ro",
               IMAGE, "-c", cmd, timeout=300)
    if p.returncode != 0:
        raise RuntimeError(p.stderr)
    return p.stdout.split()[0]


def temp_blocks(plan: dict) -> int:
    # Top node only: per the EXPLAIN docs (BUFFERS), "the number of blocks shown for an upper-level node includes
    # those used by all its child nodes". Summing the tree double-counted (round 1: b = 28 B/row; true ~14).
    return plan.get("Temp Written Blocks", 0)


def measure_golden_headroom() -> float:
    """Step 6.1: slowest wall-clock statement over every non-E11 golden (8 today) x 3 fresh boxes (includes ~0.15 s exec overhead: conservative)."""
    g_max = 0.0
    for eid, g in goldens().items():
        if eid == "E11":
            continue
        for _ in range(3):
            box = start_run_container()
            try:
                t0 = time.perf_counter()
                r = run_sql(box, g["sql"])
                ms = (time.perf_counter() - t0) * 1000
                assert r.ok, (eid, r.error)
                g_max = max(g_max, ms)
            finally:
                destroy(box)
    return g_max


def measure_scan() -> tuple[float, float, list]:
    """Steps 6.2-6.3: fastest rows/ms and temp bytes per row, from EXPLAIN (ANALYZE, TIMING OFF) of a plain count.

    Two forms: generate_series in FROM (what usage_events does; it materializes and spills, so past L the
    temp cap itself stops it, 53400) and a streaming form (SRF in the select list; no spill) so r_max is still
    measured at 4e6/16e6 rows. r_max = fastest of all runs. Returns one record per (M, form): best rate,
    max temp bytes, and the stop cause (Postgres SQLSTATE) if the cap fired.
    """
    r_max, b, per_m = 0.0, 0.0, []
    box = start_run_container()
    try:
        # 4.5e6-5e6 bracket where L / b (~4.79e6 at 14 B/row) says the cap starts firing
        for m in (1_000_000, 4_000_000, 4_500_000, 4_750_000, 5_000_000, 16_000_000):
            for form, q in (("from", f"SELECT count(*) FROM generate_series(1, {m})"),
                            ("stream", f"SELECT count(*) FROM (SELECT generate_series(1, {m})) s")):
                rec = {"M": m, "form": form, "rows_per_ms": 0.0, "temp_bytes": 0, "stopped_by": None, "runs": 0}
                for _ in range(5):
                    r = run_sql(box, f"EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) {q}")
                    if not r.ok and not r.backstop and r.code in CAP_CODES:
                        rec["stopped_by"] = r.code
                        break
                    assert r.ok, r.error
                    doc = json.loads(r.stdout)[0]
                    rec["runs"] += 1
                    rec["rows_per_ms"] = max(rec["rows_per_ms"], m / doc["Execution Time"])
                    rec["temp_bytes"] = max(rec["temp_bytes"], temp_blocks(doc["Plan"]) * BLOCK)
                r_max = max(r_max, rec["rows_per_ms"])
                b = max(b, rec["temp_bytes"] / m)
                per_m.append(rec)
    finally:
        destroy(box)
    return r_max, b, per_m


def gate() -> list[dict]:
    """Step 8: E11 in 3 fresh run containers: stopped by a real cap (code from Postgres), template unchanged."""
    sql, before, rows = goldens()["E11"]["sql"], template_checksum(), []
    for _ in range(3):
        box = start_run_container()
        try:
            t0 = time.perf_counter()
            r = run_sql(box, sql)
            ms = round((time.perf_counter() - t0) * 1000)
        finally:
            destroy(box)
        rows.append({"stopped": (not r.ok) and (not r.backstop) and r.code in CAP_CODES,
                     "code": r.code, "backstop": r.backstop, "ms": ms,
                     "template_unchanged": template_checksum() == before})
    return rows


def main() -> int:
    seed_template(DEFAULT_N)
    g_max = measure_golden_headroom()
    if g_max > T_MS / 5:
        print(f"FAIL: g_max {g_max:.0f} ms > T/5; raise T to >= {5 * g_max:.0f} ms")
        return 1
    r_max, b, per_m = measure_scan()
    n = size_n(r_max, b)
    out = {"N": n, "T_ms": T_MS, "L_bytes": L_BYTES, "r_max_rows_per_ms": r_max, "b_bytes_per_row": b,
           "g_max_ms": g_max, "per_m": per_m,
           "cap_first_fires_at_M": min((r["M"] for r in per_m if r["stopped_by"]), default=None), "date": str(date.today()), "host": f"{platform.system()} {platform.release()}"}
    (HERE / "event_log_size.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    seed_template(n)
    rows = gate()
    out["gate"] = rows
    (HERE / "event_log_size.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))
    return 0 if all(r["stopped"] and r["template_unchanged"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
