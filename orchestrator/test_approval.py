"""Checks on the human decision. Run with: python orchestrator/test_approval.py

`pending_approval` is the whole point of the policy: the loop ships a CSS fix on its
own and stops for a person on a route change. Stopping is the easy half. The half
that matters is what the Approve button then does — promote *that* round's candidate,
whole, and record who decided.

Approving twice, approving a round that was rolled back, and approving a round that
does not exist all have to be refused rather than half-applied, because the button
sits in a browser and browsers re-send things.

Everything runs against a temporary data directory and temporary trees; the real
`app/` and `data/` are never touched.
"""

from __future__ import annotations

import json
import shutil
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


def scenario(tmp: Path, status: str = "pending_approval") -> dict[str, Path]:
    """A pending round: an `app/` with the old file, a snapshot with the new one."""
    app = tmp / "app"
    app.mkdir()
    (app / "server.py").write_text("# baseline\n")
    (app / "keep.txt").write_text("still here\n")

    snapshot = tmp / "data" / "runs" / "round_1" / "candidate_snapshot"
    snapshot.mkdir(parents=True)
    (snapshot / "server.py").write_text("# baseline\n@app.route('/guest')\n")
    (snapshot / "keep.txt").write_text("still here\n")

    state = {
        "current_round": 1,
        "phase": "decided",
        "conversion_series": [{"round": 1, "control": 0.35, "treatment": 0.53,
                               "adopted": False, "pending": True}],
        "rounds": [{"round": 1, "title": "Add a guest checkout path", "status": status,
                    "autonomy": "pending_approval"}],
        "timeline": [],
    }
    (tmp / "data").mkdir(exist_ok=True)
    (tmp / "data" / "state.json").write_text(json.dumps(state))

    # Point the orchestrator at the temporary world. CANDIDATE is deliberately absent,
    # so adopt() has to fall back to the round's snapshot — which is the real case
    # when a human approves hours later, after another round has overwritten it.
    run_loop.APP = app
    run_loop.CANDIDATE = tmp / "candidate-that-is-gone"
    run_loop.DATA = tmp / "data"
    run_loop.STATE_PATH = tmp / "data" / "state.json"
    return {"app": app, "snapshot": snapshot}


print("approve")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    paths = scenario(tmp)
    result = run_loop.apply_approval(1, approve=True)
    state = json.loads((tmp / "data" / "state.json").read_text())
    entry = state["rounds"][0]

    check("approving a pending round succeeds", result.get("ok") is True, str(result))
    check("the candidate is promoted into app/",
          "/guest" in (paths["app"] / "server.py").read_text(),
          (paths["app"] / "server.py").read_text())
    check("the rest of the tree survives the whole-tree replace",
          (paths["app"] / "keep.txt").exists())
    check("a backup of the pre-adopt app is kept",
          (tmp / "data" / "runs" / "round_1" / "app_before_adopt" / "server.py").exists())
    check("status becomes adopted", entry["status"] == "adopted", entry["status"])
    check("and records that a human decided", entry["autonomy"] == "human_approved", entry["autonomy"])
    check("the chart series stops saying pending",
          state["conversion_series"][0]["adopted"] is True
          and state["conversion_series"][0]["pending"] is False,
          str(state["conversion_series"][0]))
    check("the decision is written to the timeline",
          any("approved by a human" in t["msg"] for t in state["timeline"]), str(state["timeline"]))

    # The button lives in a browser, so the second click has to be inert.
    again = run_loop.apply_approval(1, approve=True)
    check("approving the same round twice is refused", again.get("ok") is False, str(again))
    check("and says what state it is actually in", "adopted" in again.get("error", ""), str(again))


print("\nreject")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    paths = scenario(tmp)
    result = run_loop.apply_approval(1, approve=False)
    state = json.loads((tmp / "data" / "state.json").read_text())
    entry = state["rounds"][0]

    check("rejecting a pending round succeeds", result.get("ok") is True, str(result))
    check("app/ is left exactly as it was",
          (paths["app"] / "server.py").read_text() == "# baseline\n",
          (paths["app"] / "server.py").read_text())
    check("status becomes rolled back", entry["status"] == "rolled_back", entry["status"])
    check("and records that a human rejected it", entry["autonomy"] == "human_rejected", entry["autonomy"])
    check("the chart series stops saying pending",
          state["conversion_series"][0]["adopted"] is False
          and state["conversion_series"][0]["pending"] is False,
          str(state["conversion_series"][0]))

    after = run_loop.apply_approval(1, approve=True)
    check("a rejected round cannot then be approved", after.get("ok") is False, str(after))


print("\nrounds that are not waiting on anyone")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    scenario(tmp, status="adopted")
    check("an auto-adopted round is not approvable",
          run_loop.apply_approval(1, approve=True).get("ok") is False)
    check("a round that does not exist is refused",
          run_loop.apply_approval(7, approve=True).get("ok") is False)


print()
if failures:
    print(f"{len(failures)} check(s) failed")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all checks passed")
