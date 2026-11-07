import { useEffect, useRef, useState } from 'react'
import FX from './fixtures.json'
import { CheckLines, StoppedCard } from './ui.jsx'
import { attemptsOf, nowViolating, owedBy, primaryLine, range, int, usd } from './run.js'
import { loadGsap, stopScene } from './motion.js'
import { LIVE } from './api.js'

const CTA = LIVE ? { href: '?app', label: 'Open the live demo' } : { href: '?fixture', label: 'Open sample runs' }

const E8 = attemptsOf(FX.runs.E8)
const E11 = FX.runs.E11
const E11_ATTEMPTS = attemptsOf(E11)
const VERBS = ['AI promises the rows.', 'Runs it on a copy.', 'DryDock measures it.', 'Wrong? AI retries.', 'You approve. You can undo.']
const SOURCES = {
  atlassian: 'https://www.atlassian.com/engineering/post-incident-review-april-2022-outage',
  circle: 'https://eng-practices.circle.com/learn/code-review/5-whys/sql-change',
}
// Landing-only words, computed from the fixture. The app keeps verdictLine() and its technical strings.
const HURT = nowViolating(E8[0].failures).length
const RISK = usd(owedBy(E8[0].failures).arr).replace('.00', '')
const HERO_VERDICT = {
  1: `Attempt 1: red. Would write off ${HURT} customers on payment plans.`,
  2: `Attempt 2: left them out, ${usd(owedBy(E8[0].failures).owed)} protected. Green.`,
}
// Live builds name what serves runs, from /api/health (the server's config), never a hard-coded model.
function useLiveModel() {
  const [model, setModel] = useState(null)
  useEffect(() => {
    if (!LIVE) return
    fetch('/api/health').then((r) => r.json()).then((h) => setModel(h?.model || null)).catch(() => {})
  }, [])
  return model
}
const sec = (ms) => `${(ms / 1000).toFixed(2)} sec`

function HeroState({ attempt, className }) {
  const l = primaryLine(attempt)
  // the hero shows the biggest table; the others are named too, so it never reads as the whole change (live-design P1)
  const rest = (attempt.lines || []).filter((x) => x.kind === 'row_count' && x !== l && x.measured)
  return (
    <div className={`st ${className} ${attempt.green ? 'green' : 'red'}`}>
      <div className="nums">
        <div><b>{range(l.promised)}</b><span>rows promised</span></div>
        <div><b className="m">{int(l.measured)}</b><span>{l.title}</span></div>
      </div>
      {rest.length > 0 && <p className="fact">{rest.map((x) => `+${int(x.measured)} ${x.title}`).join(' · ')}</p>}
      <p className="v">{HERO_VERDICT[attempt.attempt]}</p>
    </div>
  )
}

export default function Landing() {
  const liveModel = useLiveModel()
  const e11 = useRef(null)
  const verbs = useRef(null)

  useEffect(() => {
    let dead, ctx, refresh
    loadGsap().then((g) => {
      if (!g || dead) return
      ctx = g.context(() => {
        // Why: lighting each verb as it passes reads the sequence as a sequence, without step numbers.
        verbs.current.querySelectorAll('li').forEach((li) => {
          g.fromTo(li, { '--t': 0 }, { '--t': 1, ease: 'none', scrollTrigger: { trigger: li, start: 'top 65%', end: 'bottom 65%', scrub: true } })
        })
        // Why: a race cannot be scrubbed, a stop has to happen in real time, so it plays once, with the whole card in view.
        // If GSAP arrives after the card is already on screen or passed, the final state stays: never erase what was read.
        if (e11.current.getBoundingClientRect().top >= innerHeight) {
          stopScene(g, e11.current, E11.stopped.elapsed_ms, { scrollTrigger: { trigger: e11.current, start: 'top 70%', once: true } }, sec)
        }
      })
      // Why: the font stylesheet no longer blocks render, so its swap can land after fonts.ready and move every trigger.
      refresh = () => window.ScrollTrigger.refresh()
      document.fonts?.ready.then(refresh)
      document.fonts?.addEventListener('loadingdone', refresh)
    })
    return () => { dead = true; ctx?.revert(); document.fonts?.removeEventListener('loadingdone', refresh) }
  }, [])

  const a1 = E8[0]
  const showcase = [
    { ...a1.lines.find((l) => l.kind === 'row_count'), title: 'Invoices changed: in range' },
    { ...a1.lines.find((l) => l.title === 'no_paid_written_off'), title: 'No paid invoice written off' },
    { ...a1.lines.find((l) => l.title === 'P-7'), title: 'Payment plans untouched' },
  ]

  return (
    <>
      <header className="bar">
        <span className="logo">DryDock</span>
        <small>Built for Vultr Agent Rush{LIVE ? <>{liveModel && <> · Live runs on {liveModel}</>} · <a href="?fixture">Or see scripted samples</a></> : ' · Sample runs. No live model yet.'}</small>
      </header>
      <main>
        <div className="wrap">
          <div className="hero">
            <div>
              <h1>Promise first.<br /><span>Then proof.</span></h1>
              <p className="sub">For billing and ops teams. AI tries each data fix on a throwaway copy. DryDock measures it before anyone approves.</p>
              <a className="cta" href={CTA.href}>{CTA.label}</a>
            </div>
            <section className="run" aria-label="Sample run">
              <h2>{FX.runs.E8.ticket.subject}</h2>
              <div className="stage">
                <HeroState attempt={E8[0]} className="a" />
                <HeroState attempt={E8[1]} className="b" />
              </div>
              <p className="note">Scripted example on synthetic data. No model ran.</p>
            </section>
          </div>
        </div>

        <section className="sec problem split" aria-labelledby="problem-h">
          <h2 className="big" id="problem-h">One bad script.<br /><span className="harm">883 sites.</span></h2>
          <div className="sources">
            <p><a href={SOURCES.atlassian} rel="noreferrer" target="_blank">Atlassian, 2022</a>: a deletion script got the wrong IDs. 775 customers hit.</p>
            <p><a href={SOURCES.circle} rel="noreferrer" target="_blank">Circle</a>: “The WHERE clause was accidentally removed.”</p>
          </div>
        </section>

        <section className="sec how split" aria-labelledby="how-h">
          <h2 className="big" id="how-h">Now hand that job to an AI.</h2>
          <ol className="verbs" ref={verbs}>{VERBS.map((v) => <li key={v}>{v}</li>)}</ol>
        </section>

        <section className="sec trust split" aria-labelledby="trust-h">
          <div>
            <h2 className="big" id="trust-h">Code checks the AI’s promise.</h2>
            <p className="sub wide">Plus company rules the AI is not shown up front. A red final run locks Approve.</p>
          </div>
          <div className="run">
            <CheckLines lines={showcase} plain tag={(k) => (k === 'check' ? 'Written by AI, graded by code' : 'Graded by code')} />
            <div className="lock">
              <button className="btn" disabled>Approve</button>
              <p>Locked until an attempt passes.</p>
            </div>
            <p className="note">Sample run.</p>
          </div>
        </section>

        <section className="sec containment" aria-labelledby="stop-h">
          <h2 className="big" id="stop-h">A fix that ran away.<br />Stopped. Nothing touched.</h2>
          <StoppedCard
            run={E11}
            attempt={E11_ATTEMPTS[0]}
            title={`Ticket: ${E11.ticket.subject}`}
            cardRef={e11}
            note="Sample run. On a laptop, hand-written test SQL stopped in 1.03 to 1.12 sec."
            words={{
              cap: 'Safety limit',
              stat: sec(E11.stopped.elapsed_ms),
              statLabel: 'to stop it',
              verdict: <>Stopped on the first attempt. Copy destroyed. <span className="safe">Sample data unchanged. Approve locked.</span></>,
            }}
          />
        </section>

        <section className="sec pricing split" aria-labelledby="price-h">
          <div>
            <h2 className="big" id="price-h">{RISK} a year at risk.<br />Built to catch it before approval.</h2>
          </div>
          <div>
          <div className="prices">
            <div><b>$20</b><span>per ops seat</span></div>
            <i aria-hidden="true">+</i>
            <div><b>$0.99</b><span>per approved change</span></div>
          </div>
          <p className="sub wide">A pricing hypothesis, untested with buyers.</p>
          </div>
        </section>

        <section className="sec close split" aria-labelledby="close-h">
          <h2 className="big" id="close-h">Promise first.<br /><span className="then">Then proof.</span></h2>
          <div className="close-row">
            <a className="cta" href={CTA.href}>{CTA.label}</a>
          </div>
        </section>
      </main>

      <footer className="foot"><span className="logo">DryDock</span></footer>
    </>
  )
}
