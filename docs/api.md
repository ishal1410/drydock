# DryDock orchestrator API (Task 6)

Code: `orch/app.py` (routes), `orch/runs.py` (run state, caps, dry run, UI views). Tests: `tests/test_orch.py`.
Client: `web/src/api.js`; field list: `.superpowers/sdd/ui-api-contract.md`. There is no OpenAPI page; this file is
the contract.

JSON in and out, same origin through Caddy (no CORS headers). Local UI work: the Vite dev server already proxies `/api`
to `http://localhost:8000`.

Run: `.venv/Scripts/python -m uvicorn orch.app:app --port 8000` (one process; never `--workers`). It binds to 127.0.0.1;
on the VM Caddy is the only way in. `X-Forwarded-For` counts only when the peer is loopback, otherwise the peer address
is the client.

## Errors (every endpoint)

```json
{"card": "busy", "message": "Busy, try in a moment"}
```

| Status | `card` | When |
|---|---|---|
| 422 | `invalid` | bad body: empty or > 2,000-char text, both or neither of `ticket_id`/`text`, unknown ticket, extra fields, bad role or reason |
| 404 | `invalid` | unknown or malformed run id; no receipt yet |
| 429 | `rate_limited` | 6th `POST /api/runs` from one IP within 60 s (`Retry-After: 60`) |
| 429 | `rate_limited` | one IP used its runs for the UTC day (`DRYDOCK_IP_DAILY_RUNS`, `DRYDOCK_IP_DAILY_TEXT_RUNS`), or its share of the daily tokens (`DRYDOCK_IP_TOKEN_PCT`) |
| 429 | `rate_limited` | API reads over `DRYDOCK_GET_RATE_MAX` a minute from one IP (`GET /api/health` is never limited) |
| 429 | `daily_cap` | daily run cap or daily token budget reached; free text stops first (`DRYDOCK_TEXT_TOKEN_PCT`), so listed tickets keep working |
| 503 | `busy` | another run's sandbox is running; one at a time, no queue (`Retry-After: 10`) |
| 503 | `sandbox_down` | `POST /api/runs` only: the Docker daemon is unreachable. Refused before the day's count |
| 503 | `model_unavailable` | `POST /api/runs` only: no key set. A model failure later on arrives as `status: "unavailable"` on `GET /api/runs/{id}` |
| 413 | `invalid` | body over 16 KB |
| 403 | `role_not_allowed` | ops on class B |
| 409 | `not_approvable` | class C or no-op |
| 409 | `refused` | refused before anything ran (task ended, another run's change is live) |
| 409 | `apply_failed` | the apply ran and rolled back; `message` is the reason as is ("stopped by disk spill cap"). The task has ended, live is back to seed |
| 409 | `not_ready` | approve, undo or close on a run that is still running or that failed |
| 409 | `nothing_to_undo` | no applied change, or the task already ended |
| 500 | `error` | anything else; details in the server log only |

## Endpoints

### `GET /api/health`
`200 {"ok": true, "docker": true}`. `"docker": false` means the Docker daemon is unreachable, so no run can start; the site and samples still serve.

### `GET /api/tickets`
Inbox.
```json
{"tickets": [{"id": "E1", "demo": true, "requester": "Support agent", "subject": "Charged twice for INV-10101",
              "body": "Amberline Tea Co. says ...", "created_on": "2026-11-01"}],
 "hidden": [],
 "eval": {"correct": 14, "total": 18, "file": "results-2026-11-06.md"},
 "live_checksum": "ef18c0...", "seed_checksum": "ef18c0..."}
```
- `eval`: from the newest `eval/results-*.md` only; `null` when none exists or the newest is partial.
- `hidden`: always `[]` until Task 8's `startup_guard` exists.
- Both checksums are `null` when Docker is unavailable or the reaper has not warmed up yet: this route never starts a container. The UI must not read `null === null` as "= seed".

### `POST /api/runs`
Body: `{"ticket_id": "E8"}` or `{"text": "Write off every open invoice 90+ days overdue"}`.

Returns at once: `201 {"run_id": "3f9c0d0e6b0b4a3c9a3e5f1b2c7d8e9f"}`. The dry run (self-test, agent, compare, sandbox
destroyed) goes on in the background for up to `DRYDOCK_RUN_DEADLINE_S` (240 s by default). Poll `GET /api/runs/{run_id}`
until `status` is not `"running"`. The sandbox lock is held until then, so another `POST` gets `busy`.

Starting a run ends an older applied run (LC1 `next_run`), resetting live first. A model known to be down (no key),
`busy` and the caps are refused here, before any of this happens.

### `GET /api/runs/{run_id}`
```json
{
  "run_id": "3f9c...",
  "ticket": {"id": "E8", "requester": "AR lead", "subject": "Month-end write-offs", "body": "..."},
  "source_kind": "ticket",
  "status": "done",
  "task_state": "ready",
  "created_at": "2026-11-05T14:02:11.123456+00:00",
  "verdict": {"green": true, "failures": [], "warnings": []},
  "approval_class": {"class": "B", "reason": "a policy failed in an earlier attempt; 1 revision used"},
  "sql_sha256": "9a1f...",
  "chips": [{"name": "server-side COPY denied", "ok": true, "detail": "executor: 42501 ERROR: ..."}],
  "strip": [{"step": 5, "what": "sql", "role": "executor", "container_id": "drydock-run-1a2b",
             "elapsed_ms": 61, "cap_ms": 5000, "mem_peak_bytes": 48339353, "mem_cap_bytes": 536870912,
             "stopped_by": null, "code": null}],
  "destroyed_at": "2026-11-05T14:02:21.283000+00:00",
  "stopped": null,
  "ledger": [{"service": "Model provider: api.example.com", "detail": "example-model, 22,510 tokens in / 984 out, 8,980 ms"},
             {"service": "Sandbox container", "detail": "drydock-run-1a2b, executor role, created ..., destroyed ..."},
             {"service": "Live-db container", "detail": "untouched until Approve, same flags and role"}],
  "audit": [{"run_id": "3f9c...", "step": "dry_run", "container_id": "...", "created_at": "...", "destroyed_at": "..."}],
  "live_checksum": "ef18c0...",
  "explanation": "...",
  "reply": "Done: 84 invoices written off; 3 on a payment plan were left alone per policy P-7.",
  "trace": {"steps": ["..."], "revisions": 1, "contract_sha": "...", "contract_locked_at": "..."}
}
```

- `status`: `"running"`, `"done"` or `"unavailable"`. Only `"done"` has the fields shown above.
  - `"running"`: `{run_id, ticket, source_kind, status, created_at, task_state: "running"}`. Poll again.
  - `"unavailable"`: the same plus `{card, message}`, `task_state: "ended"`. `card` is `model_unavailable` (no answer from
    the model, or the run hit its time or token limit) or `error` (details in the log only). Nothing to approve.
- `task_state` is the lifecycle: `running`, `ready`, `applied` or `ended`. Class C runs are `ended` when they finish (LC1).
- `stopped`: `null`, or built only from a real SQLSTATE on a lookup or run step: `{cap_label, code, started_at,
  stopped_at, elapsed_ms, container_id, destroyed_at, rows_label, live_checksum_before, live_checksum_after}`. The
  checksums are measured before and after the dry run. `rows_label` is `null` until Task 8.
- Timing and memory fields in `strip` and `stopped` are passed through from the step payloads, `null` until Task 8.
- The `Vultr Cloud Compute VM` ledger row appears only when `DRYDOCK_VM_ID` is set (on the VM, Task 13).

`trace` is Task 5's `RunTrace` as JSON, passed through, with two API-side conversions: a step's `tokens_in`/`tokens_out`
become `tokens: {in, out}`, and any `effect` gains `money[]` (each `money_cents_by_column` entry plus `key`). Step and
payload shapes are listed in `ui-api-contract.md` and are not produced by anything until Task 5 lands.

### `POST /api/runs/{run_id}/approve`
Body: `{"role": "ops"}` or `{"role": "engineer"}`, nothing else: SQL, sha256 and approved effect come from the server's
dry run (D1a). Class A: either role. Class B: engineer. Class C and no-op: nobody.
```json
{"apply": {"role": "executor", "container_id": "drydock-live-1a2b", "sql_sha256": "9a1f...", "approved_sha256": "9a1f...",
           "effect_match": true, "rows_by_table": {"invoices": {"count_delta": 0, "touched": 84}},
           "started_at": "...", "ended_at": "...",
           "caps": {"statement_timeout": "5s", "temp_file_limit": "64MB", "memory": "512m", "network": "none"}},
 "live_checksum_before": "ef18c0...", "live_checksum_after": "7ab2d1...",
 "receipt": {"run_id": "3f9c...", "approval_class": "B", "approver_role_asserted": "engineer", "receipt_sha256": "...", "...": "..."}}
```
Any 2xx means committed. Errors: 403 `role_not_allowed`, 409 `not_approvable` / `refused` / `apply_failed`, 404.

### `POST /api/runs/{run_id}/undo`
Restores the pre-apply snapshot, then ends the task.
```json
{"checksum": "ef18c0...", "matches_before": true,
 "task_end": {"destroyed_at": "...", "reset_at": "...", "new_live_id": "drydock-live-...", "checksum_ok": true}}
```

### `POST /api/runs/{run_id}/close`
LC1 task end, live reset to seed. Body optional: `{"reason": "close"}` (default, "Close ticket") or `{"reason": "reject"}`.
```json
{"destroyed_at": "...", "reset_at": "...", "new_live_id": "drydock-live-...", "checksum_ok": true}
```
On a task that already ended, no second reset runs and the last reset's values are returned (`null` if the task ended
without one).

### `GET /api/runs/{run_id}/receipt`
The receipt JSON as a download (`Content-Disposition: attachment`). Survives the reset. 404 until the run has applied.

## Background behaviour

- A reaper thread warms the seed box and live-db at start, then every 30 s ends applied tasks idle for 10 min (LC1).
- With no key set, `POST /api/runs` returns `503 model_unavailable`. Use `?fixture` for run screens.
- Runs live in memory; past `DRYDOCK_MAX_RUNS` the oldest ended, then oldest unapplied, return 404. Receipts stay on disk.
