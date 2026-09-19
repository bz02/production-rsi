"""Round driver. PROTECTED — not agent-editable.

One round =
  analyse the previous round  ->  agent proposes + implements one hypothesis
  ->  diff  ->  smoke gate  ->  screenshots  ->  A/B against two live instances
  ->  analyse  ->  adopt / await approval / roll back  ->  write state.json

A/B is two whole instances rather than a feature flag: baseline on :8000 serves
`app/`, candidate on :8001 serves `candidate/`. The agent edits one copy of one tree
and never has to reason about flag plumbing, and "roll back" is just discarding a
directory.

  python orchestrator/run_loop.py --round 0 --n 80 --seed 7      # baseline only
  python orchestrator/run_loop.py --round 1 --n 80 --seed 7      # a full round
  python orchestrator/run_loop.py --rounds 3 --n 80 --seed 7     # 0,1,2,3 in sequence
  python orchestrator/run_loop.py --target 0.55 --max-rounds 6   # keep going until it converges
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analyzer.analyze import analyze_round, build_agent_summary  # noqa: E402

APP = ROOT / "app"
CANDIDATE = ROOT / "candidate"
DATA = ROOT / "data"
STATE_PATH = DATA / "state.json"
BASELINE_PORT = 8000
CANDIDATE_PORT = 8001
# Parallel browser workers per arm. Sessions are independent by construction, and the
# simulator seeds each one from its index, so this changes wall-clock time and nothing
# else — which is what makes a sample size large enough to resolve a 10pp win
# affordable in a demo slot.
WORKERS = 6

SHOT_TARGETS = [("signup", "/signup"), ("payment", "/payment")]
SHOT_VIEWPORTS = [("desktop", 1440, 900, False), ("mobile", 390, 844, True)]


# ------------------------------------------------------------------- server plumbing

def pick_port(preferred: int, label: str) -> int:
    """The documented port when it is free, an ephemeral one when it is not.

    A developer machine very often already has something on :8000, and failing a
    twenty-minute run over that is noise rather than signal. Nothing in the loop
    depends on the numbers: the simulator, the smoke gate and the screenshotter are
    all handed the instance's own URL."""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    print(f"  · :{preferred} is in use; serving the {label} arm on :{port} instead", flush=True)
    return port


class Instance:
    """A running copy of the shop, on its own port, serving its own tree."""

    def __init__(self, tree: Path, port: int, variant: str) -> None:
        self.tree = tree
        self.port = port
        self.variant = variant
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout_s: float = 20.0) -> None:
        env = dict(os.environ, PORT=str(self.port), APP_VARIANT=self.variant, PYTHONDONTWRITEBYTECODE="1")
        self.proc = subprocess.Popen(
            [sys.executable, str(self.tree / "server.py")],
            env=env, cwd=str(self.tree),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.url}/healthz", timeout=2) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.4)
        raise RuntimeError(f"{self.variant} instance on :{self.port} did not come up")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ----------------------------------------------------------------------- utilities

def run_step(argv: list[str], timeout: int = 2400) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def write_diff(round_dir: Path) -> Path:
    """`diff -ru` is the record of what the agent changed; the agent never writes it."""
    round_dir.mkdir(parents=True, exist_ok=True)
    out = round_dir / "diff.patch"
    proc = subprocess.run(
        ["diff", "-ru", "--exclude=__pycache__", "--exclude=*.pyc", "app", "candidate"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    out.write_text(proc.stdout)
    return out


def screenshots(round_dir: Path, baseline_url: str, candidate_url: str | None) -> list[str]:
    """Before/after captures of the pages the funnel turns on."""
    from playwright.sync_api import sync_playwright  # imported late: not needed for --help

    chrome = os.environ.get("FLYWHEEL_CHROME") or next(
        (p for p in ("/opt/pw-browsers/chromium",
                     "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
                     "/usr/bin/chromium") if Path(p).exists()),
        None,
    )
    round_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    pairs = [("before", baseline_url)] + ([("after", candidate_url)] if candidate_url else [])

    with sync_playwright() as pw:
        launch: dict[str, Any] = {"args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if chrome:
            launch["executable_path"] = chrome
        browser = pw.chromium.launch(**launch)
        try:
            for phase, base in pairs:
                for vp_name, w, h, mobile in SHOT_VIEWPORTS:
                    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
                    page = ctx.new_page()
                    for step, path in SHOT_TARGETS:
                        try:
                            page.goto(base + path, wait_until="domcontentloaded", timeout=10000)
                            page.wait_for_timeout(250)
                            name = f"{phase}_{vp_name}_{step}.png"
                            page.screenshot(path=str(round_dir / name), full_page=False)
                            written.append(name)
                        except Exception:  # noqa: BLE001 — a missing shot must not fail the round
                            continue
                    # The names the dashboard contract asks for by default.
                    try:
                        page.goto(base + "/payment", wait_until="domcontentloaded", timeout=10000)
                        page.wait_for_timeout(200)
                        page.screenshot(path=str(round_dir / f"{phase}_{vp_name}.png"))
                        written.append(f"{phase}_{vp_name}.png")
                    except Exception:  # noqa: BLE001
                        pass
                    ctx.close()
        finally:
            browser.close()
    return written


def simulate(round_no: int, variant: str, n: int, seed: int, base_url: str, split: str = "train") -> dict[str, Any]:
    suffix = "" if split == "train" else f".{split}"
    out = DATA / "logs" / f"round_{round_no}" / f"{variant}{suffix}.jsonl"
    proc = run_step([
        sys.executable, str(ROOT / "sim" / "simulator.py"),
        "--round", str(round_no), "--variant", variant, "--n", str(n),
        "--seed", str(seed), "--base-url", base_url, "--split", split, "--out", str(out),
        "--workers", str(WORKERS),
    ])
    if proc.returncode != 0:
        raise RuntimeError(f"simulator failed for {variant}/{split}: {proc.stderr[-400:]}")
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------- state

def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "current_round": 0,
        "phase": "idle",
        "phase_options": ["analyzing", "implementing", "running_ab", "decided", "idle"],
        "conversion_series": [],
        "rounds": [],
        "timeline": [],
    }


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = time.time()
    STATE_PATH.write_text(json.dumps(state, indent=2))


def note(state: dict[str, Any], msg: str) -> None:
    state["timeline"].append({"ts": time.time(), "msg": msg})
    state["timeline"] = state["timeline"][-80:]
    save_state(state)
    print(f"  · {msg}", flush=True)


def set_phase(state: dict[str, Any], round_no: int, phase: str) -> None:
    state["current_round"] = round_no
    state["phase"] = phase
    save_state(state)


def upsert_round(state: dict[str, Any], entry: dict[str, Any]) -> None:
    rounds = [r for r in state["rounds"] if r["round"] != entry["round"]]
    rounds.append(entry)
    state["rounds"] = sorted(rounds, key=lambda r: r["round"])


def upsert_series(state: dict[str, Any], point: dict[str, Any]) -> None:
    series = [p for p in state["conversion_series"] if p["round"] != point["round"]]
    series.append(point)
    state["conversion_series"] = sorted(series, key=lambda p: p["round"])


# ---------------------------------------------------------------------- the rounds

def run_round_zero(n: int, seed: int, state: dict[str, Any]) -> dict[str, Any]:
    print("\n=== Round 0 — baseline ===", flush=True)
    set_phase(state, 0, "running_ab")
    baseline = Instance(APP, BASELINE_PORT, "baseline")
    baseline.start()
    round_dir = DATA / "runs" / "round_0"
    try:
        note(state, f"Round 0: measuring the untouched baseline over {n} sessions")
        simulate(0, "control", n, seed, baseline.url, "train")
        simulate(0, "control", max(12, n // 4), seed, baseline.url, "holdout")
        screenshots(round_dir, baseline.url, None)
    finally:
        baseline.stop()

    metrics = analyze_round(0, smoke_pass=True, diff_path=round_dir / "diff.patch")
    summary = build_agent_summary(0, metrics)
    (DATA / "metrics").mkdir(parents=True, exist_ok=True)
    (DATA / "analysis").mkdir(parents=True, exist_ok=True)
    (DATA / "metrics" / "round_0.json").write_text(json.dumps(metrics, indent=2))
    (DATA / "analysis" / "round_0_summary.json").write_text(json.dumps(summary, indent=2))

    cr = metrics["control"]["conversion_rate"]
    upsert_series(state, {"round": 0, "control": cr})
    upsert_round(state, {
        "round": 0,
        "title": "Baseline measurement",
        "status": "baseline",
        "autonomy": "n/a",
        "lift_rel": None, "p_value": None,
        "before_img": "runs/round_0/before_mobile.png",
        "after_img": None,
        "diff_path": None,
        "metrics_path": "metrics/round_0.json",
        "conversion": {"control": cr, "treatment": None},
        "hypotheses": [],
        "change_md": None,
        "smoke": {"passed": True, "pass_count": 0, "case_count": 0},
        "top_friction": summary["top_friction"][:5],
    })
    set_phase(state, 0, "decided")
    note(state, f"Round 0 baseline conversion {cr:.1%}; top friction: "
                f"{', '.join(f['value'] for f in summary['top_friction'][:3]) or 'none'}")
    return metrics


def run_round(round_no: int, n: int, seed: int, state: dict[str, Any], auto_approve: bool) -> dict[str, Any]:
    print(f"\n=== Round {round_no} ===", flush=True)
    round_dir = DATA / "runs" / f"round_{round_no}"
    summary_path = DATA / "analysis" / f"round_{round_no - 1}_summary.json"

    # 1. the agent reads last round's summary and implements one hypothesis
    set_phase(state, round_no, "implementing")
    note(state, f"Round {round_no}: agent reading round {round_no - 1} summary")
    proc = run_step([
        sys.executable, str(ROOT / "agent" / "agent.py"),
        "--round", str(round_no), "--summary", str(summary_path), "--out", str(round_dir),
    ])
    if proc.returncode != 0:
        raise RuntimeError(f"agent failed: {proc.stderr[-500:]}")
    agent_out = json.loads(proc.stdout)
    hypotheses = json.loads((round_dir / "hypotheses.json").read_text())
    note(state, f"Agent proposed {len(hypotheses)} hypotheses ({agent_out['mode']} ranking), "
                f"selected: {agent_out['title']}")

    # 2. the diff is produced by the orchestrator, not the agent
    diff_path = write_diff(round_dir)
    changed = [ln for ln in diff_path.read_text().splitlines() if ln.startswith("diff -ru")]
    note(state, f"Diff written: {len(changed)} file(s) changed")

    # 3. bring both arms up
    set_phase(state, round_no, "running_ab")
    baseline = Instance(APP, BASELINE_PORT, "baseline")
    candidate = Instance(CANDIDATE, CANDIDATE_PORT, "candidate")
    smoke: dict[str, Any] = {"passed": False, "cases": []}
    metrics: dict[str, Any]

    try:
        baseline.start()
        try:
            candidate.start()
        except RuntimeError as exc:
            note(state, f"Candidate failed to boot — {exc}")
            smoke = {"passed": False, "cases": [{"case": "candidate boots", "kind": "contract",
                                                 "passed": False, "detail": str(exc)}],
                     "pass_count": 0, "case_count": 1}
            metrics = analyze_round(round_no, smoke_pass=False, diff_path=diff_path)
            _finalise(state, round_no, metrics, hypotheses, smoke, round_dir, agent_out, auto_approve)
            return metrics

        # 4. the eval gate. A candidate that fails here never sees traffic.
        proc = run_step([sys.executable, str(ROOT / "sim" / "smoke.py"),
                         "--base-url", candidate.url, "--out", str(round_dir / "smoke.json")])
        try:
            smoke = json.loads(proc.stdout)
        except json.JSONDecodeError:
            smoke = {"passed": False, "cases": [], "pass_count": 0, "case_count": 0}
        note(state, f"Eval gate: {smoke.get('pass_count')}/{smoke.get('case_count')} cases passed "
                    f"-> {'PASS' if smoke.get('passed') else 'FAIL'}")

        screenshots(round_dir, baseline.url, candidate.url)

        if not smoke.get("passed"):
            note(state, "Candidate failed the gate; skipping A/B and rolling back")
            metrics = analyze_round(round_no, smoke_pass=False, diff_path=diff_path)
        else:
            # 5. live A/B — both arms get an independent, seeded user stream
            note(state, f"Running A/B: {n} sessions per arm")
            c = simulate(round_no, "control", n, seed, baseline.url, "train")
            t = simulate(round_no, "treatment", n, seed, candidate.url, "train")
            note(state, f"control {c['conversion_rate']:.1%} vs treatment {t['conversion_rate']:.1%}")
            # holdout personas, never shown to the agent
            simulate(round_no, "control", max(12, n // 4), seed, baseline.url, "holdout")
            simulate(round_no, "treatment", max(12, n // 4), seed, candidate.url, "holdout")
            metrics = analyze_round(round_no, smoke_pass=True, diff_path=diff_path)
    finally:
        candidate.stop()
        baseline.stop()

    _finalise(state, round_no, metrics, hypotheses, smoke, round_dir, agent_out, auto_approve)
    return metrics


def _finalise(state: dict[str, Any], round_no: int, metrics: dict[str, Any],
              hypotheses: list[dict[str, Any]], smoke: dict[str, Any], round_dir: Path,
              agent_out: dict[str, Any], auto_approve: bool) -> None:
    """Write metrics, apply the decision, and update the dashboard's state."""
    set_phase(state, round_no, "decided")
    (DATA / "metrics").mkdir(parents=True, exist_ok=True)
    (DATA / "analysis").mkdir(parents=True, exist_ok=True)
    (DATA / "metrics" / f"round_{round_no}.json").write_text(json.dumps(metrics, indent=2))

    decision = metrics["decision"]
    note(state, f"Decision: {decision.upper()} — {metrics['decision_reason']}")

    # Keep a snapshot so a later approval can still merge this exact candidate.
    snapshot = round_dir / "candidate_snapshot"
    if CANDIDATE.exists():
        if snapshot.exists():
            shutil.rmtree(snapshot)
        shutil.copytree(CANDIDATE, snapshot, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    status = decision
    if decision == "adopt":
        adopt(round_no)
        status = "adopted"
        note(state, "Candidate promoted to baseline automatically (inside auto-adopt policy)")
    elif decision == "pending_approval":
        if auto_approve:
            adopt(round_no)
            status = "adopted"
            note(state, "Approval auto-granted by --auto-approve (stands in for a human click)")
        else:
            status = "pending_approval"
            note(state, "Waiting for a human decision in the dashboard")
    elif decision in ("rollback", "invalid"):
        status = "rolled_back" if decision == "rollback" else "invalid"

    summary = build_agent_summary(round_no, metrics)
    (DATA / "analysis" / f"round_{round_no}_summary.json").write_text(json.dumps(summary, indent=2))

    c_rate = metrics["control"]["conversion_rate"]
    t_rate = (metrics["treatment"] or {}).get("conversion_rate") if metrics["treatment"] else None
    comparison = metrics.get("comparison") or {}
    upsert_series(state, {
        "round": round_no,
        "control": c_rate,
        "treatment": t_rate,
        "adopted": status == "adopted",
        "pending": status == "pending_approval",
    })
    upsert_round(state, {
        "round": round_no,
        "title": agent_out.get("title") or f"Round {round_no}",
        "hypothesis_id": agent_out.get("chosen"),
        "selection_mode": agent_out.get("mode"),
        "status": status,
        "autonomy": metrics.get("autonomy"),
        "lift_abs": comparison.get("lift_abs"),
        "lift_rel": comparison.get("lift_rel"),
        "p_value": comparison.get("p_value"),
        "ci95": comparison.get("ci95"),
        "before_img": f"runs/round_{round_no}/before_mobile.png",
        "after_img": f"runs/round_{round_no}/after_mobile.png",
        "shots": sorted(p.name for p in round_dir.glob("*.png")),
        "diff_path": f"runs/round_{round_no}/diff.patch",
        "metrics_path": f"metrics/round_{round_no}.json",
        "change_md": f"runs/round_{round_no}/change.md",
        "conversion": {"control": c_rate, "treatment": t_rate},
        "hypotheses": hypotheses,
        "smoke": {"passed": smoke.get("passed"), "pass_count": smoke.get("pass_count"),
                  "case_count": smoke.get("case_count"), "cases": smoke.get("cases", [])},
        "guardrails": metrics.get("guardrails"),
        "decision_reason": metrics.get("decision_reason"),
        "diff_stats": {"files": metrics["diff"]["files_changed"], "lines": metrics["diff"]["lines_changed"],
                       "paths": metrics["diff"]["files"],
                       "protected_touched": metrics["diff"]["protected_paths_touched"],
                       "needs_approval": metrics["diff"]["needs_approval_hits"]},
        "holdout": metrics.get("holdout"),
        "top_friction": summary["top_friction"][:5],
    })
    save_state(state)


def adopt(round_no: int) -> None:
    """Promote the candidate to baseline. Deliberately a whole-tree replace: partial
    merges are how a rolled-back change leaks into production."""
    source = CANDIDATE if CANDIDATE.exists() else DATA / "runs" / f"round_{round_no}" / "candidate_snapshot"
    if not source.exists():
        raise RuntimeError(f"nothing to adopt for round {round_no}")
    backup = DATA / "runs" / f"round_{round_no}" / "app_before_adopt"
    if backup.exists():
        shutil.rmtree(backup)
    shutil.copytree(APP, backup, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.rmtree(APP)
    shutil.copytree(source, APP, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def apply_approval(round_no: int, approve: bool) -> dict[str, Any]:
    """Called by the dashboard's approve/reject endpoints."""
    state = load_state()
    entry = next((r for r in state["rounds"] if r["round"] == round_no), None)
    if entry is None:
        return {"ok": False, "error": f"round {round_no} not found"}
    if entry["status"] != "pending_approval":
        return {"ok": False, "error": f"round {round_no} is '{entry['status']}', not pending"}

    if approve:
        adopt(round_no)
        entry["status"] = "adopted"
        entry["autonomy"] = "human_approved"
        note(state, f"Round {round_no} approved by a human and promoted to baseline")
    else:
        entry["status"] = "rolled_back"
        entry["autonomy"] = "human_rejected"
        note(state, f"Round {round_no} rejected by a human; baseline unchanged")

    for point in state["conversion_series"]:
        if point["round"] == round_no:
            point["adopted"] = approve
            point["pending"] = False
    save_state(state)
    return {"ok": True, "round": round_no, "status": entry["status"]}


def current_baseline(state: dict[str, Any]) -> float | None:
    """Conversion of whatever is now serving as the baseline: the most recent adopted
    treatment, or the last measured control if nothing has been adopted."""
    series = state.get("conversion_series") or []
    if not series:
        return None
    adopted = [p for p in series if p.get("adopted") and p.get("treatment") is not None]
    if adopted:
        return adopted[-1]["treatment"]
    return series[-1].get("control")


def run_until_target(target: float, max_rounds: int, n: int, seed: int,
                     state: dict[str, Any], auto_approve: bool) -> dict[str, Any]:
    """Keep proposing, shipping and measuring until the baseline clears `target`.

    This is the shape the loop is meant to have: rounds are not a budget to spend but
    attempts at a number. It still stops at `max_rounds`, because a loop that cannot
    reach its target needs a human to hear about it rather than to keep burning."""
    if not any(p["round"] == 0 for p in state.get("conversion_series", [])):
        run_round_zero(n, seed, state)

    outcome = {"target": target, "reached": False, "rounds_run": 0}
    for r in range(1, max_rounds + 1):
        baseline = current_baseline(state)
        if baseline is not None and baseline >= target:
            outcome.update(reached=True, final=baseline, rounds_run=r - 1)
            note(state, f"Target reached: baseline {baseline:.1%} >= {target:.1%} after {r - 1} round(s)")
            return outcome
        note(state, f"Baseline {(baseline or 0):.1%} is short of the {target:.1%} target; starting round {r}")
        run_round(r, n, seed, state, auto_approve)
        outcome["rounds_run"] = r

    final = current_baseline(state)
    outcome.update(reached=bool(final is not None and final >= target), final=final)
    if outcome["reached"]:
        note(state, f"Target reached: baseline {final:.1%} >= {target:.1%}")
    else:
        note(state, f"Stopped at the {max_rounds}-round limit with the baseline at "
                    f"{(final or 0):.1%}, short of the {target:.1%} target")
    return outcome


def main() -> None:
    global BASELINE_PORT, CANDIDATE_PORT, WORKERS

    ap = argparse.ArgumentParser(description="Run one or more improvement rounds")
    ap.add_argument("--round", type=int, help="run exactly this round")
    ap.add_argument("--rounds", type=int, help="run rounds 0..N in sequence")
    ap.add_argument("--target", type=float,
                    help="keep running rounds until the baseline conversion reaches this "
                         "(e.g. 0.55), instead of a fixed round count")
    ap.add_argument("--max-rounds", type=int, default=6,
                    help="hard stop when using --target (default 6)")
    ap.add_argument("--n", type=int, default=80, help="sessions per arm")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--workers", type=int, default=WORKERS,
                    help=f"parallel browser sessions per arm (default {WORKERS}; the "
                         "results do not depend on it)")
    ap.add_argument("--baseline-port", type=int, default=BASELINE_PORT,
                    help=f"port for the baseline arm (default {BASELINE_PORT}; a free port is "
                         "chosen automatically if it is taken)")
    ap.add_argument("--candidate-port", type=int, default=CANDIDATE_PORT,
                    help=f"port for the candidate arm (default {CANDIDATE_PORT})")
    ap.add_argument("--auto-approve", action="store_true",
                    help="stand in for a human clicking Approve, for unattended runs")
    ap.add_argument("--reset", action="store_true", help="clear data/ and restore app/ from git before running")
    args = ap.parse_args()

    WORKERS = max(1, args.workers)
    BASELINE_PORT = pick_port(args.baseline_port, "baseline")
    CANDIDATE_PORT = pick_port(args.candidate_port, "candidate")

    if args.reset:
        for sub in ("logs", "runs", "metrics", "analysis"):
            shutil.rmtree(DATA / sub, ignore_errors=True)
        STATE_PATH.unlink(missing_ok=True)
        subprocess.run(["git", "checkout", "--", "app"], cwd=str(ROOT), capture_output=True)
        print("reset: data/ cleared and app/ restored from git")

    state = load_state()
    started = time.time()

    target_outcome: dict[str, Any] | None = None
    if args.target is not None:
        target_outcome = run_until_target(args.target, args.max_rounds, args.n, args.seed,
                                          state, args.auto_approve)
    elif args.rounds is not None:
        run_round_zero(args.n, args.seed, state)
        for r in range(1, args.rounds + 1):
            run_round(r, args.n, args.seed, state, args.auto_approve)
    elif args.round == 0:
        run_round_zero(args.n, args.seed, state)
    elif args.round is not None:
        run_round(args.round, args.n, args.seed, state, args.auto_approve)
    else:
        ap.error("pass --round N, --rounds N, or --target RATE")

    set_phase(state, state["current_round"], "idle")
    series = state["conversion_series"]
    print(f"\n=== done in {time.time() - started:.0f}s ===")
    for p in series:
        t = f" treatment {p['treatment']:.1%}" if p.get("treatment") is not None else ""
        flag = " [adopted]" if p.get("adopted") else (" [pending]" if p.get("pending") else "")
        print(f"  round {p['round']}: control {p['control']:.1%}{t}{flag}")

    base = series[0]["control"] if series else None
    final = current_baseline(state)
    if base and final:
        print(f"  baseline moved {base:.1%} -> {final:.1%} ({(final - base) / base:+.0%} relative)")
    if target_outcome:
        verdict = "REACHED" if target_outcome["reached"] else "NOT REACHED"
        print(f"  target {target_outcome['target']:.1%}: {verdict} "
              f"after {target_outcome['rounds_run']} round(s)")


if __name__ == "__main__":
    main()
