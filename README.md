# Self-Improving Growth Agent

A closed loop that improves a real website on its own, and proves the improvement
with a live A/B test rather than asserting it.

```
 logs ──► analyse ──► hypotheses ──► code ──► eval gate ──► A/B (2 live instances)
                                                                    │
        ◄──────── adopt · await approval · roll back ◄── decide ◄────┘
```

Every number in this repository comes from a real browser session against a real
server. Nothing is a mock, and no curve is hardcoded.

---

## What it actually does

`app/` is **Nimbus Supply**, a small e-commerce checkout funnel
(`landing → product → cart → signup → shipping → payment → confirmation`). It ships
with three defects, planted the way real defects occur — each one looked reasonable
to somebody:

| | defect | how it shows up in telemetry |
|---|---|---|
| **D1** | signup asks for nine fields before you may pay | `abandon_reason: too_many_fields` at `signup` |
| **D2** | checkout is gated behind account creation, no guest path | `abandon_reason: forced_signup` at `signup` |
| **D3** | the pinned mobile total bar covers the pay button | `click_blocked` / `click_intercepted` at `payment`, mobile only |

Each round the agent reads a **summary** of the previous round's logs, proposes
several hypotheses, implements exactly one, and has to clear an eval gate before a
single simulated customer sees it. Then both versions run side by side and the
statistics decide.

**Measured baseline: 26.3% conversion** over 80 sessions (holdout 20.0%), with 65%
of shoppers lost at `signup` and 25% of the survivors lost at `payment`.

---

## Quick start

```bash
pip install flask playwright && playwright install chromium

# round 0 measures the untouched baseline; rounds 1..3 each improve it
python orchestrator/run_loop.py --rounds 3 --n 80 --seed 7

# watch it happen
python dashboard/server.py        # http://127.0.0.1:8080
```

Or let it decide how many rounds it needs:

```bash
python orchestrator/run_loop.py --target 0.55 --max-rounds 6 --n 80 --seed 7
```

Rounds then stop being a budget to spend and become attempts at a number: the loop
keeps proposing, shipping and measuring until the baseline conversion clears the
target, and stops at `--max-rounds` if it cannot — because a loop that cannot reach
its target needs a human to hear about it rather than to keep burning.

Useful flags: `--round N` (one round only), `--rounds N` (a fixed count), `--n`
(sessions per arm), `--auto-approve` (stand in for a human clicking Approve on an
unattended run), `--reset` (clear `data/` and restore `app/` from git).

The seed is fixed, so a run reproduces exactly — same personas, same order, same
assignment. That is a property worth stating out loud rather than hiding: it is what
makes a regression in this loop debuggable.

---

## Layout

```
app/                      the site under improvement — the ONLY tree the agent may edit
candidate/                the agent's working copy, served on :8001 (generated)
personas/personas.json    four rule-based personas, train mix + holdout
sim/simulator.py          Playwright sessions; the sole writer of logs
sim/smoke.py              the eval gate that stands between "code written" and "traffic"
analyzer/analyze.py       funnel, per-persona split, z-test, guardrails, the decision
agent/agent.py            the improvement loop: summary in, one code change out
agent/policy.json         auto-adopt allowlist / approval paths / protected paths
orchestrator/run_loop.py  chains one round together and owns all state
dashboard/                static page + tiny API, polls data/state.json every 2s
data/                     logs, metrics, analysis, per-round artifacts, state.json
```

---

## The five design decisions that matter

**1. A/B is two whole instances, not a feature flag.** Baseline serves `app/` on
:8000, candidate serves `candidate/` on :8001. The agent edits one copy of one tree
and never reasons about flag plumbing, and "roll back" is discarding a directory.

**2. The simulator writes the logs, not the app.** If the app emitted its own
telemetry, an agent editing the app could break instrumentation — or quietly improve
its own numbers. Moving the writer outside the editable tree makes that impossible
rather than merely discouraged.

**3. The agent cannot touch its own evaluator.** `write_file` refuses any path
outside `candidate/`, and the analyzer independently re-checks the round's diff
against `protected_paths` and marks the round `invalid` if it finds a hit. Two
enforcement points, two processes. Verified against `../analyzer/analyze.py`,
`../personas/personas.json`, `../../etc/passwd` and `/tmp`.

**4. The decision is not the model's to make.** The analyzer computes it, in code:

```
smoke failed or protected path touched  ->  invalid    (roll back)
http error rate rose > 2pp              ->  rollback
lift > 0 and p < 0.10                   ->  adopt, or pending_approval per policy
otherwise                               ->  rollback
```

An agent that could influence its own scoring would have no credibility. The model
chooses *what to try*; arithmetic decides *what survives*.

**5. One hypothesis per round.** The agent proposes three to five and implements the
top one. Attribution stays clean: a lift belongs to a single named change, not a
bundle.

---

## Autonomy: earned per change, not granted once

`agent/policy.json` decides how much rope each change gets, from the diff itself:

- diff confined to `app/templates/**` or `app/static/**`, ≤6 files, ≤150 lines
  → **auto-adopt**, no human involved
- touches `app/server.py`, `app/payment*`, `app/auth*`, or exceeds the limits
  → **pending_approval**, with Approve / Reject buttons in the dashboard
- touches `sim/`, `analyzer/`, `personas/`, `agent/`, `data/`, `orchestrator/`
  → **invalid**, rolled back on principle

So the same loop ships a CSS fix unattended and stops for a human on an auth change.
The third rule has never fired in a real round, and that is the point: it is the
mechanism that would catch an agent editing its own scorer, and there is a test for
it precisely because it should never fire by accident.

One deliberate addition to the frozen policy: `app/server.py` is listed under
`needs_approval_paths` even though the auto-adopt allowlist already routes it to a
human (it is neither `templates/**` nor `static/**`). The verdict is identical either
way — verified against the spec-literal policy — so this only changes the wording of
the reason the dashboard shows, from "outside the auto-adopt allowlist" to "touches
app/server.py".

---

## Statistics

Two-proportion z-test on the conversion rate (pooled variance for the test, Wald
interval on the absolute difference), with the round's funnel derived from the steps
that actually appear in each arm's logs — never a fixed list, because the agent is
allowed to delete a funnel step and a hardcoded funnel would silently mis-attribute
that round.

At `n=80` per arm the design detects roughly a 15pp move at `p≈0.05`. An
underpowered win is rolled back, not shipped: at `n=16` a genuine +18.8pp came back
`p=0.238` and the analyzer correctly refused it.

**Holdout personas** (`senior_tablet`) never appear in the agent's summary and never
influence a decision. They are reported separately on the dashboard as an
overfitting check — if adopted rounds stop moving the holdout, the loop is learning
the training mix rather than fixing the site.

---

## On tooling: why this is self-contained

The obvious reach is for a hosted experimentation platform (GrowthBook, Statsig,
Unleash) and a hosted eval harness (promptfoo, Braintrust, Inspect). For this loop
they were the wrong trade, for three specific reasons:

- **The decision must be local and deterministic.** The gate is four lines of
  arithmetic that has to run identically on every machine and in CI. A network call
  in that position adds a failure mode and buys nothing — the estimator here is the
  same two-proportion z-test GrowthBook's frequentist engine uses for a binomial
  metric, so a number from this analyzer is directly comparable to one from a
  GrowthBook deployment.
- **The agent has no network by design.** Its tools are `read_file`, `write_file`,
  `list_dir`, `run_smoke`. Adding an HTTP client to reach an experiment API would
  punch a hole in the sandbox that makes decision 3 above provable.
- **Reproducibility.** A fixed seed has to reproduce a run exactly. Server-side
  bucketing in a hosted platform is not reproducible from a seed.

What the hosted tools are genuinely better at — long-running experiments across real
deployments, sequential testing under continuous peeking, org-wide metric governance
— is exactly what a two-hour loop against a simulated population does not need.
`sim/smoke.py` keeps a promptfoo-shaped case list (one named case, one assertion, one
verdict per row) so the gate can be lifted into `promptfoo eval` against a real
deployment when there is one, and `analyzer/analyze.py` is the natural seam to swap
for a GrowthBook or Statsig readout if this ever pointed at real traffic.

---

## Where Claude fits

With `ANTHROPIC_API_KEY` set, Claude ranks the candidate hypotheses and writes each
round's rationale (30s timeout, one retry, then fall back). The edits themselves come
from a playbook of parameterised, smoke-tested transforms rather than free-form
generated diffs.

That is a deliberate trade, and worth being explicit about: in a loop whose whole
claim is that it cannot break its own evaluation harness, a free-form patch is the
one thing that could. The model picks *what* to fix from evidence; the playbook
guarantees the edit is well-formed. Without a key, ranking falls back to friction
volume — which is the same order the model picks anyway, because the evidence is not
ambiguous.

---

## What the dashboard shows

- **Conversion curve** by round, control vs candidate, each round labelled with its
  relative lift, adopted rounds banded green and pending ones amber, with the holdout
  numbers on their own row underneath.
- **Where customers are lost** — the friction ranking straight from the logs, by
  distinct sessions rather than raw events, so a retry loop does not outrank a wall
  of shoppers leaving.
- **Every round**: the full hypothesis table (what was proposed, what was
  implemented, what was deferred and why), the eval gate case by case, guardrail
  chips, before/after screenshots at both viewports, and `diff.patch` and `change.md`
  inline.
- **Approve / Reject** for any round the policy reserved for a human.

---

## Tests

```bash
python analyzer/test_stats.py
```

20 checks over the arithmetic that decides things: the z-test (including the
interface document's own claim that 30% vs 45% at n=80 lands at p≈0.05), the
degenerate cases, and policy classification from real diff text — including that a
diff editing `analyzer/analyze.py` comes back `invalid`. That last one is what makes
"the agent cannot edit its own scorer" a test rather than a claim.

---

## Reproducing the headline

```bash
python orchestrator/run_loop.py --rounds 3 --n 80 --seed 7 --auto-approve --reset
python dashboard/server.py
```

Round 0 establishes the baseline. Each later round should pick the largest remaining
loss in the funnel — which, given the three planted defects, means the form, then the
forced account, then the mobile tap target.
