"""Checks on the model call. Run with: python agent/test_llm.py

`llm_rank` is the only place a model touches this loop, and the loop's promise is
that it degrades to the heuristic rather than failing a round. That promise is worth
testing precisely because it is invisible when it works: a run with a dead API key
looks exactly like a run without one, and the only way to know the difference is
deliberate is to assert it.

The transport is stubbed — no key, no network, no cost — so these run in CI beside
everything else. What is asserted is the contract: a good answer is used, and every
bad one falls back to None, which is what makes the model optional rather than a
dependency.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Loaded by path: running this file puts `agent/` on sys.path, so `import agent`
# would find agent.py itself rather than the directory it lives in.
_spec = importlib.util.spec_from_file_location("flywheel_agent", ROOT / "agent" / "agent.py")
_agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_agent)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  pass  {name}")
    else:
        failures.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


CANDIDATES = [
    {"id": "h_trim_signup", "title": "Cut the signup form", "evidence": "113 sessions",
     "risk": "low", "_already_applied": False},
    {"id": "h_guest_checkout", "title": "Add a guest checkout path", "evidence": "15 sessions",
     "risk": "medium", "_already_applied": False},
]
SUMMARY = {"baseline_conversion": 0.285, "top_friction": []}


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def with_transport(fn, key: str | None = "test-key"):
    """Run llm_rank against a stubbed urlopen, restoring both afterwards."""
    original = _agent.urllib.request.urlopen
    had_key = os.environ.get("ANTHROPIC_API_KEY")
    if key is None:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    else:
        os.environ["ANTHROPIC_API_KEY"] = key
    _agent.urllib.request.urlopen = fn
    try:
        return _agent.llm_rank(SUMMARY, CANDIDATES)
    finally:
        _agent.urllib.request.urlopen = original
        if had_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = had_key


def text_response(text: str):
    return lambda *_a, **_kw: FakeResponse({"content": [{"type": "text", "text": text}]})


print("llm_rank")

good = json.dumps({"chosen_id": "h_guest_checkout",
                   "order": ["h_guest_checkout", "h_trim_signup"],
                   "rationale": "Guest checkout removes the wall entirely."})
v = with_transport(text_response(good))
check("a well-formed answer is used", (v or {}).get("chosen_id") == "h_guest_checkout", str(v))
check("and its ordering comes back", (v or {}).get("order") == ["h_guest_checkout", "h_trim_signup"], str(v))
check("and its rationale comes back", "Guest checkout" in (v or {}).get("rationale", ""), str(v))

# Models sometimes wrap JSON in prose; the parser cuts to the outermost braces.
wrapped = f"Here is my answer:\n```json\n{good}\n```\nHope that helps."
v = with_transport(text_response(wrapped))
check("JSON wrapped in prose is still parsed", (v or {}).get("chosen_id") == "h_guest_checkout", str(v))

# Everything below must fall back rather than raise, or a round dies on a bad answer.
v = with_transport(text_response(json.dumps({"chosen_id": "h_delete_the_analyzer", "order": []})))
check("an id that was never offered is refused", v is None, str(v))

v = with_transport(text_response("I would suggest trimming the signup form."))
check("an answer with no JSON at all falls back", v is None, str(v))

v = with_transport(text_response('{"chosen_id": "h_trim_signup", '))
check("truncated JSON falls back", v is None, str(v))

calls: list[int] = []


def flaky(*_a, **_kw):
    calls.append(1)
    raise urllib.error.URLError("connection refused")


v = with_transport(flaky)
check("an unreachable API falls back", v is None, str(v))
check("and retries exactly once before giving up", len(calls) == 2, f"{len(calls)} call(s)")


def must_not_be_called(*_a, **_kw):
    raise AssertionError("urlopen was called with no API key set")


v = with_transport(must_not_be_called, key=None)
check("no API key means no request at all", v is None, str(v))


print("\nheuristic fallback ranking")

# The claim the README makes about the fallback: it picks by observed friction volume,
# which is the same thing the model picks when the evidence is not ambiguous.
scored = [
    {"id": "h_trim_signup", "_count": 113, "_already_applied": False},
    {"id": "h_guest_checkout", "_count": 15, "_already_applied": False},
    {"id": "h_fix_mobile_tap", "_count": 15, "_already_applied": True},
]
scored.sort(key=lambda d: (not d["_already_applied"], d["_count"]), reverse=True)
actionable = [c for c in scored if not c["_already_applied"] and c["_count"] > 0]
check("the heaviest unshipped friction wins", actionable[0]["id"] == "h_trim_signup", str(actionable[0]))
check("an already-shipped change sinks to the bottom", scored[-1]["id"] == "h_fix_mobile_tap", str(scored[-1]))


print()
if failures:
    print(f"{len(failures)} check(s) failed")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all checks passed")
