// GSAP from cdnjs at a pinned version with SRI. The page's default render is always the final state, so a blocked CDN
// or prefers-reduced-motion simply means no animation. No new npm packages (user rule).
const V = 'https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/'
const FILES = [
  [`${V}gsap.min.js`, 'sha384-g4NTh/Iv5PPU4xPyhEWqPcwtNXOvdaDI8LLnyYfyNZOjKJeYQyjzQ9X5275eBjpt'],
  [`${V}ScrollTrigger.min.js`, 'sha384-Z3REaz79l2IaAZqJsSABtTbhjgOUYyV3p90XNnAPCSHg3EMTz1fouunq9WZRtj3d'],
]
const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches

const inject = ([src, integrity]) => new Promise((ok, fail) => {
  const s = Object.assign(document.createElement('script'), { src, integrity, crossOrigin: 'anonymous', onload: ok, onerror: fail })
  document.head.append(s)
  setTimeout(fail, 4000)
})

let loading
export function loadGsap() {
  if (reduced()) return Promise.resolve(null)
  loading ||= (async () => {
    try {
      for (const f of FILES) await inject(f)
      window.gsap.registerPlugin(window.ScrollTrigger)
      return window.gsap
    } catch { return null }
  })()
  return loading
}

// Sync accessor for layout effects: animate only if GSAP is already here, otherwise show the final state with no flash.
export const G = () => (!reduced() && window.gsap) || null

export const EASE = 'expo.out' // closest GSAP curve to the mock's cubic-bezier(.16,1,.3,1)

// Why: the bar races for the run's elapsed_ms and stops dead at the cap, so the containment is seen, not described.
// In fixtures that value is a sample, not a measurement.
// Deliberately no ease-out on the stop: a cap does not decelerate.
export function stopScene(g, el, ms, vars = {}, fmt = (v) => `${(v / 1000).toFixed(2)} sec`) { // one stop-time format, as on the landing (qa.md)
  const num = el.querySelector('.m')
  const o = { v: 0 }
  el.classList.add('counting')
  num.textContent = fmt(0) // the render holds the final value for no-GSAP; never show it over an empty bar
  const tl = g.timeline(vars)
  tl.fromTo(el.querySelector('.race-fill'), { clipPath: 'inset(0 100% 0 0 round 16px)' },
    { clipPath: 'inset(0 0% 0 0 round 16px)', duration: ms / 1000, ease: 'power2.in' }, 0)
  tl.fromTo(o, { v: 0 }, { v: ms, duration: ms / 1000, ease: 'power2.in', onUpdate() { num.textContent = fmt(o.v) }, onComplete() { num.textContent = fmt(ms) } }, 0)
  tl.call(() => el.classList.remove('counting'))
  tl.fromTo(el.querySelectorAll('.reveal'), { opacity: 0, y: 12 }, { opacity: 1, y: 0, duration: 0.4, ease: EASE, stagger: 0.08 }, '+=0.12')
  return tl
}
