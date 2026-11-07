// One client for both modes. Live = the Task 6 orchestrator API. Fixture (?fixture in the URL) = seed-computed
// sample runs from fixtures.json (web/make_fixtures.py), so every screen renders with no backend.
// ponytail: fixture mode is opt-in by URL only; a failing live API never falls back to fixtures (no replay, cut S1).
import FX from './fixtures.json'
import { pollRun } from './run.js'

const params = new URLSearchParams(location.search)
export const FIXTURE = params.has('fixture')
// The one switch for "is a live model behind this build". `VITE_DRYDOCK_LIVE=1 npm run build` in the deploy path only.
export const LIVE = import.meta.env?.VITE_DRYDOCK_LIVE === '1'
const OUTAGE = params.has('outage') // fixture only: simulate inference down

export class ApiError extends Error {
  constructor(status, card, message) { super(message); this.status = status; this.card = card }
}

async function call(method, path, body) {
  const res = await fetch(path, {
    method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined,
  })
  const data = await res.json().catch(() => null)
  // Task 6 error cards: {card: "model_unavailable" | "busy" | "rate_limited" | "daily_cap" | "invalid", message}
  if (!res.ok) throw new ApiError(res.status, data?.card || 'error', data?.message || data?.detail || res.statusText)
  // Why: a static host that rewrites /api/* to index.html answers 200 with HTML. That is an outage, not an empty success.
  if (data === null) throw new ApiError(res.status, 'error', 'The server did not send JSON')
  return data
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms))

// The whole run or its card, however long the dry run takes. status "unavailable" = the run failed, nothing to approve.
async function finished(id) {
  const run = await pollRun(() => call('GET', `/api/runs/${encodeURIComponent(id)}`), wait)
  if (run.status === 'unavailable') throw new ApiError(503, run.card, run.message)
  return run
}
const fx = { applied: {} }

function fixtureRun(input) {
  if (OUTAGE) throw new ApiError(503, 'model_unavailable', 'Model unavailable, try later')
  // e2e H4: no model runs here, so typed text must never come back as another ticket's replayed green.
  if (!input.ticket_id) throw new ApiError(422, 'sample_only', 'Samples cover only the listed tickets. Your own ticket needs the live demo.')
  return structuredClone(FX.runs[input.ticket_id])
}

export const api = {
  // GET /api/tickets -> {tickets: [ticket], hidden: [ticket_id] (E11 start-up guard), eval: {correct, total, file} | null,
  //                      live_checksum, seed_checksum}
  async tickets() {
    if (!FIXTURE) return call('GET', '/api/tickets')
    return { tickets: FX.tickets, hidden: [], eval: FX.eval, live_checksum: FX.seed_checksum, seed_checksum: FX.seed_checksum }
  },
  // POST /api/runs {ticket_id} | {text} -> {run_id}; then GET /api/runs/{id} until status != "running".
  // onCreated(run_id) fires as soon as the id exists, so a reload during the wait comes back to this run.
  async startRun(input, onCreated) {
    if (!FIXTURE) {
      const { run_id } = await call('POST', '/api/runs', input)
      onCreated(run_id)
      // e2e H2: past this point a run exists, so a failure must not read as "no run started"
      try { await finished(run_id) } catch (e) { e.started = true; throw e }
      return
    }
    await wait(350)
    const run = fixtureRun(input)
    fx[run.run_id] = run
    onCreated(run.run_id)
  },
  async getRun(id) {
    if (!FIXTURE) return finished(id)
    if (!fx[id]) {
      const t = Object.values(FX.runs).find((r) => r.run_id === id)
      if (!t) throw new ApiError(404, 'invalid', 'No such run')
      fx[id] = structuredClone(t)
    }
    return fx[id]
  },
  // POST /api/runs/{id}/approve {role: "ops" | "engineer"} -> {apply, live_checksum_before, live_checksum_after, receipt}
  async approve(id, role) {
    if (!FIXTURE) return call('POST', `/api/runs/${encodeURIComponent(id)}/approve`, { role })
    await wait(500)
    const run = fx[id]
    const sql = run.trace.steps.filter((s) => s.name === 'sql').at(-1).payload.sql
    const sha = await sha256(sql)
    const box = run.strip[0]?.container_id || 'live'
    const t = new Date().toISOString()
    const apply = {
      role: 'executor', caps: { statement_timeout: '5s', temp_file_limit: '64MB', memory: '512m', cpus: 1, pids: 128, network: 'none' },
      container_id: 'live-' + box.slice(0, 8), sql_sha256: sha, approved_sha256: sha, effect_match: true,
      rows_by_table: run.trace.steps.filter((s) => s.name === 'compare').at(-1).payload.effect?.rows_by_table,
      started_at: t, ended_at: t, elapsed_ms: 71,
    }
    const receipt = {
      run_id: id, ticket_id: run.ticket.id, sql, sql_sha256: sha, role: 'executor', caps: apply.caps,
      container_id: apply.container_id, effect_match: true, approval_class: run.approval_class.class, approver_role_asserted: role,
      live_checksum_before: run.live_checksum, live_checksum_after: run.apply_checksum_after,
      rows_by_table: apply.rows_by_table, started_at: t, ended_at: t, fixture: true,
    }
    receipt.receipt_sha256 = await sha256(JSON.stringify(receipt))
    fx.applied[id] = true
    return { apply, live_checksum_before: run.live_checksum, live_checksum_after: run.apply_checksum_after, receipt }
  },
  // POST /api/runs/{id}/undo -> {checksum, matches_before, task_end: {destroyed_at, reset_at, new_live_id, checksum_ok}}
  async undo(id) {
    if (!FIXTURE) return call('POST', `/api/runs/${encodeURIComponent(id)}/undo`)
    await wait(400)
    return { checksum: fx[id].live_checksum, matches_before: true, task_end: taskEnd() }
  },
  // POST /api/runs/{id}/close {reason: "close" | "reject"} -> {destroyed_at, reset_at, new_live_id, checksum_ok}
  async close(id, reason = 'close') {
    if (!FIXTURE) return call('POST', `/api/runs/${encodeURIComponent(id)}/close`, { reason })
    await wait(400)
    // e2e M2: like orch/apply.py NO_RESET, ending a run that never applied resets nothing, so it claims no reset
    return fx.applied[id] ? taskEnd() : { ...taskEnd(), reset_at: null }
  },
}

function taskEnd() {
  const t = new Date()
  return { destroyed_at: t.toISOString(), reset_at: new Date(t.getTime() + 2140).toISOString(), new_live_id: 'live-' + Math.random().toString(16).slice(2, 10), checksum_ok: true }
}

export async function sha256(text) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('')
}
