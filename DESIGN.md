---
name: DryDock
description: Promise first, then proof. The measurement arrives before the verdict.
colors:
  accent: "#006bdf"
  "on": "#ffffff"
  red: "#c81e3a"
  green: "#0a7f4f"
  bg: "#f6f6f7"
  ink: "#0e0f12"
  mute: "#4a4d55"
  card: "#ffffff"
  line: "#e3e4e8"
  code: "#f0f1f3"
  accent-dark: "#4d9dff"
  on-dark: "#0d0e11"
  red-dark: "#ff6b81"
  green-dark: "#3ddc97"
  bg-dark: "#0d0e11"
  ink-dark: "#f2f3f5"
  mute-dark: "#a9adb7"
  card-dark: "#17191e"
  line-dark: "#2a2d34"
  code-dark: "#17191e"
typography:
  display:
    fontFamily: "Cabinet Grotesk, system-ui, sans-serif"
    fontSize: "clamp(60px, 5vw, 72px)"
    fontWeight: 800
    lineHeight: 1
    letterSpacing: "-0.03em"
  headline:
    fontFamily: "Cabinet Grotesk, system-ui, sans-serif"
    fontSize: "clamp(36px, 3.4vw, 48px)"
    fontWeight: 800
    lineHeight: 1
    letterSpacing: "-0.03em"
  title:
    fontFamily: "Cabinet Grotesk, system-ui, sans-serif"
    fontSize: "clamp(36px, 3.4vw, 48px)"
    fontWeight: 800
    lineHeight: 1
    letterSpacing: "-0.03em"
  run-title:
    fontFamily: "Cabinet Grotesk, system-ui, sans-serif"
    fontSize: "clamp(26px, 2.5vw, 36px)"
    fontWeight: 800
    lineHeight: 1
    letterSpacing: "-0.03em"
  numeral:
    fontFamily: "Cabinet Grotesk, system-ui, sans-serif"
    fontSize: "clamp(48px, 3.9vw, 56px)"
    fontWeight: 800
    lineHeight: 1
    letterSpacing: "-0.03em"
    fontFeature: "tnum"
  numeral-small:
    fontFamily: "Cabinet Grotesk, system-ui, sans-serif"
    fontSize: "clamp(32px, 3.4vw, 48px)"
    fontWeight: 800
    lineHeight: 1.1
    letterSpacing: "-0.02em"
    fontFeature: "tnum"
  verdict:
    fontFamily: "Cabinet Grotesk, system-ui, sans-serif"
    fontSize: "clamp(24px, 2.2vw, 34px)"
    fontWeight: 700
    lineHeight: 1.15
  card-title:
    fontFamily: "Cabinet Grotesk, system-ui, sans-serif"
    fontSize: "clamp(24px, 2.1vw, 32px)"
    fontWeight: 700
    letterSpacing: "-0.01em"
  label-large:
    fontFamily: "Cabinet Grotesk, system-ui, sans-serif"
    fontSize: "clamp(22px, 2vw, 28px)"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  sub:
    fontFamily: "Cabinet Grotesk, system-ui, sans-serif"
    fontSize: "clamp(22px, 2vw, 30px)"
    fontWeight: 500
    lineHeight: 1.25
  body:
    fontFamily: "Cabinet Grotesk, system-ui, sans-serif"
    fontSize: "clamp(20px, 1.7vw, 26px)"
    fontWeight: 500
    lineHeight: 1.35
  body-small:
    fontFamily: "Cabinet Grotesk, system-ui, sans-serif"
    fontSize: "clamp(18px, 1.5vw, 22px)"
    fontWeight: 500
    lineHeight: 1.35
  label:
    fontFamily: "Cabinet Grotesk, system-ui, sans-serif"
    fontSize: "18px"
    fontWeight: 500
    lineHeight: 1.35
  button:
    fontFamily: "Cabinet Grotesk, system-ui, sans-serif"
    fontSize: "24px"
    fontWeight: 700
    lineHeight: 1.2
  logo:
    fontFamily: "Cabinet Grotesk, system-ui, sans-serif"
    fontSize: "28px"
    fontWeight: 800
    letterSpacing: "-0.02em"
  code:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "16px"
    fontWeight: 500
    lineHeight: 1.5
rounded:
  all: "16px"
spacing:
  xs: "8px"
  sm: "12px"
  md: "16px"
  lg: "24px"
  gutter: "clamp(16px, 3vw, 40px)"
  card: "clamp(24px, 3vw, 44px)"
  stack: "clamp(20px, 2.4vw, 36px)"
  section: "clamp(96px, 13vw, 200px)"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on}"
    typography: "{typography.button}"
    rounded: "{rounded.all}"
    padding: "18px 32px"
  button-primary-disabled:
    backgroundColor: "{colors.line}"
    textColor: "{colors.mute}"
    typography: "{typography.button}"
    rounded: "{rounded.all}"
    padding: "18px 32px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.button}"
    rounded: "{rounded.all}"
    padding: "18px 32px"
  proof-card:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink}"
    rounded: "{rounded.all}"
    padding: "{spacing.card}"
  attempt-toggle:
    textColor: "{colors.mute}"
    rounded: "{rounded.all}"
    padding: "10px 18px"
    height: "44px"
  attempt-toggle-pressed:
    backgroundColor: "{colors.card}"
    rounded: "{rounded.all}"
    padding: "10px 18px"
    height: "44px"
  ticket-input:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.all}"
    padding: "18px 22px"
  range-track:
    backgroundColor: "{colors.line}"
    rounded: "{rounded.all}"
    height: "22px"
  range-band:
    backgroundColor: "{colors.accent}"
    rounded: "{rounded.all}"
  race-track:
    backgroundColor: "{colors.line}"
    rounded: "{rounded.all}"
    height: "32px"
  code-block:
    backgroundColor: "{colors.code}"
    typography: "{typography.code}"
    rounded: "{rounded.all}"
    padding: "16px 18px"
---

# Design System: DryDock

## Overview

**Creative North Star: "The Reading Before the Verdict"**

DryDock looks like an instrument taking a reading. A promise is on the left, a measurement lands beside it, and only then does the screen say Red or Green. That order is the product's whole claim, so the type, the color and the motion all enact it: numerals are the largest thing on a card, color is withheld until the number has landed, and the verdict is a single plain sentence below a hairline.

The world is a cool pale ground, near-black ink, one saturated blue, and one heavy grotesk with very few words. The user rejected oversized type and blank space on 2026-11-06, so sizes stay at the scale below and section gaps under 96px. Each screen has one focal point, and everything an expert might want to audit sits behind native disclosures, not on the default view. Landing and app share the same real components, so the landing's visuals are the actual UI.

The user rejected three earlier looks: dark with one orange accent, warm paper with rubber stamps and ticket tabs, and a slate dashboard. Their stated reasons were generic template, cheap or amateur, and boring with no impact.

**Key Characteristics:**
- Numerals first: promise and measurement at display scale, tabular.
- One blue accent, plus red and green that only ever report an outcome.
- One radius (16px) and one family (Cabinet Grotesk) across every surface.
- White cards lifted by a soft tinted shadow, never edged as well.
- Motion is evidence: the default render is always the final state.

## Colors

A cool neutral ground and a single electric blue, with semantic red and green held back until a result exists. Light is the default; the dark set applies under `prefers-color-scheme: dark` unless `data-theme=light` is set, and under `data-theme=dark` directly.

### Primary
- **Vultr Blue** (#006bdf, dark #4d9dff): the one accent, the sponsor's brand blue (#007BFC) darkened to 4.66:1 on Cool Paper and 5.04:1 under white text, and lifted to 6.97:1 under dark On Accent in dark mode. Calls to action, "Then proof." in the hero, the promised band on the range track, the race fill while counting, link underlines, focus rings, selection, text caret, the last verb on the landing, and the fixture label in the nav.
- **On Accent** (#ffffff, dark #0d0e11): text on Vultr Blue and on selection.

### Secondary
- **Verdict Red** (#c81e3a, dark #ff6b81): failed verdicts, failed checks, the stopped race fill, error titles and alerts, the "Warning:" label, and the harm count in the landing's problem statement.
- **Verdict Green** (#0a7f4f, dark #3ddc97): passing verdicts, passing checks and the measured number on a green result.

### Neutral
- **Cool Paper** (#f6f6f7, dark #0d0e11): page ground, and ticket row hover.
- **Near Black Ink** (#0e0f12, dark #f2f3f5): primary text, the measured marker, the race cap, and the measured number while it counts.
- **Quiet Slate** (#4a4d55, dark #a9adb7): supporting text, labels, inactive attempts, ghost button stroke, input stroke, scrollbar.
- **Card White** (#ffffff, dark #17191e): cards, the pressed attempt, the input, and the halo around the marker.
- **Hairline** (#e3e4e8, dark #2a2d34): dividers, track backgrounds, disabled buttons, skeleton bones.
- **Code Well** (#f0f1f3, dark #17191e): SQL and JSON blocks only.

### Named Rules
**The One Blue Rule.** Blue is the only accent. There is no secondary brand color and no gradient.

**The Earned Color Rule.** Red and green report an outcome or a harm that really happened. They are never decoration, never a category, and never a hover.

**The Withheld Verdict Rule.** While a number is counting it stays ink and the race fill stays blue. Red or green arrives when the measurement has landed, in the same beat as the verdict.

## Typography

**Display Font:** Cabinet Grotesk (with system-ui, sans-serif), weights 500, 700, 800, from Fontshare
**Body Font:** Cabinet Grotesk (same stack)
**Label/Mono Font:** ui-monospace stack, for code and SQL only

**Character:** One confident grotesk doing everything, separated by scale and weight rather than by pairing. Display sizes are tight and heavy; reading text stays at a medium weight and is never small.

### Hierarchy
- **Display** (800, clamp 60 to 72px, 1): the hero promise line. The largest type on any screen.
- **Headline** (800, clamp 36 to 48px, 1.05): every landing section statement. The verb ladder uses Statement (800, clamp 26 to 36px, 1.1), and prices use Numeral.
- **Title** (800, clamp 36 to 48px, 1): app page titles. A run page uses **Run Title** (clamp 26 to 36px) so the numerals stay the largest thing on screen.
- **Numeral** (800, clamp 48 to 56px, 1, tabular): promised and measured numbers, prices. **Numeral Small** (800, clamp 26 to 36px, 1.1, tabular) carries money lines; checksums sit at 28px.
- **Verdict** (700, clamp 22 to 26px, 1.2): the one sentence that states the result.
- **Card Title** (700, clamp 22 to 26px): card headings, ticket subjects, form labels.
- **Label Large** (700, clamp 20 to 22px, 1.2): the lookup line, disclosure summaries, and headings inside disclosures.
- **Sub** (500, clamp 22 to 30px, 1.25): one supporting sentence, 26ch to 34ch wide.
- **Body** (500, clamp 20 to 26px, 1.35): default text.
- **Body Small** (500, clamp 18 to 22px): check detail, table cells, race label.
- **Label** (500, 18px): the floor. Nav notes, ledes, metadata, tick values, ticket requesters.
- **Button** (700, 24px, 1.2): call to action text.
- **Code** (500, 16px, 1.5): SQL and JSON, wrapped, never scrolled sideways.

### Named Rules
**The 18 Floor Rule.** Nothing is set below 18px. The single exception is code and SQL at 16px.

**The No Eyebrow Rule.** No small label sits above a heading, and no text is set uppercase or letter-spaced open. Letter spacing only ever tightens.

**The Few Words Rule.** A verdict is a sentence, a section is one idea, and copy uses plain hyphens and full stops. No em-dashes and no en-dashes anywhere, ranges included.

## Layout

The landing uses a 1280px container and the app a 1000px one, both centered with the fluid gutter. The landing's first view sits under a 72px nav, with promise and proof card in two equal columns (gap clamp 32 to 64px) and the next section visible below them. Later sections use two equal columns or one full-width card, 64 to 96px apart, and hold one idea each.

The single breakpoint is 767px. Below it the hero and split sections collapse to one column, numerals in the stopped card stack, tables turn into label and value rows, and buttons in a row go full width. The page never scrolls sideways; long values wrap.

### Named Rules
**The One Column Rule.** The app is one column of attention: no sidebars, no second panel. Promise and measurement may stand side by side inside a card, because that comparison is the content.

**The One Focal Point Rule.** Each screen has one thing to look at first. On a run page that is the reading; on the inbox it is the ticket list.

## Elevation & Depth

A hybrid of tonal layering and one soft lift. The page is flat; cards rise off it with a wide, low, tinted shadow that reads as light rather than as an edge. Inside a card, depth stops and hairlines do the separating.

### Shadow Vocabulary
- **Card lift** (`box-shadow: 0 30px 70px -20px var(--sh)`, tint rgba(20,30,70,.14), dark rgba(0,0,0,.5)): proof cards and the ticket list.
- **Pressed lift** (`box-shadow: 0 8px 24px -10px var(--sh)`): the selected attempt only.
- **Marker halo** (`box-shadow: 0 0 0 3px var(--card)`): lifts the measured marker off the band it lands on.
- **Inset stroke** (`box-shadow: inset 0 0 0 1.5px var(--mute)`, 1px on the input): the outline of ghost buttons and the input, which carry no outer shadow.

### Named Rules
**The Shadow Or Stroke Rule.** A surface gets a shadow or a stroke, never both. Interior hairlines inside a card are dividers and are fine.

## Shapes

One gently soft corner (16px) on everything that has a corner: cards, buttons, tracks, bands, code blocks, inputs, focus rings, and even the clip used by the race animation. Elements thinner than 32px simply resolve to pills under the same value. The only drawn icon is the disclosure plus, two 20 by 3px bars where one rotates flat when open.

### Named Rules
**The Sixteen Rule.** There is one radius, and it is 16px. Hierarchy never comes from a tighter or looser corner.

## Components

Components are tactile and plain. They are real product parts, reused between the landing and the app.

### Buttons
- **Shape:** gently soft corners (16px), 18px by 32px padding.
- **Primary:** Vultr Blue with On Accent text. One per decision.
- **Hover / Focus:** lifts 2px on hover-capable pointers only; presses to 1px down at 98% scale; 3px blue focus ring, offset 3px. Transform runs 0.2s on the signature curve.
- **Ghost:** transparent with an inset Quiet Slate stroke and ink text, for the secondary action beside a primary.
- **Disabled:** Hairline fill with Quiet Slate text. A locked Approve stays visible and disabled, with a sentence above it saying why.

### Chips
- **Style:** attempt toggles are bare 20px bold Quiet Slate text, 44px minimum height.
- **State:** the pressed attempt gets Card White, the pressed lift, and its own red or green text.

### Cards / Containers
- **Corner Style:** 16px.
- **Background:** Card White.
- **Shadow Strategy:** card lift, no border.
- **Border:** none outside. A hairline sits above the verdict.
- **Internal Padding:** fluid, clamp 24 to 44px.

### Inputs / Fields
- **Style:** Card White, 1px inset Quiet Slate stroke, 16px corners, 18px by 22px padding, body type. Placeholder is full-opacity Quiet Slate.
- **Focus:** the shared 3px blue ring.
- **Error / Disabled:** the action button disables while the field is empty; errors appear as a red alert line below.

### Navigation
A 72px row: the wordmark at 28px and 800 on the left, an 18px Quiet Slate note on the right that always says the data is synthetic. In fixture mode that note turns bold Vultr Blue and says it is not live. Inside a run, a bold underlined blue Inbox link follows the wordmark, 44px minimum height. Mobile keeps the same row and drops the extra words of the fixture note.

### Proof Card (signature)
The reading itself, in this order: lookup line, promised and measured numerals, range track, verdict, then facts such as who is now violating and money. A failed attempt shown on screen never offers Approve. The card holds red or green only through the measured number and the verdict.

### Range Track (signature)
A 22px Hairline track where the promise is a blue band filling the middle 50% and the measurement is a 6px ink marker with a card halo, with the promised bounds as tick values under it. Landing inside or outside the band reads at any scale. Far outside values clamp to the end.

### Stopped Card (signature)
A 32px track with a 4px ink cap at 74%, the cap's name below, and the elapsed time at numeral scale. The fill races blue and turns red the moment it stops. Then "Red. Stopped." with the container and live data facts.

### Ticket List
One lifted card holding rows split by hairlines: subject at card title scale, requester at 18px, a bold blue Open on the right. Hover tints the row Cool Paper and nudges Open 4px to the right.

### Disclosures
Native `details`, separated by hairlines, with a Label Large summary and the drawn plus. Checks, changed rows, the agent's trace, containment and the ticket body all live here. Code blocks sit in the Code Well inside them.

### Motion Moments
GSAP 3.12.5 loads from cdnjs with SRI and a 4s timeout. If it does not arrive, or if reduced motion is on, the page stays on its final state. The signature curve is `cubic-bezier(.16,1,.3,1)`, written `expo.out` in GSAP, used for every arrival.

- **Hero red to green:** CSS keyframes rather than GSAP, so it runs without the CDN. One pass of 3.7s (WCAG 2.2.2: motion ends inside 5s, no pause control needed): red holds 3.2s, the old state leaves up 12px and the new one rises 12px in sequence, never overlapping. It stays on green; reduced motion shows green only.
- **E11 race and dead stop:** runs for the run's elapsed_ms (a sample value of 1,043 ms in fixtures, inside the 1,032 to 1,122 ms laptop gate) with `power2.in` on both fill and counter, and no ease-out, because a cap does not decelerate. Facts reveal 0.12s after. On the landing it plays once, when the card's centre reaches 60% of the viewport, counts in seconds from zero, and is skipped if GSAP arrives after the card was already on screen.
- **Number, track, verdict:** the number counts for 0.9s while the marker slides with it (`power2.out`), the verdict color lands, and only then do verdict and facts rise in over 0.4s with a 0.06s stagger.
- **E8 red replaced by green:** the red holds 3.2s, leaves up 12px in 0.2s, and the green reading counts on from the red's number. Choosing an attempt by hand stops the sequence.
- **Apply and undo:** the new checksum flips up from below its clipped line in 0.45s while the block enters over 0.35s.
- **Verb ladder:** each landing verb is scrubbed from Quiet Slate to ink as it passes, the last to blue, through a `--t` custom property mixed in CSS so the colours stay theme tokens.

### Named Rules
**The Final State Rule.** The default render is always the finished result. Animation only ever replays it, so nothing is hidden behind a script that may not load.

**The Reading First Rule.** The number arrives before the verdict, on every surface and in every state that animates. Never reverse that order or play them together.

**The Native Disclosure Rule.** The default view shows the verdict and the key numbers. Everything else goes behind native `details`, not tabs, modals or custom accordions.

## Do's and Don'ts

### Do:
- **Do** set promised and measured numbers at numeral scale with tabular figures, larger than any heading on a run page.
- **Do** keep every radius at 16px, including focus rings and animation clips.
- **Do** take every new color from the token set, and define it in both the light and the dark sets.
- **Do** use the signature curve for arrivals, and a straight `power2.in` for the race, which must stop dead.
- **Do** keep the final state as the default render, so a blocked CDN or reduced motion loses nothing but the animation.
- **Do** keep hit targets at 44px or more on small controls, and keep the 3px blue focus ring on everything interactive.

### Don't:
- **Don't** set any text below 18px, except code and SQL at 16px.
- **Don't** use eyebrows, uppercase labels or open letter spacing.
- **Don't** use em-dashes or en-dashes, in copy or in number ranges.
- **Don't** put a stroke and an outer shadow on the same surface.
- **Don't** add a second accent, or use red or green for anything but an outcome or a harm.
- **Don't** add a second column, sidebar or panel to the app.
- **Don't** add tabs, modals or custom accordions where native `details` works.
- **Don't** show a verdict before its number has landed.
- **Don't** return to dark with one orange accent, warm paper with rubber stamps and ticket tabs, or a slate dashboard.
