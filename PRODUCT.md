# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
Confirmed by the user: hackathon judges and billing/ops leads count equally.
- Judges (Vultr Agent Rush, Challenge 2 "Powering the Future of Work") watch a video of about 4 minutes, then click the live demo. They score Application of Technology, Presentation, Business Value and Originality.
- Billing and support ops leads at SaaS companies have data-fix tickets ("write off invoices 90+ days overdue") that wait on engineers. The product has to pass as something they would really use.

## Product Purpose
A person picks or types a data-fix ticket. An AI agent writes a change contract (tables, row range, scope rule, checks), then SQL, and runs it on a throwaway copy of the database. DryDock measures what really changed against the contract. On a mismatch the agent revises, at most twice. A person with the right approval class applies it, and it can be undone. Success: a judge can say what it does after one viewing, and an ops lead can see why they would trust it.

## Positioning
Promise first, then proof. The defensible claim is only the combination: ticket, change contract, measured diff on a disposable copy, role-based approval, reset. Never claim "first" or "only" for any single part: Bytebase, Atlas and others cover pieces of this (plan §6.4).

## Operating Context
- Surfaces: a landing page first, then the working demo (user's choice).
- Video order: the billing-ops ticket and problem, slides, demo (E8 red to green, approval, undo), then E11 as proof the caps hold.
- E11: the agent's own query is stopped by a resource cap on its first attempt, which ends the run: the container is destroyed, live data is unchanged, Approve is disabled (class C).
- E8: a month-end write-off fails policy P-7, the agent revises, then it goes green.
- All data is synthetic and must be labelled so.

## Capabilities and Constraints
- Stack (existing): React 18, Vite, Tailwind 4, built locally and served as static files by the FastAPI orchestrator. No npm on the VM.
- The API contract is fixed: `web/src/api.js`, `docs/api.md`.
- Fixture mode (`?fixture`, `?fixture&outage`) must keep working, and samples must never read as a live run.
- No new npm packages without asking the user.
- SQL is the "code" the agent writes. Never claim "no shell": bash is still in the image.

## Brand Commitments
Name: DryDock. No logo or other assets exist.
User rejected three earlier looks: dark with one orange accent; warm paper with rubber stamps and ticket tabs; a slate dashboard. Their stated reasons: generic template, looks cheap or amateur, boring with no impact.

## Evidence on Hand
Real: seed figures in `web/src/fixtures.json` and `seed/golden.json` (E8: $31,319.00 written off, $1,169.18 protected; E11: 588,126,318 synthetic rows, disk spill cap, code 53400); public incidents with sources in plan §6.1 (Atlassian 2022, 883 sites / 775 customers; Circle).
Not on hand, never invent: customers, testimonials, benchmarks, live eval score, deployment claims. Whether a live model run exists is stated in README.md, not here. E11 numbers are laptop-provisional. Pricing ($20 per ops seat + $0.99 per approved change) and market size are hypotheses and must be labelled so.

## Product Principles
1. The promise is written before the work, and the measurement is never the model's word.
2. Nothing touches live data without a person.
3. Show the mechanism working, don't describe it.
4. Honest about what is synthetic, sampled or unproven.

## Accessibility & Inclusion
Keyboard reachable, contrast passing, reduced motion respected, no horizontal page scroll at 390px.
