"""Log analysis, A/B statistics and the decision gate. PROTECTED — not agent-editable.

Two outputs per round:

  data/metrics/round_{N}.json          full A/B result + guardrails + decision
  data/analysis/round_{N}_summary.json the compact summary handed to the agent

The decision is computed here, deterministically, and never delegated to the model.
That separation is the point: the agent proposes, the analyzer judges. An agent that
could influence its own scoring would have no credibility, and the protected-path
check below is the mechanical proof that it cannot.

Funnels are derived from the steps that actually appear in each arm's logs rather
than from a fixed list, because the agent is allowed to add or delete funnel steps —
a hardcoded funnel would silently mis-attribute a round that removed `signup`.

Statistics: two-proportion z-test on the conversion rate with a Wald interval on the
absolute difference. This is the same estimator GrowthBook's frequentist engine uses
for a binomial metric, so a number here is comparable to one from a GrowthBook
deployment; it is implemented locally to keep the loop hermetic and reproducible
under a fixed seed.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PERSONAS_PATH = ROOT / "personas" / "personas.json"

# p-value below which a positive lift is considered real enough to act on.
P_THRESHOLD = 0.10
# a guardrail: how much the HTTP error rate may rise before the round is rolled back
HTTP_ERROR_TOLERANCE_PP = 0.02


# --------------------------------------------------------------------- statistics

def _norm_sf(z: float) -> float:
    """Upper-tail standard normal survival function."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def two_proportion_z(c_conv: int, c_n: int, t_conv: int, t_n: int) -> dict[str, Any]:
    """Two-sided two-proportion z-test plus a Wald CI on the absolute difference."""
    if c_n == 0 or t_n == 0:
        return {"lift_abs": 0.0, "lift_rel": 0.0, "p_value": 1.0, "ci95": [0.0, 0.0], "z": 0.0}
    p_c = c_conv / c_n
    p_t = t_conv / t_n
    diff = p_t - p_c

    # Pooled variance for the null hypothesis test.
    pooled = (c_conv + t_conv) / (c_n + t_n)
    se_pooled = math.sqrt(pooled * (1 - pooled) * (1 / c_n + 1 / t_n))
    z = 0.0 if se_pooled == 0 else diff / se_pooled
    p_value = 2 * _norm_sf(abs(z))

    # Unpooled variance for the interval — the standard Wald form.
    se_unpooled = math.sqrt(p_c * (1 - p_c) / c_n + p_t * (1 - p_t) / t_n)
    half = 1.959964 * se_unpooled
    return {
        "lift_abs": round(diff, 4),
        "lift_rel": round(diff / p_c, 4) if p_c > 0 else 0.0,
        "p_value": round(min(1.0, p_value), 4),
        "ci95": [round(diff - half, 4), round(diff + half, 4)],
        "z": round(z, 3),
    }


# ------------------------------------------------------------------ log reduction

def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def persona_devices() -> dict[str, str]:
    cfg = json.loads(PERSONAS_PATH.read_text())
    return {p["id"]: p["device"] for p in cfg["personas"]}


def sessions_of(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        by_session[e.get("session_id", "?")].append(e)
    return by_session


def dynamic_funnel(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Funnel over whatever steps this arm actually logged, ordered by first sighting."""
    order: list[str] = []
    reached: dict[str, set[str]] = defaultdict(set)
    for e in events:
        step = e.get("step") or "unknown"
        if step in ("unknown", None):
            continue
        if e["event"] == "page_view":
            if step not in order:
                order.append(step)
            reached[step].add(e["session_id"])

    dropped_at: Counter[str] = Counter()
    for sid, evs in sessions_of(events).items():
        end = next((e for e in reversed(evs) if e["event"] == "session_end"), None)
        if end and end["meta"].get("outcome") == "abandoned":
            dropped_at[end["meta"].get("last_step") or "unknown"] += 1

    funnel = []
    for step in order:
        n_reached = len(reached[step])
        n_dropped = dropped_at.get(step, 0)
        funnel.append({
            "step": step,
            "reached": n_reached,
            "dropped": n_dropped,
            "drop_rate": round(n_dropped / n_reached, 4) if n_reached else 0.0,
        })
    return funnel


def arm_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_session = sessions_of(events)
    devices = persona_devices()

    ends = []
    for sid, evs in by_session.items():
        end = next((e for e in reversed(evs) if e["event"] == "session_end"), None)
        if end:
            ends.append(end)

    n = len(ends)
    conversions = sum(1 for e in ends if e["meta"].get("outcome") == "converted")
    steps = [e["meta"].get("steps", 0) for e in ends]
    durations = [e["meta"].get("total_ms", 0) / 1000 for e in ends]

    http_error_sessions = {e["session_id"] for e in events if e["event"] == "http_error"}
    blocked_sessions = {e["session_id"] for e in events if e["event"] == "click_blocked"}

    by_persona: dict[str, dict[str, Any]] = {}
    for persona in {e["persona"] for e in ends}:
        p_ends = [e for e in ends if e["persona"] == persona]
        p_conv = sum(1 for e in p_ends if e["meta"].get("outcome") == "converted")
        by_persona[persona] = {
            "n": len(p_ends),
            "conversions": p_conv,
            "conversion_rate": round(p_conv / max(1, len(p_ends)), 4),
            "device": devices.get(persona, "unknown"),
        }

    return {
        "n": n,
        "conversions": conversions,
        "conversion_rate": round(conversions / max(1, n), 4),
        "avg_steps": round(sum(steps) / max(1, len(steps)), 2),
        "avg_duration_s": round(sum(durations) / max(1, len(durations)), 1),
        "http_error_rate": round(len(http_error_sessions) / max(1, n), 4),
        "blocked_click_rate": round(len(blocked_sessions) / max(1, n), 4),
        "funnel": dynamic_funnel(events),
        "by_persona": by_persona,
    }


def top_friction(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ranked friction, by how many distinct sessions each cause ended or damaged.

    Session counts, not event counts: three blocked clicks in one session is one
    frustrated customer, and ranking by raw events would over-weight retry loops."""
    devices = persona_devices()
    abandon_sessions: dict[tuple[str, str], set[str]] = defaultdict(set)
    blocked: dict[tuple[str, str], set[str]] = defaultdict(set)
    validation: dict[str, set[str]] = defaultdict(set)
    field_counts: dict[str, int] = {}

    for e in events:
        step = e.get("step") or "unknown"
        if e["event"] == "abandon":
            reason = e["meta"].get("reason") or "unknown"
            abandon_sessions[(reason, step)].add(e["session_id"])
            if reason == "too_many_fields" and e["meta"].get("field_count"):
                field_counts[step] = max(field_counts.get(step, 0), int(e["meta"]["field_count"]))
        elif e["event"] == "click_blocked":
            device = devices.get(e["persona"], "unknown")
            blocked[(step, device)].add(e["session_id"])
        elif e["event"] == "validation_error":
            validation[step].add(e["session_id"])

    items: list[dict[str, Any]] = []
    for (reason, step), sids in abandon_sessions.items():
        item = {"type": "abandon_reason", "value": reason, "count": len(sids), "step": step}
        if step in field_counts and reason == "too_many_fields":
            item["field_count"] = field_counts[step]
        items.append(item)
    for (step, device), sids in blocked.items():
        items.append({"type": "blocked_click", "value": "click_intercepted", "count": len(sids),
                      "step": step, "device": device})
    for step, sids in validation.items():
        items.append({"type": "validation_error", "value": "field_rejected", "count": len(sids), "step": step})

    items.sort(key=lambda d: d["count"], reverse=True)
    return items[:8]


def sample_traces(events: list[dict[str, Any]], limit: int = 5) -> list[str]:
    """Up to `limit` abandoned sessions, each flattened to one readable line."""
    traces: list[str] = []
    for sid, evs in sessions_of(events).items():
        end = next((e for e in reversed(evs) if e["event"] == "session_end"), None)
        if not end or end["meta"].get("outcome") != "abandoned":
            continue
        parts = []
        for e in evs:
            if e["event"] in ("page_view", "step_complete", "input"):
                continue
            tag = e["event"]
            if tag == "abandon":
                tag = f"abandon({e['meta'].get('reason')})"
            elif tag == "click_blocked":
                tag = f"click_blocked({e.get('element')})"
            parts.append(f"{e.get('step')}:{tag}")
        persona = end["persona"]
        traces.append(f"[{persona}] " + " -> ".join(parts[-8:]) +
                      f" | last_step={end['meta'].get('last_step')} steps={end['meta'].get('steps')}")
        if len(traces) >= limit:
            break
    return traces


# ------------------------------------------------------------------- policy gate

def load_policy() -> dict[str, Any]:
    return json.loads((ROOT / "agent" / "policy.json").read_text())


def changed_files(diff_path: Path) -> list[str]:
    """Paths touched by the round's diff, normalised to repo-relative app paths."""
    if not diff_path.exists():
        return []
    files: set[str] = set()
    for line in diff_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^(?:diff -[a-zA-Z]+ |\+\+\+ |--- )(\S+)", line)
        if not m:
            continue
        path = m.group(1)
        if path in ("/dev/null",):
            continue
        # `diff -ru app candidate` prints paths like `candidate/templates/signup.html`
        path = re.sub(r"^(?:a/|b/)", "", path)
        path = re.sub(r"^candidate/", "app/", path)
        if not path.startswith("app/"):
            path = "app/" + path.split("app/")[-1] if "app/" in path else path
        files.add(path)
    return sorted(files)


def diff_size(diff_path: Path) -> int:
    if not diff_path.exists():
        return 0
    n = 0
    for line in diff_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if (line.startswith("+") and not line.startswith("+++")) or (
            line.startswith("-") and not line.startswith("---")
        ):
            n += 1
    return n


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path, pat.rstrip("*") + "*") for pat in patterns)


def classify_autonomy(diff_path: Path, policy: dict[str, Any]) -> dict[str, Any]:
    """Decide whether a winning change may self-adopt, needs a human, or is invalid."""
    files = changed_files(diff_path)
    lines = diff_size(diff_path)
    protected = [f for f in files if matches_any(f, policy["protected_paths"])]
    needs_approval = [f for f in files if matches_any(f, policy["needs_approval_paths"])]
    allowed = policy["auto_adopt"]["allowed_paths"]
    outside_allowlist = [f for f in files if not matches_any(f, allowed)]
    over_files = len(files) > policy["auto_adopt"]["max_files_changed"]
    over_lines = lines > policy["auto_adopt"]["max_lines_changed"]

    if protected:
        verdict = "invalid"
    elif needs_approval or outside_allowlist or over_files or over_lines:
        verdict = "pending_approval"
    else:
        verdict = "auto"

    return {
        "verdict": verdict,
        "files": files,
        "files_changed": len(files),
        "lines_changed": lines,
        "protected_paths_touched": bool(protected),
        "protected_hits": protected,
        "needs_approval_hits": needs_approval,
        "outside_allowlist": outside_allowlist,
        "over_file_limit": over_files,
        "over_line_limit": over_lines,
    }


# -------------------------------------------------------------------- the round

def analyze_round(round_no: int, smoke_pass: bool = True, diff_path: Path | None = None,
                  data_dir: Path | None = None) -> dict[str, Any]:
    data = data_dir or ROOT / "data"
    logs = data / "logs" / f"round_{round_no}"
    control = read_events(logs / "control.jsonl")
    treatment = read_events(logs / "treatment.jsonl")

    c = arm_metrics(control)
    t = arm_metrics(treatment) if treatment else None

    policy = load_policy()
    autonomy = classify_autonomy(diff_path or (data / "runs" / f"round_{round_no}" / "diff.patch"), policy)

    if t is None:
        # Round 0: baseline only, nothing to decide.
        result: dict[str, Any] = {
            "round": round_no,
            "n_per_arm": c["n"],
            "control": c,
            "treatment": None,
            "comparison": None,
            "guardrails": {"smoke_test": smoke_pass, "http_error_delta": 0.0,
                           "protected_paths_touched": False, "pass": True},
            "decision": "baseline",
            "decision_reason": f"Round 0 baseline only: conversion {c['conversion_rate']:.1%} over {c['n']} sessions.",
            "autonomy": "n/a",
            "diff": autonomy,
        }
    else:
        comparison = two_proportion_z(c["conversions"], c["n"], t["conversions"], t["n"])
        http_delta = round(t["http_error_rate"] - c["http_error_rate"], 4)
        guardrails = {
            "smoke_test": smoke_pass,
            "http_error_delta": http_delta,
            "protected_paths_touched": autonomy["protected_paths_touched"],
            "pass": smoke_pass and not autonomy["protected_paths_touched"] and http_delta <= HTTP_ERROR_TOLERANCE_PP,
        }

        # Decision rules, in this exact order. Nothing here consults a model.
        if not smoke_pass or autonomy["protected_paths_touched"]:
            decision = "invalid"
            why = ("candidate failed the smoke suite" if not smoke_pass
                   else f"candidate touched protected paths: {', '.join(autonomy['protected_hits'])}")
            reason = f"Rolled back as invalid — {why}."
        elif http_delta > HTTP_ERROR_TOLERANCE_PP:
            decision = "rollback"
            reason = (f"Guardrail failed — HTTP error rate rose {http_delta:+.1%}, over the "
                      f"{HTTP_ERROR_TOLERANCE_PP:.0%} tolerance.")
        elif comparison["lift_abs"] > 0 and comparison["p_value"] < P_THRESHOLD:
            decision = "adopt" if autonomy["verdict"] == "auto" else "pending_approval"
            scope = (f"diff touches {autonomy['files_changed']} file(s), {autonomy['lines_changed']} line(s)")
            if decision == "adopt":
                reason = (f"Conversion {comparison['lift_abs']:+.1%} "
                          f"(p={comparison['p_value']:.3f}), guardrails pass, {scope} — inside the auto-adopt policy.")
            else:
                blockers = []
                if autonomy["needs_approval_hits"]:
                    blockers.append("touches " + ", ".join(autonomy["needs_approval_hits"]))
                if autonomy["outside_allowlist"]:
                    blockers.append("outside the auto-adopt allowlist: " + ", ".join(autonomy["outside_allowlist"]))
                if autonomy["over_file_limit"]:
                    blockers.append("over the file limit")
                if autonomy["over_line_limit"]:
                    blockers.append("over the line limit")
                reason = (f"Conversion {comparison['lift_abs']:+.1%} "
                          f"(p={comparison['p_value']:.3f}) and guardrails pass, but human approval is required: "
                          f"{'; '.join(blockers)}.")
        else:
            decision = "rollback"
            if comparison["lift_abs"] <= 0:
                reason = (f"Rolled back — conversion moved {comparison['lift_abs']:+.1%}, "
                          f"which is not an improvement.")
            else:
                reason = (f"Rolled back — conversion {comparison['lift_abs']:+.1%} is not significant "
                          f"(p={comparison['p_value']:.3f} >= {P_THRESHOLD}).")

        result = {
            "round": round_no,
            "n_per_arm": max(c["n"], t["n"]),
            "control": c,
            "treatment": t,
            "comparison": comparison,
            "guardrails": guardrails,
            "decision": decision,
            "decision_reason": reason,
            "autonomy": autonomy["verdict"],
            "diff": autonomy,
        }

    # Holdout personas, reported but never fed to the agent.
    holdout: dict[str, float] = {}
    for arm in ("control", "treatment"):
        h = read_events(logs / f"{arm}.holdout.jsonl")
        if h:
            holdout[arm] = arm_metrics(h)["conversion_rate"]
    if holdout:
        result["holdout"] = holdout

    return result


def build_agent_summary(round_no: int, metrics: dict[str, Any], data_dir: Path | None = None) -> dict[str, Any]:
    """The agent's entire view of the world: a summary, never the raw logs.

    Deliberately excludes the holdout arm and the decision rules, so the agent cannot
    optimise against either."""
    data = data_dir or ROOT / "data"
    logs = data / "logs" / f"round_{round_no}"
    # Summarise the arm that is now the baseline: the adopted treatment if it won,
    # otherwise control.
    adopted = metrics.get("decision") in ("adopt",)
    arm_file = "treatment.jsonl" if adopted else "control.jsonl"
    events = read_events(logs / arm_file)
    if not events:
        events = read_events(logs / "control.jsonl")
    arm = metrics["treatment"] if (adopted and metrics.get("treatment")) else metrics["control"]

    return {
        "round_analyzed": round_no,
        "baseline_conversion": arm["conversion_rate"],
        "baseline_sessions": arm["n"],
        "funnel": arm["funnel"],
        "top_friction": top_friction(events),
        "by_persona": {k: {"n": v["n"], "conversion_rate": v["conversion_rate"], "device": v["device"]}
                       for k, v in arm["by_persona"].items()},
        "sample_traces": sample_traces(events),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze a round's logs into metrics + an agent summary")
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--smoke", choices=["pass", "fail"], default="pass")
    ap.add_argument("--diff")
    args = ap.parse_args()

    diff_path = Path(args.diff) if args.diff else None
    metrics = analyze_round(args.round, smoke_pass=args.smoke == "pass", diff_path=diff_path)
    summary = build_agent_summary(args.round, metrics)

    (ROOT / "data" / "metrics").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "analysis").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "metrics" / f"round_{args.round}.json").write_text(json.dumps(metrics, indent=2))
    (ROOT / "data" / "analysis" / f"round_{args.round}_summary.json").write_text(json.dumps(summary, indent=2))

    print(json.dumps({
        "round": args.round,
        "decision": metrics["decision"],
        "reason": metrics["decision_reason"],
        "control": metrics["control"]["conversion_rate"],
        "treatment": (metrics["treatment"] or {}).get("conversion_rate") if metrics["treatment"] else None,
    }, indent=2))


if __name__ == "__main__":
    main()
