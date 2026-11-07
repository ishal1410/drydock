// Run: node src/run.check.mjs   Asserts the honesty rules the UI depends on, including the cases no fixture exercises.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { noRunLine, trackScale, attemptsOf, dash, evalLine, moneyLines, plainReason, revisedLine, whyFailed, plural, nowViolating, range, verdictLine, warningsFor, author, approvalOf, usd, pollRun, rowLabel, attemptMoney, backedLookup, primaryLine } from './run.js'

const FX = JSON.parse(readFileSync(new URL('./fixtures.json', import.meta.url)))
const [a1, a2] = attemptsOf(FX.runs.E8)

assert.equal(range(a1.contract.contract.expected_rows.invoices), '85-95')
assert.equal(verdictLine(a1, 'B'), 'Red. 3 customers hit by a company rule.')
assert.equal(whyFailed(a1), 'Company rule the AI was not shown up front: Invoices on an active payment plan are never written off.')
assert.equal(whyFailed(a2), null)
assert.equal(revisedLine(a1, a2), 'Revised to keep the rule: 3 customers left out, $1,169.18 protected. New promise: 80-90 rows.')
assert.equal(revisedLine(null, a2), null)
assert.equal(plainReason('2 tables changed; 162 rows > 50; a policy failed in an earlier attempt; 1 revision used'), '2 tables changed, 162 rows (ops limit 50), an earlier attempt broke a company rule, needed a second attempt')
assert.equal(plainReason('68 rows > 50; $6,826.90 moved, over the ops limit of $2,500.00'), '68 rows (ops limit 50), $6,826.90 moved (ops limit $2,500.00)')
assert.deepEqual(moneyLines(a2.effect).map((m) => m.label), ['amount in invoices, value of the 87 changed rows', 'balance in accounts, across the 75 changed rows'])
assert.equal(plural(1, 'revision'), '1 revision')
assert.equal(plural(2, 'revision'), '2 revisions')
assert.equal(verdictLine(a2, 'B'), 'Green. Waiting for engineer approval.')
assert.equal(verdictLine(attemptsOf(FX.runs.E11)[0], 'C'), 'Red. Stopped by disk spill cap.')
assert.equal(verdictLine({ green: true }, 'C'), 'Green, but locked. Nobody can approve.')
assert.equal(moneyLines(a2.effect)[0].value, '$31,319.00')
assert.equal(usd(a1.failures[0].rows.reduce((t, r) => t + r.owed_cents, 0)), '$1,169.18')

assert.deepEqual(nowViolating([{ rows: [{ customer: 'A' }, { customer: 'A' }, { customer: 'B' }] }, { rows: [{ customer: 'B' }] }]), ['A', 'B'])
assert.equal(verdictLine({ green: false, failures: [{ kind: 'policy', detail: 'P-7: x', rows: [{ customer: 'A' }, { customer: 'A' }] }] }), 'Red. 1 customer hit by a company rule.')
assert.deepEqual(warningsFor({ green: false, warnings: ['w'] }), [])
assert.deepEqual(warningsFor({ green: true, warnings: ['w'] }), ['w'])
assert.equal(moneyLines({ money: [{ key: 'k', headline: true, direction: 'mixed', rows: 2, before_cents: null, after_cents: null, delta_cents: null }] })[0].value, 'Not summed')
assert.equal(evalLine(null), 'Eval: not run yet')
assert.equal(dash(null), '-')
assert.equal(dash(0), 0)
assert.equal(author('check'), 'AI-written')
assert.equal(author('policy'), 'DryDock-checked')
assert.equal(approvalOf('C'), null)
assert.equal(approvalOf('no-op'), null)
const at = trackScale({ promised: [80, 90] })
assert.equal(at(80), 25)
assert.equal(at(90), 75)
assert.equal(at(0), 0)
// B12: every contract rejected, no compare step: a real message, not a blank page
assert.equal(noRunLine({ verdict: { failures: [{ detail: 'contract rejected: reply held no JSON object' }] } }), 'No change ran: contract rejected: reply held no JSON object.')
assert.equal(noRunLine({ verdict: { failures: [{ detail: 'contract rejected: 2 errors\nfield x\nfield y' }] } }), 'No change ran: contract rejected: 2 errors.')
assert.equal(noRunLine({}), 'No change ran.')
assert.equal(attemptsOf(FX.runs.E11).length, 1)
assert.equal(evalLine({ correct: 2, total: 2, scope: 'E8, E11 x1' }), 'Eval: 2/2 correct vs hand-written goldens (reduced: E8, E11 x1)')
// Long runs: POST answers at once and the screen waits here, so a slow model never hangs a request.
{
  const replies = (...r) => { let i = 0; return async () => { const x = r[Math.min(i++, r.length - 1)]; if (x instanceof Error) throw x; return x } }
  const noWait = async () => {}
  const http = (status) => Object.assign(new Error('http'), { status })
  assert.equal((await pollRun(replies({ status: 'running' }, { status: 'running' }, { status: 'done', id: 1 }), noWait)).id, 1)
  assert.equal((await pollRun(replies({ status: 'unavailable', card: 'x' }), noWait)).card, 'x') // the caller turns it into the card
  assert.equal((await pollRun(replies(new TypeError('net'), http(502), { status: 'done', id: 2 }), noWait)).id, 2) // a blip is not a failure
  await assert.rejects(pollRun(replies(http(404)), noWait), (e) => e.status === 404) // the run is gone: say so at once
  await assert.rejects(pollRun(replies(new TypeError('net')), noWait), TypeError) // a dead server ends the wait
  await assert.rejects(pollRun(replies({ status: 'running' }), noWait, { limit: 3 }), /too long/) // never spins forever
}
// B17: a live row has no fixture `label`, but it does have the invoice number
assert.equal(rowLabel({ label: 'INV-1', pk: '7' }), 'INV-1')
assert.equal(rowLabel({ pk: '7', after: { number: 'INV-2' } }), 'INV-2')
assert.equal(rowLabel({ pk: '7', before: { number: 'INV-3' }, after: null }), 'INV-3')
assert.equal(rowLabel({ pk: '7', after: { status: 'open' } }), '7')
// B19: a rejected attempt's money is not a result; only a green attempt shows it
const eff = { money: [{ key: 'invoices.amount_cents', delta_cents: -100, rows: 1, comparable: true, scope_label: 'x' }] }
assert.deepEqual(attemptMoney({ green: false, effect: eff }, 'red'), [])
assert.deepEqual(attemptMoney({ green: true, effect: eff }, 'ink'), [])
assert.equal(attemptMoney(a2, 'green').length > 0, true)
// Headline: only the lookup the promise rests on. Live E8 run 242b6c19: the last invoices lookup was a count(*) (1 result row),
// the promise 87; "AI counted 1 row" above "87 promised" reads as a bug, so no headline.
assert.equal(backedLookup({ lookup: 90, promised: [90, 90] }), 90)
assert.equal(backedLookup({ lookup: 90, promised: [85, 95] }), 90)
assert.equal(backedLookup({ lookup: 2, promised: [1, 1] }), 2) // E1: counted 2 duplicate charges, promised to remove 1
assert.equal(backedLookup({ lookup: 3, promised: [2, 2] }), 3) // E5: counted 3 accounts, promised to merge 2
assert.equal(backedLookup({ lookup: 1, promised: [87, 87] }), null)
assert.equal(backedLookup({ lookup: null, promised: [87, 87] }), null)
assert.equal(backedLookup(null), null)
{
  const live = { lines: [{ kind: 'row_count', title: 'invoices rows changed', lookup: 1, promised: [87, 87], measured: 87 },
    { kind: 'row_count', title: 'accounts rows changed', lookup: null, promised: [1, 90], measured: 75 }] }
  assert.equal(backedLookup(primaryLine(live)), null)
}
console.log('run.js checks pass')
