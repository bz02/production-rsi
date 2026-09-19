"""Checks on --target mode. Run with: python orchestrator/test_target.py

`--target 0.55` is the shape the loop is meant to have: rounds are not a budget to
spend but attempts at a number. That makes the stopping conditions load-bearing, and
both of them are easy to get subtly wrong —

  stop too early   and a run that has not converged reports success
  stop too late    and a loop that cannot reach its target burns rounds forever

The rounds themselves are stubbed here: what is under test is the arithmetic of
"where is the baseline now" and "should there be another round", not Playwright. A
stubbed round takes microseconds, so every path gets exercised, including the one
where nothing ever improves.

`current_baseline` is the subtle one. The baseline is not the last control measured —
it is the last *adopted* treatment, because that is what is now serving. A round that
was rolled back leaves the baseline where it was.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator import run_loop  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  pass  {name}")
    else:
        failures.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


def fresh_state() -> dict:
    return {"current_round": 0, "phase": "idle", "conversion_series": [], "rounds": [], "timeline": []}


class StubbedRounds:
    """Stands in for the browser work: each round moves the baseline by `step`,
    or not at all when `step` is 0, and records that it ran."""

    def __init__(self, start: float, step: float, adopt: bool = True) -> None:
        self.start = start
        self.step = step
        self.adopt = adopt
        self.rounds_run: list[int] = []

    def zero(self, _n: int, _seed: int, state: dict) -> dict:
        run_loop.upsert_series(state, {"round": 0, "control": self.start})
        return {}

    def round(self, round_no: int, _n: int, _seed: int, state: dict, _auto: bool) -> dict:
        self.rounds_run.append(round_no)
        control = run_loop.current_baseline(state) or self.start
        treatment = control + self.step
        run_loop.upsert_series(state, {"round": round_no, "control": control,
                                       "treatment": treatment, "adopted": self.adopt,
                                       "pending": False})
        return {}


def run_target(stub: StubbedRounds, target: float, max_rounds: int) -> dict:
    zero, rnd = run_loop.run_round_zero, run_loop.run_round
    run_loop.run_round_zero, run_loop.run_round = stub.zero, stub.round
    try:
        return run_loop.run_until_target(target, max_rounds, n=8, seed=7,
                                         state=fresh_state(), auto_approve=True)
    finally:
        run_loop.run_round_zero, run_loop.run_round = zero, rnd


# Keep the stubs' state writes off the real data directory.
_tmp = tempfile.TemporaryDirectory()
run_loop.STATE_PATH = Path(_tmp.name) / "state.json"
run_loop.DATA = Path(_tmp.name)


print("current_baseline")

s = fresh_state()
check("no rounds means no baseline", run_loop.current_baseline(s) is None)

run_loop.upsert_series(s, {"round": 0, "control": 0.285})
check("round 0 alone gives the measured control", run_loop.current_baseline(s) == 0.285,
      str(run_loop.current_baseline(s)))

run_loop.upsert_series(s, {"round": 1, "control": 0.30, "treatment": 0.41, "adopted": True})
check("an adopted treatment becomes the baseline", run_loop.current_baseline(s) == 0.41,
      str(run_loop.current_baseline(s)))

run_loop.upsert_series(s, {"round": 2, "control": 0.41, "treatment": 0.60, "adopted": False})
check("a rolled-back round leaves the baseline alone", run_loop.current_baseline(s) == 0.41,
      str(run_loop.current_baseline(s)))

run_loop.upsert_series(s, {"round": 3, "control": 0.41, "treatment": 0.52, "pending": True})
check("a round still awaiting a human does not count either",
      run_loop.current_baseline(s) == 0.41, str(run_loop.current_baseline(s)))


print("\nstopping conditions")

stub = StubbedRounds(start=0.30, step=0.10)
out = run_target(stub, target=0.55, max_rounds=6)
check("it stops as soon as the target is cleared", out["reached"] is True, str(out))
check("and runs only the rounds it needed", stub.rounds_run == [1, 2, 3], str(stub.rounds_run))
check("and reports where it finished", out["final"] >= 0.55, str(out))

stub = StubbedRounds(start=0.30, step=0.0)
out = run_target(stub, target=0.90, max_rounds=3)
check("a loop that cannot converge stops at max-rounds", out["reached"] is False, str(out))
check("and does not exceed the limit", stub.rounds_run == [1, 2, 3], str(stub.rounds_run))

stub = StubbedRounds(start=0.62, step=0.10)
out = run_target(stub, target=0.55, max_rounds=6)
check("a baseline already past the target runs no rounds at all",
      out["reached"] is True and stub.rounds_run == [], str(stub.rounds_run))

# Rounds that never adopt must not be mistaken for progress.
stub = StubbedRounds(start=0.30, step=0.30, adopt=False)
out = run_target(stub, target=0.55, max_rounds=2)
check("rolled-back rounds do not count towards the target", out["reached"] is False, str(out))
check("and the baseline stays where it started", out["final"] == 0.30, str(out))


print()
_tmp.cleanup()
if failures:
    print(f"{len(failures)} check(s) failed")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all checks passed")
