"""The improvement agent. PROTECTED — the agent cannot edit its own loop.

Contract:
  input   data/analysis/round_{N-1}_summary.json   (a summary, never raw logs)
  output  data/runs/round_{N}/hypotheses.json      (3-5 ranked hypotheses)
          data/runs/round_{N}/change.md            (what changed and why)
          candidate/**                             (the actual code change)

Each round the agent reads the summary, proposes several hypotheses, ranks them, and
**implements only the top one**. One hypothesis per round keeps attribution clean: the
A/B result can be ascribed to a single change rather than to a bundle.

Tooling is a strict whitelist — read_file, write_file, list_dir, run_smoke — with no
shell and no network. write_file refuses any path outside `candidate/`, which is what
makes "the agent cannot touch the evaluator" a mechanical guarantee rather than a
promise. The analyzer independently re-checks the diff against protected paths, so
the guarantee is enforced twice, in two different processes.

Where the model fits: with ANTHROPIC_API_KEY set, Claude ranks the candidate
hypotheses and writes the rationale. The edits themselves come from a playbook of
parameterised, smoke-tested transforms rather than free-form generated diffs. That is
a deliberate trade for a loop that must not break its own evaluation harness: the
model chooses *what* to fix from evidence, and the playbook guarantees the edit is
well-formed. Without a key the ranking falls back to friction volume, which is the
same ordering the model almost always produces anyway.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "candidate"
APP = ROOT / "app"

MODEL = os.environ.get("FLYWHEEL_MODEL", "claude-sonnet-5")
LLM_TIMEOUT_S = 30


# ------------------------------------------------------------------ tool whitelist

class Tools:
    """Every filesystem touch the agent makes goes through here."""

    def __init__(self, candidate_dir: Path) -> None:
        self.candidate = candidate_dir.resolve()
        self.writes: list[str] = []

    def _guard(self, path: str) -> Path:
        target = (self.candidate / path).resolve()
        if not str(target).startswith(str(self.candidate) + os.sep) and target != self.candidate:
            raise PermissionError(f"write_file refused: {path} is outside the candidate directory")
        return target

    def list_dir(self, path: str = ".") -> list[str]:
        base = self._guard(path)
        if not base.exists():
            return []
        return sorted(p.relative_to(self.candidate).as_posix() for p in base.rglob("*") if p.is_file())

    def read_file(self, path: str) -> str:
        target = self._guard(path)
        return target.read_text(encoding="utf-8") if target.exists() else ""

    def write_file(self, path: str, contents: str) -> None:
        target = self._guard(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
        rel = target.relative_to(self.candidate).as_posix()
        if rel not in self.writes:
            self.writes.append(rel)

    def run_smoke(self, base_url: str) -> dict[str, Any]:
        """The agent may check its own work before submitting, but the orchestrator
        re-runs the gate independently — a self-reported pass is never trusted."""
        proc = subprocess.run(
            [sys.executable, str(ROOT / "sim" / "smoke.py"), "--base-url", base_url],
            capture_output=True, text=True, timeout=180,
        )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"passed": False, "cases": [], "detail": proc.stderr[-300:]}


# ----------------------------------------------------------------------- playbook

Playbook = dict[str, Any]


def _signup_field_limit(tools: Tools) -> int | None:
    """How many signup fields the candidate currently renders, if it slices them."""
    html = tools.read_file("templates/signup.html")
    m = re.search(r"fields\[:(\d+)\]", html)
    return int(m.group(1)) if m else None


def pb_trim_signup(tools: Tools) -> dict[str, Any]:
    """Render only the four fields needed to create an account."""
    html = tools.read_file("templates/signup.html")
    new = html.replace("{% for f in fields %}", "{% for f in fields[:4] %}")
    new = new.replace(
        "<p class=\"step-note\">An account keeps your order history and delivery details together.</p>",
        "<p class=\"step-note\">Four details and you are done &mdash; everything else can wait until later.</p>",
    )
    tools.write_file("templates/signup.html", new)
    return {"files": ["templates/signup.html"],
            "note": "signup now renders fields[:4]; the server validates only submitted fields, "
                    "so the remaining five are collected later instead of blocking checkout"}


def pb_guest_checkout(tools: Tools) -> dict[str, Any]:
    """Offer a guest path so an account is no longer a precondition for buying."""
    html = tools.read_file("templates/signup.html")
    if 'data-testid="guest-checkout"' not in html:
        block = (
            '<div class="guest-row">\n'
            '  <a class="btn btn-ghost btn-wide" data-testid="guest-checkout" href="/guest">'
            'Continue as guest</a>\n'
            '  <p class="guest-note">No account needed. You can create one after checkout.</p>\n'
            '</div>\n'
        )
        html = html.replace("<form method=\"post\" action=\"/signup\"", block + "<form method=\"post\" action=\"/signup\"")
        tools.write_file("templates/signup.html", html)

    server = tools.read_file("server.py")
    if '@app.route("/guest")' not in server:
        route = '''

@app.route("/guest")
def guest():
    """Guest checkout. Requiring an account before payment was costing more orders
    than the account itself was worth; the customer can register after paying."""
    session["guest"] = True
    return redirect(url_for("shipping"))
'''
        server = server.replace('\n\n@app.route("/shipping"', route + '\n\n@app.route("/shipping"')
        tools.write_file("server.py", server)

    css = tools.read_file("static/app.css")
    if ".guest-row" not in css:
        css += (
            "\n.guest-row { display: grid; gap: 6px; margin-bottom: 18px; padding-bottom: 18px;"
            " border-bottom: 1px solid var(--line); }\n"
            ".guest-note { font-size: 12.5px; color: var(--muted); margin: 0; text-align: center; }\n"
        )
        tools.write_file("static/app.css", css)

    return {"files": ["templates/signup.html", "server.py", "static/app.css"],
            "note": "adds [data-testid=\"guest-checkout\"] and a /guest route that skips account "
                    "creation; touches app/server.py, so policy requires human approval"}


def pb_fix_mobile_occlusion(tools: Tools) -> dict[str, Any]:
    """Reserve space for the pinned pay bar so it stops covering the pay button."""
    css = tools.read_file("static/app.css")
    if "--paybar-h" in css:
        return {"files": [], "note": "already applied"}
    patched = css.replace(
        """  .paybar {
    display: block;""",
        """  /* The pay bar is pinned, so the page has to reserve its height. Without this
     the last element on the page — the pay button itself — sits underneath it and
     cannot be tapped at all. */
  :root { --paybar-h: 132px; }
  body[data-step="payment"] { padding-bottom: var(--paybar-h); }

  .paybar {
    display: block;""",
    )
    tools.write_file("static/app.css", patched)
    return {"files": ["static/app.css"],
            "note": "reserves the pinned pay bar's height as page padding on phones, so the pay "
                    "button is no longer underneath it"}


PLAYBOOKS: list[Playbook] = [
    {
        "id": "h_trim_signup",
        "title": "Cut the signup form to four fields",
        "friction": {"type": "abandon_reason", "value": "too_many_fields"},
        "expected_impact": "high",
        "risk": "low",
        "files": ["app/templates/signup.html"],
        "apply": pb_trim_signup,
        "done": lambda tools: (_signup_field_limit(tools) or 99) <= 4,
        "why": "The form asks for nine fields before anyone may pay. Personas abandon "
               "when a single form exceeds what they will tolerate, and the extra five "
               "fields are not needed to take an order.",
    },
    {
        "id": "h_guest_checkout",
        "title": "Add a guest checkout path",
        "friction": {"type": "abandon_reason", "value": "forced_signup"},
        "expected_impact": "high",
        "risk": "medium",
        "files": ["app/templates/signup.html", "app/server.py"],
        "apply": pb_guest_checkout,
        "done": lambda tools: 'data-testid="guest-checkout"' in tools.read_file("templates/signup.html"),
        "why": "Checkout is gated behind account creation with no way around it. Shoppers "
               "who do not want an account currently have nowhere to go but away.",
    },
    {
        "id": "h_fix_mobile_tap",
        "title": "Stop the offer bar covering the pay button on phones",
        "friction": {"type": "blocked_click"},
        "expected_impact": "high",
        "risk": "low",
        "files": ["app/static/app.css"],
        "apply": pb_fix_mobile_occlusion,
        "done": lambda tools: "--paybar-h" in tools.read_file("static/app.css"),
        "why": "Clicks on the forward action are being intercepted on small viewports. "
               "The pinned pay bar overlaps the end of the page because no space is "
               "reserved for its height, so the button cannot be tapped at all.",
    },
]


def score_playbooks(summary: dict[str, Any], tools: Tools) -> list[dict[str, Any]]:
    """Attach the evidence from this round's summary to each playbook and rank."""
    friction = summary.get("top_friction", [])
    scored: list[dict[str, Any]] = []
    for pb in PLAYBOOKS:
        want = pb["friction"]
        hits = [
            f for f in friction
            if f.get("type") == want["type"] and ("value" not in want or f.get("value") == want["value"])
        ]
        count = sum(f.get("count", 0) for f in hits)
        already = False
        try:
            already = bool(pb["done"](tools))
        except Exception:  # noqa: BLE001 — a missing candidate file just means "not applied"
            already = False
        evidence_bits = []
        for f in hits[:2]:
            bit = f"{f.get('count')} sessions"
            if f.get("step"):
                bit += f" at step '{f['step']}'"
            if f.get("device"):
                bit += f" on {f['device']}"
            if f.get("field_count"):
                bit += f" ({f['field_count']} fields shown)"
            evidence_bits.append(bit)
        scored.append({
            "id": pb["id"],
            "title": pb["title"],
            "evidence": (f"{pb['why']} Observed: " + "; ".join(evidence_bits)) if evidence_bits
                        else f"{pb['why']} Not observed in this round's logs.",
            "expected_impact": pb["expected_impact"],
            "risk": pb["risk"],
            "files": pb["files"],
            "_count": count,
            "_already_applied": already,
            "_pb": pb,
        })
    # Rank by observed friction volume; anything already shipped drops to the bottom.
    scored.sort(key=lambda d: (not d["_already_applied"], d["_count"]), reverse=True)
    return scored


# ---------------------------------------------------------------------------- LLM

def llm_rank(summary: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Ask Claude to pick and justify. Times out fast and fails soft: the loop must
    finish a round even when the API is unreachable."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    base = (os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
    prompt = json.dumps({
        "instruction": (
            "You are a growth engineer. Pick the ONE hypothesis to implement this round and rank the rest. "
            "Choose from the given candidate ids only. Judge by evidence volume and how directly the change "
            "removes the observed friction. Respond as JSON: "
            '{"chosen_id": "...", "order": ["id", ...], "rationale": "2-3 sentences"}'
        ),
        "summary": summary,
        "candidates": [
            {"id": c["id"], "title": c["title"], "evidence": c["evidence"],
             "risk": c["risk"], "already_applied": c["_already_applied"]}
            for c in candidates
        ],
    })
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 900,
        "temperature": 0,
        "system": "You respond with a single JSON object and nothing else.",
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        f"{base}/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"},
    )
    for attempt in range(2):  # one retry, then fall back
        try:
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
                payload = json.loads(resp.read())
            text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1:
                return None
            parsed = json.loads(text[start:end + 1])
            if parsed.get("chosen_id") in {c["id"] for c in candidates}:
                return parsed
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
            if attempt == 1:
                return None
    return None


# --------------------------------------------------------------------- the round

def reset_candidate() -> None:
    """The candidate always starts as an exact copy of the current baseline, so each
    round's diff is exactly one round of change."""
    if CANDIDATE.exists():
        shutil.rmtree(CANDIDATE)
    shutil.copytree(APP, CANDIDATE, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def run(round_no: int, summary_path: Path, out_dir: Path, smoke_url: str | None = None) -> dict[str, Any]:
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {"top_friction": []}
    reset_candidate()
    tools = Tools(CANDIDATE)

    candidates = score_playbooks(summary, tools)
    verdict = llm_rank(summary, candidates)
    mode = "llm" if verdict else "heuristic"

    if verdict:
        order = {cid: i for i, cid in enumerate(verdict.get("order", []))}
        candidates.sort(key=lambda c: order.get(c["id"], 99))
        chosen = next(c for c in candidates if c["id"] == verdict["chosen_id"])
    else:
        actionable = [c for c in candidates if not c["_already_applied"] and c["_count"] > 0]
        chosen = (actionable or candidates)[0]

    result = chosen["_pb"]["apply"](tools)

    hypotheses = [
        {
            "id": c["id"],
            "title": c["title"],
            "evidence": c["evidence"],
            "expected_impact": c["expected_impact"],
            "risk": c["risk"],
            "files": c["files"],
            "friction_sessions": c["_count"],
            "already_shipped": c["_already_applied"],
            "selected": c["id"] == chosen["id"],
        }
        for c in candidates
    ]

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "hypotheses.json").write_text(json.dumps(hypotheses, indent=2))

    rationale = (verdict or {}).get("rationale") or (
        f"Selected by observed friction volume: {chosen['_count']} sessions were lost to this cause in the "
        f"round just analysed, more than any other single cause the playbook can address."
    )
    change_md = f"""# Round {round_no} — {chosen['title']}

**Selection mode:** {mode} · **Hypothesis:** `{chosen['id']}`

## Why this one
{rationale}

## Evidence
{chosen['evidence']}

## What changed
{result['note']}

Files written: {', '.join(f'`candidate/{f}`' for f in tools.writes) or '_none_'}

## Hypotheses considered
| rank | id | title | friction sessions | risk | status |
|---|---|---|---|---|---|
""" + "\n".join(
        f"| {i + 1} | `{h['id']}` | {h['title']} | {h['friction_sessions']} | {h['risk']} | "
        f"{'**selected**' if h['selected'] else ('already shipped' if h['already_shipped'] else 'deferred')} |"
        for i, h in enumerate(hypotheses)
    ) + f"""

## Baseline going in
Conversion {summary.get('baseline_conversion', 0):.1%} over {summary.get('baseline_sessions', 0)} sessions.
"""
    (out_dir / "change.md").write_text(change_md)

    smoke = tools.run_smoke(smoke_url) if smoke_url else None

    return {
        "round": round_no,
        "mode": mode,
        "chosen": chosen["id"],
        "title": chosen["title"],
        "files_written": tools.writes,
        "hypotheses": len(hypotheses),
        "self_smoke": (smoke or {}).get("passed"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Improvement agent: summary in, candidate change out")
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--summary")
    ap.add_argument("--out")
    ap.add_argument("--smoke-url")
    args = ap.parse_args()

    summary = Path(args.summary) if args.summary else ROOT / "data" / "analysis" / f"round_{args.round - 1}_summary.json"
    out = Path(args.out) if args.out else ROOT / "data" / "runs" / f"round_{args.round}"
    print(json.dumps(run(args.round, summary, out, args.smoke_url), indent=2))


if __name__ == "__main__":
    main()
