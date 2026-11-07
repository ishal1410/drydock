// Real product components, shared by the app and the landing page so the landing's visuals are the actual UI.
import { attemptMoney, author, dash, inRange, int, nowViolating, owedBy, plural, primaryLine, range, revisedLine, rows, trackScale, usd, verdictLine, warningsFor, whyFailed } from './run.js'

export function RangeTrack({ line }) {
  if (!line?.promised) return null
  const [lo, hi] = line.promised
  // design 7: an exact promise that was kept has nothing to show on a track (a lone blob and an empty tick row)
  if (lo === hi && line.measured === lo) return null
  const at = trackScale(line)
  return (
    <div aria-hidden="true">
      <div className="track">
        <i className="band" style={lo === hi ? { left: `calc(${at(lo)}% - 32px)`, width: 64 } : { left: `${at(lo)}%`, width: `${at(hi) - at(lo)}%` }} />
        <i className="marker" style={{ left: `${at(line.measured)}%` }} />
      </div>
      {lo !== hi && (
        <div className="ticks">
          <span style={{ left: `${at(lo)}%` }}>{int(lo)}</span>
          <span style={{ left: `${at(hi)}%` }}>{int(hi)}</span>
        </div>
      )}
    </div>
  )
}

export function ProofCard({ attempt, prev, cls, note, verdict }) {
  const line = primaryLine(attempt)
  const fit = inRange(line)
  const names = nowViolating(attempt?.failures)
  const owed = owedBy(attempt?.failures)
  const why = whyFailed(attempt)
  const revised = revisedLine(prev, attempt)
  const exact = line?.promised && line.promised[0] === line.promised[1]
  const money = attemptMoney(attempt, verdict?.tone)
  const warnings = warningsFor(attempt)
  const others = (attempt?.lines || []).filter((l) => l.kind === 'row_count' && l !== line)
  return (
    <section className={`run ${attempt?.green ? 'green' : 'red'}`} aria-label={`Attempt ${attempt?.attempt ?? ''} result`}>
      {line && (
        <div className="lk-row stagein">
          <h2 className="lk">AI counted {int(line.lookup)} {rows(line.lookup)} in {line.title.split(' ')[0]} first</h2>
          {note && <span className="note">{note}</span>}
        </div>
      )}
      {line && (
        <div className="nums stagein">
          <div><b>{range(line.promised)}</b><span>{rows(exact ? line.promised[0] : 2)} promised</span></div>
          <div><b className={`m${fit ? ' fit' : ''}`}>{int(line.measured)}</b><span>{rows(line.measured)} changed{fit !== null && <em className="reveal">{fit ? (exact ? 'exact match' : 'in range') : 'out of range'}</em>}</span></div>
          {names.length > 0 && <div className="reveal"><b className="hit">{int(names.length)}</b><span>{names.length === 1 ? 'customer' : 'customers'} hit</span></div>}
        </div>
      )}
      {line && <div className="stagein"><RangeTrack line={line} /></div>}
      {others.map((l) => <p className="also reveal" key={l.title}>Also {plural(l.measured, 'row')} changed in {l.title.split(' ')[0]}, {inRange(l) ? 'as promised' : `promised ${range(l.promised)}`}.</p>)}
      {revised && <p className="fact reveal">{revised}</p>}
      <p className={`v reveal ${verdict?.tone || ''}`} role="status">{verdict?.text || verdictLine(attempt, cls)}</p>
      {why && <p className="fact reveal">{why}</p>}
      {names.length > 0 && <p className="fact reveal"><strong>Customers hit:</strong> {names.join(', ')}</p>}
      {owed && <p className="fact reveal">{usd(owed.owed)} still owed. {usd(owed.arr).replace('.00', '')} a year at risk.</p>}
      {money.map((m) => (
        <p className="money reveal" key={m.key}><b>{m.value}</b> <span>{m.label}{m.flag ? `. ${m.flag}` : ''}</span></p>
      ))}
      {warnings.map((w) => <p className="warn reveal" key={w}><strong>Warning:</strong> {w}</p>)}
      {!line && note && <p className="note reveal">{note}</p>}
    </section>
  )
}

// Plain words on the card; the cap's technical name, code and timings are in the run's Sandbox proof disclosure.
// `words` lets the landing add its own verdict.
export function StoppedCard({ run, attempt, title, note, cardRef, data, words }) {
  const s = run.stopped
  const same = s.live_checksum_before && s.live_checksum_before === s.live_checksum_after
  return (
    <section className="run red stopped" ref={cardRef} aria-label="Run stopped by a cap">
      {title && <h2>{title}</h2>}
      <div className="race" aria-hidden="true">
        <div className="race-track"><i className="race-fill" /><i className="race-cap" /></div>
      </div>
      <p className="race-label">{words?.cap ?? 'Safety limit'}</p>
      <div className="nums stack">
        <div><b className="m">{words?.stat ?? `${(s.elapsed_ms / 1000).toFixed(2)} sec`}</b><span className="reveal">{words?.statLabel ?? 'to stop it'}</span></div>
        <div className="reveal"><b>{int(primaryLine(attempt)?.measured)}</b><span>rows changed</span></div>
      </div>
      <p className="v reveal" role="status">{words?.verdict ?? `Stopped by the safety limit.${(s.destroyed_at || run.destroyed_at) ? ' Copy destroyed.' : ''}${same ? ` ${data} data unchanged.` : ''}`}</p>
      {note && <p className="note reveal">{note}</p>}
    </section>
  )
}

// A check's name as words: policies by their rule text, AI-written checks with underscores as spaces.
const checkTitle = (l) => (l.kind === 'policy' && l.detail ? l.detail
  : l.kind === 'check' ? l.title.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase()) : l.title)

export function CheckLines({ lines, plain = false, tag = author }) {
  return (
    <ul className="checks">
      {lines.map((l, i) => (
        <li key={`${l.title}${i}`} className={l.ok ? 'pass' : 'fail'}>
          <div className="check-head">
            <strong>{plain ? l.title : checkTitle(l)}</strong>
            <span className="res">{l.ok ? 'Pass' : 'Fail'}</span>
            <span className="tag">{tag(l.kind)}</span>
          </div>
          {!plain && l.kind === 'row_count' && <p>AI counted {int(l.lookup)}, promised {range(l.promised)}, changed {int(l.measured)}</p>}
          {!plain && l.kind === 'policy' && <p>Company rule {l.title}. Rows breaking it: {dash(l.before)} before, {dash(l.after)} after.{l.ok && l.before > 0 && l.after === l.before ? ' Already there before this change; none added.' : ''}</p>}
          {!plain && l.kind !== 'row_count' && l.kind !== 'policy' && l.detail && <p>{l.detail}</p>}
          {!plain && l.sql && <pre><code>{l.sql}</code></pre>}
        </li>
      ))}
    </ul>
  )
}
