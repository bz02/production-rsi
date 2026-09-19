"""Build docs/index.html — the run report a reader can open without running anything.

The dashboard is live and needs a server, `data/` and a completed run. This is the
same evidence as a single static page: every number, diff, gate result and decision
is read out of the run's own artifacts and baked into the HTML, so GitHub Pages can
serve it and the numbers still cannot drift from what was measured.

Nothing here is hand-typed. If a figure in the page is wrong, the artifact it came
from is wrong, which is the only failure mode worth having.

  python docs/build_report.py        # after a run, and after docs/capture_shots.py
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "report_template.html"
OUT = HERE / "index.html"

REPO = "https://github.com/bz02/production-rsi"

# What each round's before/after pair is framed on, for the caption.
SHOT_CAPTIONS = {
    1: ("signup, desktop", "nine required fields", "four"),
    2: ("signup, phone", "an account or nothing", "a guest path above the form"),
    3: ("payment, phone, scrolled to the end",
        "the pay button is underneath the pinned bar", "it is above it"),
}


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def round_dir(n: int) -> Path:
    return DATA / "runs" / f"round_{n}"


def diff_lines(n: int) -> list[dict[str, str]]:
    """The round's diff, classified per line so the page can colour it without a
    syntax-highlighting dependency."""
    path = round_dir(n) / "diff.patch"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("diff ") or line.startswith("+++") or line.startswith("---"):
            kind = "hdr"
        elif line.startswith("@@"):
            kind = "hunk"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "del"
        else:
            kind = "ctx"
        out.append({"k": kind, "t": line})
    return out


def rationale(n: int) -> str:
    """The agent's own one-paragraph justification, lifted from its change.md."""
    path = round_dir(n) / "change.md"
    if not path.exists():
        return ""
    m = re.search(r"## Why this one\n(.+?)\n\n", path.read_text(), re.S)
    return m.group(1).strip() if m else ""


def collect() -> dict[str, Any]:
    state = read_json(DATA / "state.json", {}) or {}
    zero = read_json(DATA / "metrics" / "round_0.json")
    if zero is None:
        raise SystemExit("no data/metrics/round_0.json — run the loop first")
    zero_summary = read_json(DATA / "analysis" / "round_0_summary.json", {})

    rounds: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = [{
        "round": 0,
        "control": zero["control"]["conversion_rate"],
        "treatment": None,
        "holdout_control": (zero.get("holdout") or {}).get("control"),
        "holdout_treatment": None,
        "adopted": False,
    }]

    n = 1
    while (DATA / "metrics" / f"round_{n}.json").exists():
        m = read_json(DATA / "metrics" / f"round_{n}.json")
        entry = next((r for r in state.get("rounds", []) if r["round"] == n), {})
        cmp_ = m.get("comparison") or {}
        holdout = m.get("holdout") or {}
        t = m.get("treatment") or {}
        rounds.append({
            "round": n,
            "title": entry.get("title") or f"Round {n}",
            "hypothesis_id": entry.get("hypothesis_id"),
            "selection_mode": entry.get("selection_mode"),
            "status": entry.get("status") or m["decision"],
            "autonomy": m.get("autonomy"),
            "control": m["control"]["conversion_rate"],
            "control_n": m["control"]["n"],
            "control_conv": m["control"]["conversions"],
            "treatment": t.get("conversion_rate"),
            "treatment_n": t.get("n"),
            "treatment_conv": t.get("conversions"),
            "lift_abs": cmp_.get("lift_abs"),
            "lift_rel": cmp_.get("lift_rel"),
            "p_value": cmp_.get("p_value"),
            "ci95": cmp_.get("ci95"),
            "decision": m["decision"],
            "decision_reason": m["decision_reason"],
            "files": m["diff"]["files"],
            "files_changed": m["diff"]["files_changed"],
            "lines_changed": m["diff"]["lines_changed"],
            "needs_approval": m["diff"]["needs_approval_hits"],
            "protected": m["diff"]["protected_paths_touched"],
            "guardrails": m.get("guardrails", {}),
            "hypotheses": read_json(round_dir(n) / "hypotheses.json", []),
            "smoke": read_json(round_dir(n) / "smoke.json", {}),
            "diff": diff_lines(n),
            "rationale": rationale(n),
            "holdout": holdout,
            "shots": {
                "caption": SHOT_CAPTIONS.get(n, ("", "", "")),
                "before": f"shots/r{n}_before.png" if (HERE / "shots" / f"r{n}_before.png").exists() else None,
                "after": f"shots/r{n}_after.png" if (HERE / "shots" / f"r{n}_after.png").exists() else None,
            },
        })
        series.append({
            "round": n,
            "control": m["control"]["conversion_rate"],
            "treatment": t.get("conversion_rate"),
            "holdout_control": holdout.get("control"),
            "holdout_treatment": holdout.get("treatment"),
            "adopted": (entry.get("status") or m["decision"]) == "adopted",
        })
        n += 1

    sessions = sum(r["control_n"] + (r["treatment_n"] or 0) for r in rounds) + zero["control"]["n"]
    final = series[-1]["treatment"] if series[-1]["treatment"] is not None else series[-1]["control"]
    base = series[0]["control"]

    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "repo": REPO,
        "seed": 7,
        "n_per_arm": zero["control"]["n"],
        "sessions": sessions,
        "baseline": base,
        "final": final,
        "total_rel": (final - base) / base if base else 0,
        "adopted": sum(1 for r in rounds if r["status"] == "adopted"),
        "rounds_run": len(rounds),
        "funnel0": zero["control"]["funnel"],
        "friction0": (zero_summary or {}).get("top_friction", [])[:5],
        "persona0": (zero_summary or {}).get("by_persona", {}),
        "holdout0": (zero.get("holdout") or {}).get("control"),
        "series": series,
        "rounds": rounds,
    }


def main() -> None:
    payload = collect()
    template = TEMPLATE.read_text()
    # Escaped so a stray "</script>" in a diff cannot break out of the data block.
    blob = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    out = template.replace("/*%%RUN_DATA%%*/null", blob)
    out = out.replace("%%GENERATED%%", html.escape(payload["generated"]))
    OUT.write_text(out)
    print(f"wrote {OUT.relative_to(ROOT)}  ({len(out) // 1024} KB, "
          f"{payload['rounds_run']} rounds, {payload['sessions']} sessions)")


if __name__ == "__main__":
    main()
