# DryDock measured numbers (source of truth; `sandbox/harden.md` explains them)

| Name | Value | Where set |
|---|---|---|
| Cold `start_run_container()` | **972 ms** (Task 1 first run: 1054 ms). Docker Desktop / Windows, placeholder seed. Re-measure on the VM with the Task 2 seed. | measured by `test_start_under_5s` (`-s` prints it) |
| T = `statement_timeout` | **5s** | `sandbox/container.py` `STATEMENT_TIMEOUT`; role setting + per-connection `options=` |
| L = `temp_file_limit` | **64MB** | `sandbox/template.py` `TEMP_FILE_LIMIT`; role setting, superuser-only GUC |
| Effective backstop ceiling | **15 s** (T + 10 s) | `sandbox/runner.py` `BACKSTOP_S`. This is the hard limit: the executor can `SET statement_timeout = 0` inside a statement. |

## usage_events sizing (E11) — PROVISIONAL — laptop (Docker Desktop, Windows); re-run on the Vultr VM

E11 is a billing-ops ticket: "The meter applied the wrong seat-day rate when it totalled October; the event log itself is right. Recompute every account's October usage charge from usage_events." The stored `usage_cents_oct` is `100*randint + 37*(id mod 7) + 1`: never 0 mod 10, and it spans >10x while the true October sums are near-equal across accounts, so no single scale (e.g. "halve it") maps one onto the other (`naive_sql` in golden.json; tests `test_e11_stored_usage_is_not_twice_anything`, `test_e11_stored_usage_is_no_scaling_of_true_sums`, `test_e11_naive_halving_is_not_the_golden_result`). The golden re-sums all three metrics, not only seat_day (`metric_note` in golden.json). `usage_events(id, account_id, metric, quantity, amount_cents, occurred_at)` is a synthetic metered-usage VIEW over `generate_series(1, N)` (stores no rows); account_id, quantity and occurred_at come from `hashint4(g)` (no int4 overflow, SQLSTATE 22003 tested; no g-range maps to October). `metric` is still `g % 3`. `hashint4` is an internal function: values are tied to the `postgres:17` image pinned by digest (`postgres@sha256:3b0cda93be7c…`, `sandbox/container.py` IMAGE; compose uses the same digest). N is clamped to int4 max (2,147,483,647) so the view builds on a faster VM. Script and output file keep their Task 2 names (`seed/size_event_log.py`, `seed/event_log_size.json`).

Re-measured 2026-11-04 (fix round 2, corrected `temp_blocks`, hash-derived occurred_at) by `python -m seed.size_event_log`. Host: Windows 11, Docker Desktop 29.6.2. **Every number below is laptop-provisional: the VM re-run (and its Step 8 gate) is still pending.**

| Name | Value (laptop-provisional) |
|---|---|
| N (`usage_events` rows) | **588,126,318** = ceil(10 x T x r_max); N x b = 8.2 GB, far above 3 x L |
| T | 5000 ms (unchanged) |
| L | 64 MB (67,108,864 B) |
| r_max (fastest count rate) | 11,762.5 rows/ms (streaming form, 4e6 rows; EXPLAIN ANALYZE, TIMING OFF) |
| b (temp bytes written per row) | **14.0 B/row** (14.001, FROM form, measured; top plan node only. Round 1's 28.0 summed the plan tree and double-counted) |
| g_max (slowest non-E11 golden, wall clock incl. docker exec) | 460.2 ms; T/5 = 1000 ms, so T stays 5 s |
| cap_first_fires_at_M | **5,000,000** (4.75e6 ran 5/5 at 66.5 MB < L; 5e6 stopped by 53400) |
| Gate (Step 8, local) | E11 golden in 3 fresh run containers: stopped by SQLSTATE 53400 (temp_file_limit, code from Postgres) 3/3, in 1032-1122 ms, runner backstop not involved (`RunResult.backstop` = false); template checksum unchanged 3/3 |

Per-M stop cause (FROM form = what the view runs; `per_m` in `event_log_size.json`):

| M | FROM form | streaming form |
|---|---|---|
| 1e6 | ran 5/5, 14.0 MB temp written | ran 5/5, no temp |
| 4e6 | ran 5/5, 56.0 MB temp written (< L) | ran 5/5, no temp |
| 4.5e6 | ran 5/5, 63.0 MB temp written (< L) | ran 5/5, no temp |
| 4.75e6 | ran 5/5, 66.5 MB temp written (< L) | ran 5/5, no temp |
| 5e6 | **stopped by 53400** on the first run | ran 5/5, no temp |
| 16e6 | **stopped by 53400** on the first run | ran 5/5, no temp |

I1 closed: round 1's 112 MB at 4e6 was the double count; measured temp at 4e6 is 56.0 MB < L. Predicted fire point L / b = 67,108,864 / 14.0 = ~4.79e6 rows; measured: between 4.75e6 and 5e6.

**The stop is byte-bound.** The cap that fires is `temp_file_limit` (SQLSTATE 53400), on bytes, not the 5 s `statement_timeout`. N is sized by the time formula (10 x T x r_max); the FROM-form spill hits L at ~4.8e6 rows (measured between 4.75e6 and 5e6), about 0.8% of N, so a faster VM does not remove the stop. UI label comes from the code Postgres reports, never hard-coded: `sandbox.runner.cap_label()` maps 53400 to "disk spill cap" and 57014 to "time limit". `golden.json` E11 records `observed_stop` (read from the last gate) and `rows_label` ("usage_events: <N> synthetic rows"); both 53400 and 57014 are accepted in `expected_stop`.

The executor can raise `work_mem` with `SET` and move the stop (assumptions H4): Task 8 must reject statement-leading `SET`/`RESET`/`ALTER`, and `set_config(` anywhere, in agent SQL (plan v4.2: leading keyword only, since "anywhere" would block every `UPDATE … SET`).

## Seed and tickets (fix round 1)

- Ticket cards: `seed/tickets.json` (id, demo, requester role, subject, body, created_on = ASOF 2026-11-01). No SQL, no row ids, no answers (tested). Answers: `seed/golden.json` (sql, expected counts, class, `expected_amount_cents`). Both are written by `python -m seed.seed`; a test fails if either is stale.
- 6 demo goldens (E1, E2, E5, E6, E8, E11) + 3 held-out eval goldens on `credits` (H1-H3, `"demo": false`, never shown in the UI).
- Class A demo beat: E1 (one table, 1 row) and E2 (one table, 1 row); E1 carries the note in golden.json.
- Customer names: generic word pairs (brand words Harbor/Freight/Pixel/Rapid removed, no "Demo N" suffix) plus 6 hand-named ticket customers: Amberline Tea Co., Halvorsen Ledgerworks, Okafor & Lund Bakeries, Cedarfall Clinics, Lumenhaus Studio, Quietwater Robotics. **Names: web-check pending** (not yet searched for real companies). Emails stay `*.example.test`; requesters are roles, not people.
- One spelling: `cancelled` for subscriptions and payment plans. E8 decoy plan is `cancelled` (the customer broke the plan). E8 trap: 2 installment charges per active plan (2026-09-20, 2026-10-20, amount // installments; council ruling), `p7_protected_cents` = still owed, `p7_arr_cents` = 12 x their active subscriptions (computed: $26,448.00). Write-off unchanged: 87 invoices, $31,319.00.
- Data hygiene (round 2): no payment dated on/after ASOF (spread by invoice id); nothing issued in the last 30 days on a cancelled subscription; an open invoice on a cancelled subscription exists only 90+ days overdue (unpaid final invoice, the E8 backlog), newer ones are void.
- All sizes, prices, probabilities and date ranges in `seed/seed.py` are arbitrary synthetic choices (stated in its docstring).
- Test order: `tests/test_sandbox.py` leaves the placeholder template; `tests/test_seed.py` rebuilds the real one in its module fixture.
