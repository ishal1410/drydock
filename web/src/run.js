// Pure helpers that turn API fields into what the screens say. No React, so `node src/run.check.mjs` can assert them.
export const dash = (v) => (v === null || v === undefined || v === '' ? '-' : v)
export const int = (v) => (v === null || v === undefined ? '-' : Number(v).toLocaleString('en-US'))
export const usd = (c) => (c === null || c === undefined ? '-'
  : `${c < 0 ? '-' : ''}$${(Math.abs(c) / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`)
export const signedUsd = (c) => (c > 0 ? `+${usd(c)}` : usd(c))
export const range = (p) => (!p ? '-' : p[0] === p[1] ? int(p[0]) : `${int(p[0])}-${int(p[1])}`)
export const short = (h) => (h ? h.slice(0, 6) : '-')
export const clock = (iso) => (iso ? `${iso.slice(11, 19)} UTC` : '-')
export const ms = (v) => (v === null || v === undefined ? '-' : `${int(v)} ms`)
export const mib = (b) => (b === null || b === undefined ? '-' : `${Math.round(b / 1048576)} MB`)
export const rows = (n) => `row${n === 1 ? '' : 's'}`
export const plural = (n, word) => `${int(n)} ${word}${n === 1 ? '' : 's'}`
export const author = (kind) => (kind === 'check' ? 'AI-written' : 'DryDock-checked')

// One entry per compare step, with the contract, SQL and run result that produced it.
export function attemptsOf(run) {
  const out = []
  let contract = null, sql = null, ran = null
  for (const s of run.trace?.steps || []) {
    if (s.name === 'contract') contract = s.payload
    if (s.name === 'sql') sql = s.payload?.sql
    if (s.name === 'run') ran = s.payload
    if (s.name === 'compare') out.push({ ...s.payload, contract, sql, ran })
  }
  return out
}

export function primaryLine(a) {
  return (a?.lines || []).filter((l) => l.kind === 'row_count').sort((x, y) => (y.measured ?? 0) - (x.measured ?? 0))[0] || null
}

// The lookup a promise rests on: it counted at least the promise's low end (the promise is those rows or a subset, like
// E1's 2 charges counted, 1 removed). The API sends the table's latest lookup, which can be a count(*) answering
// "1 row" under an 87-row promise (live E8 run 242b6c19); that backs nothing, so the screen says nothing about it.
export const backedLookup = (line) => (line?.promised && line.lookup !== null && line.lookup !== undefined
  && line.lookup >= line.promised[0] ? line.lookup : null)

export function inRange(line) {
  if (!line?.promised || line.measured === null || line.measured === undefined) return null
  return line.measured >= line.promised[0] && line.measured <= line.promised[1]
}

export const nowViolating = (failures) => [...new Set((failures || []).flatMap((f) => (f.rows || []).map((r) => r.customer)).filter(Boolean))]

// Why it failed, in the company's own words: the policy text DryDock checks and the AI is not given up front.
export function whyFailed(a) {
  const f = (a?.failures || []).find((x) => x.kind === 'policy' && x.text)
  return a && !a.green && f ? `Company rule the AI was not shown up front: ${f.text}` : null
}

// Why the promise moved between a red attempt and the green one: what it left out and what that protected.
export function revisedLine(prev, cur) {
  if (!prev || prev.green || !cur?.green) return null
  const names = nowViolating(prev.failures)
  const owed = owedBy(prev.failures)
  const line = primaryLine(cur)
  if (!names.length) return null
  return `Revised to keep the rule: ${plural(names.length, 'customer')} left out${owed ? `, ${usd(owed.owed)} protected` : ''}.`
    + (line ? ` New promise: ${range(line.promised)} rows.` : '')
}

// approval_class.reason in plain words, for the line above Approve. Unknown parts pass through unchanged.
export function plainReason(reason) {
  return (reason || '').split('; ').filter(Boolean).map((p) => {
    const m = p.match(/^(\d+) rows > (\d+)$/)
    if (m) return `${m[1]} rows (ops limit ${m[2]})`
    const cash = p.match(/^(\$[\d,.]+) moved, over the ops limit of (\$[\d,.]+)$/)
    if (cash) return `${cash[1]} moved (ops limit ${cash[2]})`
    if (p === 'a policy failed in an earlier attempt') return 'an earlier attempt broke a company rule'
    if (/^1 revision used$/.test(p)) return 'needed a second attempt'
    if (p === 'free-text request') return 'typed, not a listed ticket'
    return p.replace(', ticket card', '')
  }).join(', ')
}

export function verdictLine(a, approvalClass) {
  if (!a) return '-'
  if (a.green) {
    if (approvalClass === 'A') return 'Green. Waiting for ops approval.'
    if (approvalClass === 'B') return 'Green. Waiting for engineer approval.'
    if (approvalClass === 'no-op') return 'Green. 0 rows changed, nothing to approve.'
    return 'Green, but locked. Nobody can approve.'
  }
  const names = nowViolating(a.failures)
  if (names.length) return `Red. ${plural(names.length, 'customer')} hit by a company rule.`
  const d = a.failures?.[0]?.detail || 'A check failed'
  return `Red. ${d[0].toUpperCase()}${d.slice(1)}.`
}

// B12: nothing reached a compare step (every contract refused). First line only: pydantic errors run long.
export function noRunLine(run) {
  const d = (run?.verdict?.failures?.[0]?.detail || '').split('\n')[0]
  return d ? `No change ran: ${d}.` : 'No change ran.'
}

export const warningsFor = (a) => (a?.green ? a.warnings || [] : [])

// Headline money only. A mixed charge/refund column has no meaningful sum (core/measure.py), so it never gets one.
// Why the green guard: a rejected attempt's sums are not a result. 'ink' is after a reject, undo or close: not live any more.
export const attemptMoney = (attempt, tone) => (attempt?.green && tone !== 'ink' ? moneyLines(attempt.effect) : [])
export const rowLabel = (r) => r.label || r.after?.number || r.before?.number || r.pk

export function moneyLines(effect) {
  const heads = (effect?.money || []).filter((m) => m.headline)
  // Two tables, two figures: say which is which ("balance in accounts"), or two bare dollar lines read as one change twice
  const where = (m) => (heads.length > 1 ? `${m.key.split('.')[1].replace(/_cents$/, '').replace(/_/g, ' ')} in ${m.key.split('.')[0]}, ` : '')
  return heads.map((m) => {
    if (m.direction === 'mixed' || (m.after_cents == null && m.delta_cents == null)) {
      return { key: m.key, value: 'Not summed', label: `${int(m.rows)} rows mix charges and refunds` }
    }
    return {
      key: m.key,
      value: m.column_changed ? signedUsd(m.delta_cents) : usd(m.after_cents),
      label: where(m) + m.scope_label,
      flag: m.null_rows ? `${int(m.null_rows)} rows had no value` : null,
    }
  })
}

export function owedBy(failures) {
  const rows = (failures || []).flatMap((f) => f.rows || [])
  if (!rows.length) return null
  return { owed: rows.reduce((s, r) => s + (r.owed_cents || 0), 0), arr: rows.reduce((s, r) => s + (r.arr_cents || 0), 0) }
}

// Who can approve: the class comes from the API, never decided here.
export function approvalOf(cls) {
  if (cls === 'A') return { role: 'ops', label: 'Approve as ops lead' }
  if (cls === 'B') return { role: 'engineer', label: 'Approve as engineer' }
  return null
}

export const evalLine = (e) => (e ? `Eval: ${e.correct}/${e.total} correct vs hand-written goldens${e.scope ? ` (reduced: ${e.scope})` : ''}` : 'Eval: not run yet')

// Position on the range track, in percent. The promise fills the middle 50%, so landing inside or outside is the
// thing you see at any scale. A measurement far outside clamps to the end of the track.
export function trackScale(line) {
  const [lo, hi] = line?.promised || [0, 0]
  const span = hi - lo || Math.max(1, lo * 0.1)
  const min = lo - span / 2
  return (v) => Math.max(0, Math.min(100, (((v ?? 0) - min) / (span * 2)) * 100))
}

// Why: a run takes minutes, so POST answers at once and the screen waits here. One blip on a phone must not end a run
// that is still going; a 4xx means the run is gone, so that ends the wait straight away.
export async function pollRun(get, wait, { everyMs = 1500, misses = 5, limit = 240 } = {}) {
  let missed = 0
  for (let i = 0; i < limit; i++) {
    try {
      const run = await get()
      if (run.status !== 'running') return run
      missed = 0
    } catch (e) {
      if ((e.status >= 400 && e.status < 500) || ++missed > misses) throw e
    }
    await wait(everyMs)
  }
  throw new Error('This run is taking too long. Try again in a moment.')
}
