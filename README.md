# DryDock

An AI agent for billing and support ops data-fix tickets, such as "write off every open invoice 90+ days overdue". It promises what the change will do, runs its SQL on a throwaway copy of the database, and code measures the result against the promise. A person approves before anything touches the live copy, and can undo it.

Built for the Vultr Agent Rush hackathon, Challenge 2 "Powering the Future of Work". All data is synthetic. [Status](#status) lists what has been tested and what has not run yet.

## How the agent executes real tasks

1. **Ticket.** A person picks one of the listed tickets or types their own (2,000 characters at most).
2. **Lookups.** The agent runs read-only lookups. It gets back row counts and column sums, by design not row values.
3. **Contract.** It writes a change contract before any change: tables, row range, scope rule, checks.
4. **SQL in a sandbox.** One statement runs in a fresh Postgres container copied from a seed template, as the `executor` role.
5. **Measure.** Code, not the model, diffs the copy and compares it with the contract and the billing policies (`core/`, `policy/policies.yaml`). The model writes the contract and its own checks; the policies are the company's, and the model is not shown them up front. A change that adds a policy violation is red.
6. **Revise.** On a red result the agent gets the failure back and tries again in a new container, up to `MAX_REVISIONS`. A hit on a resource cap ends the run.
7. **Approval.** The server computes the class. A: ops or engineer. B: engineer only. C: nobody. "No-op": nothing to apply.
8. **Apply.** Approve runs the same SQL, pinned by sha256, on the live-db container with the same limits. It commits only if the effect matches the dry run.
9. **Undo or reset.** Undo restores the snapshot. Each task end recreates live from the seed. A JSON receipt stays on disk.

There is no login. The approval class sets the role the button approves as, so in the demo anyone can press it. The receipt records that role as `approver_role_asserted`; SSO would fill it for real.

## Vultr services used

| Service | Use |
|---|---|
| Vultr Cloud Compute VM (Ubuntu 24.04) | The one backend: API, agent loop, Docker control, every sandbox and the live-db, run state, receipts, TLS. Sized from the caps, not measured: up to 3 Postgres containers at 512 MB each, plus the app |
| Vultr Serverless Inference | The model behind live runs: `glm-5.3-flash`, `reasoning_effort` medium (OpenAI-compatible API) |

The live demo is at https://64-176-195-195.sslip.io, served from the Vultr VM. Its live runs call Vultr Serverless Inference; the landing header shows the model id the server is configured with (`/api/health`). For Challenge 2 the event page calls Serverless Inference optional, so other providers also work:

> "A Vultr VM backend is mandatory for every project across both challenges. For LLM calls, Vultr Serverless Inference is strictly mandatory for Challenge 1 (Blast Radius Zero), but if you are participating in Challenge 2 (Powering the Future of Work), Serverless Inference is optional and you can use other LLM providers alongside Vultr." (organizer, Discord, 2026-11-07)

Model calls go to any OpenAI-compatible endpoint set in `.env`. The screen names the host that really answered, and names the VM only when `DRYDOCK_VM_ID` is set.

## Architecture

```
Browser ── HTTPS ──> Caddy :443 (TLS, 16 KB body limit)
                       │ loopback
                       v
             orch 127.0.0.1:8000  FastAPI, one process: /api + built web/dist
              │            │
   OpenAI-compatible    docker CLI over /var/run/docker.sock
   model endpoint          │
                           v
              drydock-run-* (one per attempt)   drydock-live-*
              Postgres 17, --network none, 512 MB, no capabilities, read-only root,
              socket-only peer auth, statement_timeout 5s, temp_file_limit 64MB
```

- `orch/`: API, run state, caps, approve and undo. `llm/`: model client and agent loop. `sandbox/`: containers and SQL runner. `core/`: contract and measurement. `web/`: React UI. `eval/`: scoring against hand-written goldens.
- Flags and roles are explained in `sandbox/harden.md`; the API contract is `docs/api.md`.
- `POST /api/runs` returns a run id at once and the browser polls, because a run can take minutes.
- **Trust boundary:** the orchestrator mounts the Docker socket, which is root on the VM, and it also holds the model key. Code execution there means control of the VM and the key. No path to that is known, but it is accepted, not removed. The VM holds nothing else, and the key should be rotated after judging. Sandbox containers never get the socket or the key.
- **Limits, one shared demo:** one sandbox at a time (a second visitor sees "Busy"), run state in memory, one seeded database, no way yet to attach a real one.

## Setup

Needs Docker with compose, Python 3.12, Node 20+.

```bash
cp .env.example .env        # LLM_API_KEY, LLM_BASE_URL, DRYDOCK_MODEL. A key with no base URL or model refuses to start
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv/Scripts/
.venv/bin/python -m pytest -q                                       # tests that need Docker skip themselves
.venv/bin/python -c "from seed.size_event_log import seed_template; seed_template(None)"   # seed template, needs Docker
(cd web && npm ci && npm run build)                                 # the site; without it / is a JSON 404
.venv/bin/python -m uvicorn orch.app:app --port 8000
```

Then open `http://localhost:8000/?app` for the live app, or `http://localhost:8000/?fixture` for the sample runs, which need no key or Docker. `http://localhost:8000/` is the landing page; its button opens the live app only in a build made with `VITE_DRYDOCK_LIVE=1` (deploy does this). `GET /api/health` reports `"docker": false` when no run can start. For frontend work, `cd web && npm run dev` serves the same `?app` and `?fixture` pages on Vite's port.

Deploy to a fresh Ubuntu 24.04 VM, from the laptop, after committing:

```bash
deploy/deploy.sh root@<vm-ip>   # builds web/, copies it with the tree and .env, installs Docker, starts, runs verify
deploy/verify.sh root@<vm-ip>   # outside checks plus one sandbox self-test; spends no model tokens
```

The site is at `https://<ip-with-dashes>.sslip.io`. Tune the per-IP and daily limits in `.env` (`.env.example` lists them). Those defaults are guesses.

## Status

Last updated 2026-11-07. Passing tests below use fakes for Docker and the model unless stated.

**Tested here**
- The non-Docker test suite, `web/src/run.check.mjs` and `vite build` pass.
- `deploy/deploy.sh ran on` the Vultr VM (2026-11-07, several redeploys), and `deploy/verify.sh ran on` it after each: all 13 checks passed (TLS on the sslip.io name, Docker, tickets, checksums, sandbox self-test, port 8000 closed, metadata rule). Live at https://64-176-195-195.sslip.io.
- Live runs through the site API on Vultr Serverless Inference (`glm-5.3-flash`), 2026-11-07, one run each unless stated:
  - E2: green, class A, 0 revisions; 1 account row, as in the golden.
  - E6: green, class B, 1 revision; 68 subscriptions, the golden's SQL.
  - E11: class C, stopped by the disk spill cap on the first attempt, nothing applied, as the golden expects.
  - E1: green, class B (golden: A), 1 revision; 1 refund row for the right charge, but the method is typed in rather than copied from the charge.
  - E5: red, class C. The model closed the 2 duplicate accounts but left their subscriptions active; P-6 caught it (violations 0 → 2). The golden also cancels them.
  - E8, after the revise fixes: 6 runs on the final prompt, 4 green (3 class B as in the golden, 1 class C after 2 revisions), 2 red. Every run kept the payment-plan exclusion (87 invoices, 75 accounts); the red ones failed on account balances. Before the fixes, the one live E8 run ended red.
- The Docker test suite ran once on the VM, in a throwaway Docker-in-Docker daemon so the live template was untouched: 520 passed, 13 failed, 1 skipped (no node). 2 of the 13 pass with a dummy model host set; the other 11 are tests written before the E8 golden began updating account balances (they expect invoices only, `UPDATE 87`, or P-5 at 0), plus one apply-error message test. They are not fixed yet.
- The built page renders `/`, `?app` and `?fixture` in headless Chromium with no CSP errors.

**Known limits**
- Run records live in the server's memory. A redeploy or restart loses them; receipts on disk and the per-IP usage file survive.
- E11's cap numbers are laptop numbers (`docs/BRIEF.md`, marked provisional). `python -m seed.size_event_log` on the VM has not been run.
- The landing page and `?fixture` show scripted examples, labelled as such. No model produced them.
- The pricing shown is a hypothesis, untested with buyers. There are no customers.

## Built with Claude Code

Written with [Claude Code](https://claude.com/claude-code), Anthropic's AI coding tool, during the event (first commit Nov 3, 2026). Most of the code was written by it; a human directed the work and makes the release decisions.

## Licences

- This repository: MIT (`LICENSE`).
- **GSAP 3.12.5**, loaded from cdnjs, not copied here: proprietary, under the [Standard "No Charge" GSAP License](https://gsap.com/licensing/), **not MIT**. Commercial use is covered. The page renders its final state if GSAP is missing.
- **Cabinet Grotesk**, loaded from Fontshare, not copied here: **licence terms not verified**. Fontshare's pages did not show them to our fetch. Check them before relying on this font.
- Python dependencies, from their package metadata: FastAPI, Pydantic, pytest, PyYAML: MIT. httpx, Uvicorn, Starlette: BSD-3-Clause.
- npm, from `package-lock.json`: React, Vite, Tailwind CSS: MIT. Build-time only, not MIT: lightningcss (MPL-2.0), caniuse-lite (CC-BY-4.0), tar, chownr, minipass, yallist (BlueOak-1.0.0).
- Container images, used as is, licences not re-checked here: Postgres 17, Caddy, Python, Docker CLI.
