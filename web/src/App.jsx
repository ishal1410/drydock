import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api, FIXTURE, LIVE } from './api.js'
import { CheckLines, ProofCard, StoppedCard } from './ui.jsx'
import { approvalOf, attemptsOf, clock, dash, evalLine, int, mib, ms, noRunLine, plainReason, plural, primaryLine, rowLabel, short, trackScale, usd } from './run.js'
import { EASE, G, loadGsap, stopScene } from './motion.js'

const DATA = FIXTURE ? 'Sample' : 'Live'
const q = () => new URLSearchParams(location.search)
const go = (run) => {
  const u = new URL(location.href)
  if (run) u.searchParams.set('run', run); else u.searchParams.delete('run')
  history.pushState(null, '', u)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

function Nav({ onInbox }) {
  return (
    <nav>
      <a className="logo" href="./">DryDock</a>
      {onInbox && <button className="back" onClick={onInbox}>Inbox</button>}
      {FIXTURE ? <small className="mode">Sample runs, not live</small> : <small>Synthetic demo data</small>}
    </nav>
  )
}

export default function App() {
  const [runId, setRunId] = useState(() => q().get('run'))
  const [working, setWorking] = useState(null)   // {subject} until the run is finished
  const [failure, setFailure] = useState(null)   // ApiError from POST /api/runs

  useEffect(() => {
    loadGsap()
    const on = () => { setRunId(q().get('run')); setFailure(null); scrollTo(0, 0) }
    addEventListener('popstate', on)
    return () => removeEventListener('popstate', on)
  }, [])

  const start = async (input, subject) => {
    setFailure(null); setWorking({ subject }); scrollTo(0, 0)
    try {
      await api.startRun(input, go)
    } catch (e) {
      setFailure(e)
    } finally {
      setWorking(null)
    }
  }

  return (
    <div className="app">
      <Nav onInbox={(runId || failure) && !working ? () => { setFailure(null); go(null) } : null} />
      <main>
        {failure ? <Failure error={failure} onBack={() => { setFailure(null); go(null) }} />
          : working ? <Working subject={working.subject} />
            : runId ? <RunView id={runId} key={runId} onBack={() => go(null)} />
              : <Inbox onStart={start} />}
      </main>
    </div>
  )
}

function useFocusOnMount() {
  const r = useRef(null)
  useEffect(() => { r.current?.focus({ preventScroll: true }) }, [])
  return r
}

function Failure({ error, onBack }) {
  const h = useFocusOnMount()
  const down = error.card === 'model_unavailable'
  // e2e H3: when the model or sandbox host is down, every ticket fails the same way. The samples are the way out.
  // qa.md ISSUE-001: a used-up daily quota also needs the way out to the samples
  const offline = !FIXTURE && ['model_unavailable', 'sandbox_down', 'error', 'daily_cap', 'rate_limited'].includes(error.card)
  return (
    <>
      <h1 className="title red-text" tabIndex={-1} ref={h}>{error.message || 'Something went wrong'}</h1>
      <p className="sub wide">{error.started ? 'The run stopped partway. Nothing was applied.' : down ? 'No run started. Nothing to approve.' : null}</p>
      <div className="row">
        {offline && <a className="btn" href="?fixture">Open sample runs</a>}
        <button className={`btn${offline ? ' ghost' : ''}`} onClick={onBack}>Back to inbox</button>
      </div>
    </>
  )
}

function Working({ subject }) {
  const h = useFocusOnMount()
  // harsh F10: a live run takes minutes; a moving clock shows the page is still waiting on it, not frozen
  const [secs, setSecs] = useState(0)
  useEffect(() => { const t = setInterval(() => setSecs((n) => n + 1), 1000); return () => clearInterval(t) }, [])
  return (
    <>
      <h1 className="title" tabIndex={-1} ref={h}>{subject}</h1>
      <section className="run skeleton" role="status" aria-label="Run in progress">
        <p className="lk">Running on a throwaway copy.</p>
        <div className="nums"><div><b className="bone">00-00</b><span>rows promised</span></div><div><b className="bone">00</b><span>rows changed</span></div></div>
        <p className="v muted">Working for {secs} sec. Can take a few minutes.</p>
      </section>
    </>
  )
}

function Inbox({ onStart }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [text, setText] = useState('')
  const h = useFocusOnMount()

  useEffect(() => { api.tickets().then(setData).catch(setError) }, [])

  // design 6: the two runs that show the product first; the rest keep their order
  const first = ['E8', 'E11']
  const tickets = (data?.tickets || []).filter((t) => !(data.hidden || []).includes(t.id))
    .sort((a, b) => (first.indexOf(a.id) + 1 || 99) - (first.indexOf(b.id) + 1 || 99))
  return (
    <>
      <h1 className="title" tabIndex={-1} ref={h}>Pick a ticket.</h1>
      {error && (
        <div className="run" role="alert">
          <p className="v red-text flat">Can't reach the server.</p>
          {!FIXTURE && <div className="row"><a className="btn" href="?fixture">Open sample runs</a></div>}
        </div>
      )}
      {!error && !data && <ul className="tickets" aria-hidden="true">{[0, 1, 2, 3, 4, 5].map((i) => <li key={i}><div className="ticket"><span><strong className="bone">Month-end write-offs</strong></span></div></li>)}</ul>}
      {data && tickets.length === 0 && <p className="sub wide">No tickets yet. Write your own below.</p>}
      {data && tickets.length > 0 && (
        <ul className="tickets">
          {tickets.map((t) => (
            <li key={t.id}>
              <button className="ticket" onClick={() => onStart({ ticket_id: t.id }, t.subject)}>
                <span><strong>{t.subject}</strong><small>{t.requester}</small></span>
                <em>{FIXTURE ? 'Open' : 'Run'}</em>
              </button>
            </li>
          ))}
        </ul>
      )}
      {data && FIXTURE && <p className="meta">Samples cover only the tickets above. {LIVE ? <a href="?app">Write your own in the live demo.</a> : 'Writing your own ticket needs the live demo.'}</p>}
      {data && !FIXTURE && (
        <form className="own" onSubmit={(e) => { e.preventDefault(); const v = text.trim(); if (v) onStart({ text: v }, 'Your ticket') }}>
          <label htmlFor="own">Or write your own ticket</label>
          <textarea id="own" rows="3" maxLength={2000} value={text} onChange={(e) => setText(e.target.value)}
            placeholder="Write off every open invoice 90+ days overdue" />
          <div className="row"><button className="btn" disabled={!text.trim()}>Run it</button></div>
        </form>
      )}
      {data?.eval && <p className="meta">{evalLine(data.eval)}</p>}
    </>
  )
}

function RunView({ id, onBack }) {
  const [run, setRun] = useState(null)
  const [error, setError] = useState(null)
  const h = useRef(null)

  useEffect(() => { api.getRun(id).then(setRun).catch(setError) }, [id])
  useEffect(() => { if (run) h.current?.focus({ preventScroll: true }) }, [run])

  if (error) {
    return (
      <>
        <h1 className="title red-text">{error.message || 'No such run'}</h1>
        <div className="row"><button className="btn" onClick={onBack}>Back to inbox</button></div>
      </>
    )
  }
  if (!run) return <Working subject="Loading" />
  return (
    <>
      <h1 className="title run-title" tabIndex={-1} ref={h}>{run.ticket?.subject || 'Run'}</h1>
      {run.ticket?.body && <p className="lede">{run.ticket.body}<span className="who"> {run.ticket.requester}</span></p>}
      {run.stopped ? <Stopped run={run} onBack={onBack} /> : <Story run={run} onBack={onBack} />}
    </>
  )
}

function Stopped({ run, onBack }) {
  const card = useRef(null)
  const attempts = attemptsOf(run)
  // Why: the app replays the stop at the run's elapsed_ms (a sample in fixture mode) so the reader sees the cap fire, then the verdict.
  useLayoutEffect(() => {
    const g = G()
    const el = card.current
    if (!g || !el) return undefined
    const ctx = g.context(() => { stopScene(g, el, run.stopped.elapsed_ms) }, el)
    return () => { ctx.revert(); el.classList.remove('counting') }
  }, [run])
  return (
    <>
      <StoppedCard run={run} attempt={attempts[0]} cardRef={card} data={DATA} />
      <Decision run={run} attempts={attempts} view={attempts.length - 1} onView={() => {}} onBack={onBack} />
      <Details run={run} attempt={attempts[attempts.length - 1]} attempts={attempts} />
    </>
  )
}

function Story({ run, onBack }) {
  const attempts = attemptsOf(run)
  const last = Math.max(attempts.length - 1, 0)
  // Why: E8 is told red-then-green once, like the hero. Skipped with no GSAP or reduced motion (final state instantly).
  const storyOn = useRef(attempts.length > 1 && !!G())
  const [view, setView] = useState(storyOn.current ? 0 : last)
  const [done, setDone] = useState(null)
  const root = useRef(null)
  const prev = useRef(0)
  const timer = useRef(null)
  const a = attempts[view]

  const goTo = useCallback((i, byUser) => {
    if (byUser) storyOn.current = false
    clearTimeout(timer.current)
    const g = G()
    if (!g || !root.current) { setView(i); return }
    // Why: the old result leaves upward so red-to-revised reads as a replacement, not an edit (same move as the hero).
    g.to(root.current.querySelectorAll('.run > *, .decision'), {
      autoAlpha: 0, y: -12, duration: 0.2, ease: 'power2.out', overwrite: true, onComplete: () => setView(i),
    })
  }, [])

  useLayoutEffect(() => {
    const g = G()
    const el = root.current
    if (!g || !el || !a) return undefined
    const line = primaryLine(a)
    const cardEl = el.querySelector('.run')
    const num = el.querySelector('.m')
    const marker = el.querySelector('.marker')
    const from = prev.current
    const to = line?.measured ?? 0
    prev.current = to
    g.set(el.querySelectorAll('.run > *, .decision'), { clearProps: 'all' })
    const ctx = g.context(() => {
      cardEl.classList.add('counting')
      const tl = g.timeline({
        onComplete() {
          if (storyOn.current && view < last) timer.current = setTimeout(() => { storyOn.current = false; goTo(last) }, 3200)
        },
      })
      tl.from(el.querySelectorAll('.stagein'), { autoAlpha: 0, y: 12, duration: 0.3, ease: EASE, stagger: 0.04 }, 0)
      if (num) {
        // Why: the number has to arrive before the verdict; that order is the product's whole claim.
        const o = { v: from }
        tl.to(o, { v: to, duration: 0.9, ease: 'power2.out', onUpdate() { num.textContent = int(Math.round(o.v)) }, onComplete() { num.textContent = int(to) } }, 0.05)
      }
      if (marker && line) {
        const at = trackScale(line)
        tl.fromTo(marker, { x: ((at(from) - at(to)) / 100) * marker.parentElement.offsetWidth }, { x: 0, duration: 0.9, ease: 'power2.out' }, 0.05)
      }
      tl.call(() => cardEl.classList.remove('counting'))
      tl.from(el.querySelectorAll('.reveal'), { autoAlpha: 0, y: 12, duration: 0.4, ease: EASE, stagger: 0.06 }, '+=0.08')
      tl.from(el.querySelectorAll('.decision'), { autoAlpha: 0, y: 12, duration: 0.4, ease: EASE }, '<0.1')
    }, el)
    return () => { ctx.revert(); cardEl.classList.remove('counting') }
  }, [view, run])

  useEffect(() => () => clearTimeout(timer.current), [])

  if (!a) {
    return (
      <>
        <p className="sub wide">{noRunLine(run)}</p>
        <Decision run={run} attempts={attempts} view={0} onView={() => {}} onBack={onBack} />
        <Details run={run} attempts={attempts} />
      </>
    )
  }
  return (
    <>
      {attempts.length > 1 && (
        <div className="attempts" role="group" aria-label="Attempts">
          {attempts.map((x, i) => (
            <button key={x.attempt} aria-pressed={i === view} className={x.green ? 'green' : 'red'} onClick={() => i !== view && goTo(i, true)}>
              Attempt {x.attempt}: {x.green ? 'green' : 'red'}
            </button>
          ))}
        </div>
      )}
      <div ref={root}>
        <ProofCard attempt={a} prev={attempts[view - 1]} cls={run.approval_class?.class} verdict={view === last ? done : null} />
        <Decision run={run} attempts={attempts} view={view} onView={(i) => goTo(i, true)} onBack={onBack} onOutcome={setDone} />
      </div>
      <Details run={run} attempt={a} attempts={attempts} />
    </>
  )
}

// One statement per outcome. In a Story it replaces the card's verdict, which must stop saying "waiting for approval".
function outcomeLine(o) {
  if (!o) return null
  if (o.kind === 'applied') return { text: 'Green. Approved and applied.', tone: '' }
  if (o.kind === 'undone') {
    return o.r?.matches_before
      ? { text: 'Undone. Data is back as it was.', tone: 'ink' }
      : { text: 'Undo ran, but the data does not match how it was.', tone: 'bad' }
  }
  if (o.kind === 'closed') return { text: o.reason === 'reject' ? 'Rejected. Nothing was applied.' : 'Ticket closed.', tone: 'ink' }
  return { text: 'Task ended.', tone: 'ink' }
}

function Decision({ run, attempts, view, onView, onBack, onOutcome }) {
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [outcome, setOutcome] = useState(() => (run.task_state === 'applied' ? { kind: 'applied' } : null))
  const cls = run.approval_class?.class
  const last = attempts.length - 1
  const ended = run.task_state === 'ended' && cls !== 'C'
  const line = outcomeLine(outcome || (ended ? { kind: 'ended' } : null))
  useEffect(() => { onOutcome?.(line) }, [line?.text])

  const act = async (name, fn, next) => {
    setBusy(name); setError(null)
    try {
      setOutcome(next(await fn()))
    } catch (e) {
      setError(e)
      // nothing_to_undo: the server already ended the task (idle reaper, or another visitor's run): bugs.md F1
      if (['apply_failed', 'refused', 'nothing_to_undo'].includes(e.card)) setOutcome({ kind: 'ended' })
    } finally {
      setBusy(null)
    }
  }
  const close = (reason) => act(reason, () => api.close(run.run_id, reason), (r) => ({ kind: 'closed', reason, r }))

  if (outcome?.kind && outcome.kind !== 'applied') return <Ended outcome={outcome} head={onOutcome ? null : line} error={error} onBack={onBack} />
  if (outcome?.kind === 'applied') {
    return (
      <Applied run={run} data={outcome.r} error={error} busy={busy} head={onOutcome ? null : line}
        onUndo={() => act('undo', () => api.undo(run.run_id), (r) => ({ kind: 'undone', r, was: outcome.r?.live_checksum_after }))}
        onClose={() => close('close')} />
    )
  }
  if (ended) return <Ended outcome={{ kind: 'ended' }} head={onOutcome ? null : line} onBack={onBack} />

  // A failed attempt on screen must never offer Approve: the button would read as approving what is shown.
  if (view < last) {
    return (
      <div className="decision failed">
        <p className="v flat">This attempt failed. Only attempt {attempts[last].attempt} can be approved.</p>
        <button className="btn" onClick={() => onView(last)}>View attempt {attempts[last].attempt}</button>
      </div>
    )
  }
  const can = attempts[last]?.green ? approvalOf(cls) : null
  const lockCopy = cls === 'no-op' ? 'Nothing to approve. 0 rows changed.' : 'Too risky to approve. Nobody can.'
  const who = can?.role === 'ops' ? 'Ops can approve' : 'Needs an engineer'
  return (
    <div className="decision">
      {can
        ? (
          <>
          {run.approval_class?.reason && <div className="fact why">{who}:<ul>{run.approval_class.reason.split('; ').filter(Boolean).map((p) => <li key={p}>{plainReason(p)}</li>)}</ul></div>}
          <div className="row">
            <button className="btn" disabled={!!busy} onClick={() => act('approve', () => api.approve(run.run_id, can.role), (r) => ({ kind: 'applied', r }))}>
              {busy === 'approve' ? 'Applying' : can.label}
            </button>
            <button className="btn ghost" disabled={!!busy} onClick={() => close('reject')}>{busy === 'reject' ? 'Rejecting' : 'Reject'}</button>
          </div>
          <p className="tagline">Demo: no login, so anyone can press this as the {can.role === 'ops' ? 'ops lead' : 'engineer'}.</p>
          </>
        )
        : (
          <>
            <p className="v flat">{lockCopy}</p>
            <div className="row">
              <button className="btn" disabled>Approve</button>
              <button className="btn ghost" disabled={!!busy} onClick={() => close('close')}>{busy === 'close' ? 'Closing' : 'Close ticket'}</button>
            </div>
          </>
        )}
      {error && <p className="alert" role="alert">{error.message}</p>}
    </div>
  )
}

function useEnter(ref) {
  // Why: apply and undo are the moments the live database changes, so the new checksum visibly replaces the old one.
  useLayoutEffect(() => {
    const g = G()
    if (!g || !ref.current) return undefined
    const ctx = g.context(() => {
      const flips = ref.current.querySelectorAll('.flip')
      const tl = g.timeline().from(ref.current, { autoAlpha: 0, y: 12, duration: 0.35, ease: EASE })
      if (flips.length) tl.from(flips, { yPercent: 100, duration: 0.45, ease: EASE }, 0.15)
    }, ref.current)
    return () => ctx.revert()
  }, [])
}

function Applied({ run, data, error, busy, head, onUndo, onClose }) {
  const ref = useRef(null)
  const [url, setUrl] = useState(null)
  useEnter(ref)
  useEffect(() => {
    if (!data?.receipt) return undefined
    const u = URL.createObjectURL(new Blob([JSON.stringify(data.receipt, null, 2)], { type: 'application/json' }))
    setUrl(u)
    return () => URL.revokeObjectURL(u)
  }, [data])
  const a = data?.apply
  return (
    <div className="decision applied" ref={ref}>
      {head && <p className="v flat green-text" role="status">{head.text}</p>}
      <div className="row">
        <button className="btn" disabled={!!busy} onClick={onUndo}>{busy === 'undo' ? 'Undoing' : 'Undo'}</button>
        <button className="btn ghost" disabled={!!busy} onClick={onClose}>{busy === 'close' ? 'Closing' : 'Close ticket'}</button>
        <a className="btn ghost" href={url || `/api/runs/${encodeURIComponent(run.run_id)}/receipt`} download={`receipt-${run.run_id}.json`}>Download receipt</a>
      </div>
      {error && <p className="alert" role="alert">{error.message}</p>}
      {a && (
        <details className="more">
          <summary>Apply details</summary>
          <dl>
            <div><dt>Data fingerprint before, after</dt><dd>{short(data.live_checksum_before)}, {short(data.live_checksum_after)}</dd></div>
            <div><dt>Role</dt><dd>{dash(a.role)}</dd></div>
            <div><dt>{FIXTURE ? 'Sample container' : 'Container'}</dt><dd>{dash(a.container_id)}</dd></div>
            <div><dt>Caps</dt><dd>{Object.entries(a.caps || {}).map(([k, v]) => `${k} ${v}`).join(', ') || '-'}</dd></div>
            <div><dt>SQL hash matches approval</dt><dd>{a.sql_sha256 && a.sql_sha256 === a.approved_sha256 ? `Yes, ${short(a.sql_sha256)}` : 'No'}</dd></div>
            <div><dt>Effect matches dry run</dt><dd>{a.effect_match ? 'Yes' : 'No'}</dd></div>
            <div><dt>Receipt</dt><dd>{short(data.receipt?.receipt_sha256)}</dd></div>
          </dl>
        </details>
      )}
    </div>
  )
}

function Ended({ outcome, head, error, onBack }) {
  const ref = useRef(null)
  useEnter(ref)
  const r = outcome.r
  const reset = outcome.kind === 'undone' ? r?.task_end : r
  // design 1: after undo the verdict already says the data is back; a reset line there would repeat it
  const line = reset?.reset_at && outcome.kind !== 'undone'
    ? (reset.checksum_ok ? `${DATA} data put back to the starting copy.` : 'Reset ran, but the data does not match the starting copy.')
    : null
  return (
    <div className="decision" ref={ref}>
      {head && <p className={`v flat ${head.tone}`} role="status">{head.text}</p>}
      {line && <p className="fact">{line}</p>}
      {error && <p className="alert" role="alert">{error.message}</p>}
      <div className="row"><button className="btn" onClick={onBack}>Back to inbox</button></div>
    </div>
  )
}

function Step({ s }) {
  const p = s.payload || {}
  const who = { model: 'AI', sandbox: 'Sandbox', drydock: 'DryDock' }[s.actor] || dash(s.actor)
  const t = s.tokens ? `, ${int(s.tokens.in)} in / ${int(s.tokens.out)} out` : ''
  return (
    <li>
      <div className="check-head">
        <strong>{s.name.replace('_', ' ')}</strong>
        <span className="tag">{who}{s.model_id ? `, ${s.model_id}` : ''}{t}, {ms(s.ms)}</span>
      </div>
      {p.text && <p>{p.text}</p>}
      {s.name === 'lookup' && p.result && <p>{int(p.result.count)} rows{Object.entries(p.result.sums || {}).map(([k, v]) => `, ${k} ${k.endsWith('_cents') ? usd(v) : int(v)}`).join('')}</p>}
      {s.name === 'compare' && <p>{p.green ? 'Green' : 'Red'}, {dash(p.command_tag)}</p>}
      {s.name === 'run' && <p>{dash(p.command_tag)}, {ms(p.elapsed_ms)} of {ms(p.cap_ms)}{p.cap_label ? `, stopped by ${p.cap_label}` : ''}{p.code ? ` (${p.code})` : ''}</p>}
      {p.fed_back && <p>Fed back: {p.fed_back.join('; ')}</p>}
      {p.sha256 && <p>Locked before SQL, {short(p.sha256)}, at {clock(p.locked_at)}</p>}
      {p.sql && <pre><code>{p.sql}</code></pre>}
      {p.contract && <pre><code>{JSON.stringify(p.contract, null, 2)}</code></pre>}
      {p.diff && <pre><code>{p.diff.join('\n')}</code></pre>}
    </li>
  )
}

function Details({ run, attempt, attempts }) {
  const lines = attempt?.lines || []
  const failed = lines.filter((l) => !l.ok).length
  const effect = attempt?.effect
  const rows = Object.entries(effect?.sample || {}).flatMap(([t, rs]) => rs.map((r) => ({ t, ...r })))
  const steps = run.trace?.steps || []
  return (
    <div className="details">
      {(run.explanation || run.reply) && (
        <details>
          <summary>Explanation and reply</summary>
          <p>{dash(run.explanation)}</p>
          <h3>Drafted reply</h3>
          <p>{dash(run.reply)}</p>
        </details>
      )}
      <details>
        <summary>Checks: {lines.length - failed} passed, {failed} failed</summary>
        <p className="tagline">Approval class {dash(run.approval_class?.class)}: {dash(run.approval_class?.reason)}</p>
        <p className="tagline">{dash(attempt?.command_tag)}{attempt?.ran ? `, ${ms(attempt.ran.elapsed_ms)}` : ''}</p>
        <CheckLines lines={lines} />
      </details>
      {effect && (
        <details>
          <summary>Rows changed</summary>
          {(effect.column_summary || []).map((c) => <p key={c}>{c}</p>)}
          <table className="diff">
            <thead><tr><th>Row</th><th>Column</th><th>Before</th><th>After</th></tr></thead>
            <tbody>
              {rows.flatMap((r) => (r.changed || []).slice(0, r.op === 'inserted' || r.op === 'insert' ? 3 : 8).map((c) => {
                const v = (o) => (o == null || o[c] == null ? '-' : c.endsWith('_cents') ? usd(o[c]) : String(o[c]))
                return <tr key={`${r.t}${r.pk}${c}`}><td data-h="Row">{rowLabel(r)}</td><td data-h="Column">{c}</td><td data-h="Before">{v(r.before)}</td><td data-h="After">{v(r.after)}</td></tr>
              }))}
            </tbody>
          </table>
          {rows.length > 0 && <p className="tagline">Sample of up to 5 rows per table.</p>}
        </details>
      )}
      <details>
        <summary>What the agent did: {steps.length} steps</summary>
        <ul className="checks">{steps.map((s) => <Step s={s} key={s.n} />)}</ul>
        {attempts.length > 1 && <p className="tagline">{attempts.length} attempts, {plural(run.trace?.revisions ?? 0, 'revision')}.</p>}
      </details>
      <details>
        <summary>Sandbox proof</summary>
        {FIXTURE && <p className="note">Sample values. No VM or model was used.</p>}
        <h3>Self-test</h3>
        <ul className="checks">
          {(run.chips || []).map((c) => (
            <li key={c.name} className={c.ok ? 'pass' : 'fail'}>
              <div className="check-head"><strong>{c.name}</strong><span className="res">{c.ok ? 'Pass' : 'Fail'}</span></div>
              <p>{dash(c.detail)}</p>
            </li>
          ))}
        </ul>
        <h3>Sandbox steps</h3>
        <table className="diff">
          <thead><tr><th>Step</th><th>Role</th><th>Time used</th><th>Memory used</th><th>Stopped by</th></tr></thead>
          <tbody>
            {(run.strip || []).map((r) => (
              <tr key={r.step}>
                <td data-h="Step">{r.step} {r.what}</td><td data-h="Role">{dash(r.role)}</td>
                <td data-h="Time used">{ms(r.elapsed_ms)} of {ms(r.cap_ms)}</td>
                <td data-h="Memory used">{mib(r.mem_peak_bytes)} of {mib(r.mem_cap_bytes)}</td>
                <td data-h="Stopped by">{dash(r.stopped_by)}{r.code ? ` (${r.code})` : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p>Container destroyed at {clock(run.destroyed_at)}. {DATA} data fingerprint at dry run {short(run.live_checksum)}.</p>
        <h3>Ledger</h3>
        <dl>{(run.ledger || []).map((l) => <div key={l.service}><dt>{l.service}</dt><dd>{dash(l.detail)}</dd></div>)}</dl>
      </details>
    </div>
  )
}
