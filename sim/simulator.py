"""Persona-driven user simulator. PROTECTED — the agent may not edit this file.

Real browser sessions via Playwright, driven by the rule-based personas in
personas/personas.json. There is deliberately no LLM in the loop here: the simulator
has to be fast, cheap and reproducible under a fixed seed, and a scripted persona is
all three.

The simulator is also the only writer of logs. The app never emits telemetry, so the
agent cannot break (or game) instrumentation by editing the app.

Behaviour rules, per step:
  1. visible required fields > persona.max_fields_per_form  -> likely abandon
     (reason=too_many_fields)
  2. on `signup` with no [data-testid="guest-checkout"]     -> abandon with
     persona.abandon_on_forced_signup_prob (reason=forced_signup)
  3. guest-checkout present and persona prefers it          -> click it
  4. click intercepted or times out                         -> click_blocked, retry
     twice, then abandon (reason=blocked)
  5. with persona.typo_rate, submit an invalid value first  -> validation_error,
     then correct it and continue

Unknown steps fall through to the generic handler: fill every required field, then
click primary-action. That is what lets the agent add or remove funnel steps.

Usage:
  python sim/simulator.py --round 1 --variant control --n 80 --seed 7 \
      --base-url http://127.0.0.1:8000 --split train
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from pathlib import Path
from typing import Any, Iterator

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PERSONAS_PATH = ROOT / "personas" / "personas.json"

# Pin the browser binary. The pip `playwright` package expects whatever build its
# own version was pinned to, which need not be the one installed on the machine, so
# an explicit path is more reliable than letting it resolve by version.
CHROME_PATH = os.environ.get("FLYWHEEL_CHROME") or next(
    (p for p in ("/opt/pw-browsers/chromium/chrome",
                 "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
                 "/usr/bin/chromium",
                 "/usr/bin/google-chrome") if Path(p).exists()),
    None,
)

PRIMARY = '[data-testid="primary-action"]'
GUEST = '[data-testid="guest-checkout"]'
FIELD = 'input[data-testid^="field-"]'
CLICK_TIMEOUT_MS = 2500
NAV_TIMEOUT_MS = 8000
MAX_STEPS = 24

SAMPLE_VALUES = {
    "email": "shopper{n}@example.com",
    "password": "correct-horse-{n}",
    "password_confirm": "correct-horse-{n}",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "company": "Nimbus",
    "phone": "+44 7700 900{n:03d}",
    "referral": "a friend",
    "newsletter_pref": "weekly",
    "address1": "{n} Enfield Road",
    "city": "Manchester",
    "postcode": "M1 {n:01d}AB",
    "card_number": "4242424242424242",
    "card_expiry": "04/29",
    "card_cvc": "123",
}


def load_personas() -> dict[str, Any]:
    return json.loads(PERSONAS_PATH.read_text())


class EventLog:
    """Buffers a session's events so every session is written atomically, and every
    session is guaranteed to end with exactly one `session_end`."""

    def __init__(self, path: Path, round_no: int, variant: str) -> None:
        self.path = path
        self.round = round_no
        self.variant = variant
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self._last_ts: float | None = None

    def emit(self, session_id: str, persona: str, event: str, step: str,
             element: str | None = None, meta: dict[str, Any] | None = None) -> None:
        now = time.time()
        duration_ms = 0 if self._last_ts is None else int((now - self._last_ts) * 1000)
        self._last_ts = now
        rec = {
            "ts": round(now, 3),
            "round": self.round,
            "variant": self.variant,
            "session_id": session_id,
            "persona": persona,
            "event": event,
            "step": step,
            "element": element,
            "duration_ms": duration_ms,
            "meta": meta or {},
        }
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def start_session(self) -> None:
        self._last_ts = None

    def close(self) -> None:
        self._fh.flush()
        self._fh.close()


def weighted_personas(cfg: dict[str, Any], split: str, n: int, rng: random.Random) -> Iterator[dict[str, Any]]:
    """Deterministic persona stream. For `train` the mix comes from train_mix; for
    `holdout` every holdout persona is used uniformly."""
    by_id = {p["id"]: p for p in cfg["personas"]}
    if split == "holdout":
        pool = [p for p in cfg["personas"] if p.get("split") == "holdout"] or cfg["personas"]
        for i in range(n):
            yield pool[i % len(pool)]
        return
    mix = cfg["train_mix"]
    ids = list(mix.keys())
    weights = [mix[i] for i in ids]
    for _ in range(n):
        yield by_id[rng.choices(ids, weights=weights, k=1)[0]]


def visible_required_fields(page: Any) -> list[Any]:
    out = []
    for handle in page.query_selector_all(FIELD):
        try:
            if handle.get_attribute("data-required") == "true" and handle.is_visible():
                out.append(handle)
        except PlaywrightError:
            continue
    return out


def visible_fields(page: Any) -> list[Any]:
    out = []
    for handle in page.query_selector_all(FIELD):
        try:
            if handle.is_visible():
                out.append(handle)
        except PlaywrightError:
            continue
    return out


def fill_fields(page: Any, session_no: int, log: EventLog, sid: str, persona: str,
                step: str, leave_one_blank: bool) -> None:
    """Fill every visible field. When `leave_one_blank` is set the persona fat-fingers
    one required field, which is what triggers the app's validation path."""
    fields = visible_fields(page)
    blank_target = None
    if leave_one_blank:
        required = [f for f in fields if f.get_attribute("data-required") == "true"]
        if required:
            blank_target = required[-1]
    for handle in fields:
        testid = handle.get_attribute("data-testid") or "field-unknown"
        name = testid[len("field-"):]
        if handle is blank_target:
            continue
        template = SAMPLE_VALUES.get(name, "Nimbus {n}")
        try:
            handle.fill(template.format(n=session_no))
            log.emit(sid, persona, "input", step, testid)
        except PlaywrightError:
            continue


def click(page: Any, selector: str, log: EventLog, sid: str, persona: str, step: str,
          retries: int) -> str:
    """Click, distinguishing "the click was intercepted" from "the click worked".

    A click that another element covers is exactly the third planted defect, and it is
    the most valuable signal in the whole system — so it gets its own event type
    rather than being folded into a generic timeout."""
    testid = selector.split('"')[1] if '"' in selector else selector
    for attempt in range(retries + 1):
        try:
            page.click(selector, timeout=CLICK_TIMEOUT_MS)
            log.emit(sid, persona, "click", step, testid, {"attempt": attempt + 1})
            return "ok"
        except (PlaywrightTimeout, PlaywrightError) as exc:
            message = str(exc).split("\n")[0][:200]
            intercepted = "intercepts pointer events" in str(exc) or "not stable" in str(exc)
            log.emit(sid, persona, "click_blocked", step, testid, {
                "attempt": attempt + 1,
                "intercepted": intercepted,
                "error": message,
            })
    return "blocked"


def run_session(page: Any, persona: dict[str, Any], sid: str, session_no: int,
                base_url: str, log: EventLog, cfg: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    pid = persona["id"]
    log.start_session()
    started = time.time()
    steps_seen = 0
    errors = 0
    last_step = "landing"
    outcome = "abandoned"
    abandon_reason: str | None = None
    delay_lo, delay_hi = persona["step_delay_ms"]
    too_many_prob = cfg.get("abandon_on_too_many_fields_prob", 0.7)
    retries = cfg.get("max_blocked_click_retries", 2)

    try:
        page.goto(base_url + "/", timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
    except PlaywrightError as exc:
        log.emit(sid, pid, "http_error", "landing", None, {"error": str(exc)[:200]})
        log.emit(sid, pid, "session_end", "landing", None, {
            "outcome": "abandoned", "last_step": "landing", "total_ms": 0,
            "steps": 0, "errors": 1, "abandon_reason": "http_error",
        })
        return {"outcome": "abandoned", "persona": pid}

    for _ in range(MAX_STEPS):
        # Human-ish dwell time, scaled down so a full round finishes in a demo slot.
        page.wait_for_timeout(min(150, rng.randint(delay_lo, delay_hi) // 10))

        body = page.query_selector("body")
        step = (body.get_attribute("data-step") if body else None) or "unknown"
        last_step = step
        steps_seen += 1
        log.emit(sid, pid, "page_view", step)

        if step == "confirmation":
            outcome = "converted"
            log.emit(sid, pid, "purchase", step, None, {"persona": pid})
            break

        guest = page.query_selector(GUEST)

        # Rules are evaluated in the order the interface document numbers them, and
        # that order is load-bearing: a persona who would balk both at the length of
        # the form and at the forced account is attributed to whichever it meets
        # first, which is the wall of fields already on screen.

        # Rule 1: too much asked at once.
        required = visible_required_fields(page)
        if len(required) > persona["max_fields_per_form"]:
            if rng.random() < too_many_prob:
                log.emit(sid, pid, "abandon", step, None, {
                    "reason": "too_many_fields",
                    "field_count": len(required),
                    "max_tolerated": persona["max_fields_per_form"],
                })
                log.emit(sid, pid, "session_end", step, None, {
                    "outcome": "abandoned", "last_step": step,
                    "total_ms": int((time.time() - started) * 1000),
                    "steps": steps_seen, "errors": errors, "abandon_reason": "too_many_fields",
                })
                return {"outcome": "abandoned", "persona": pid}

        # Rule 2: forced account creation with no way around it.
        if step == "signup" and guest is None:
            if rng.random() < persona["abandon_on_forced_signup_prob"]:
                abandon_reason = "forced_signup"
                break

        # Rule 3: a guest path, if this persona wants one, beats any account form.
        if guest is not None and persona["prefers_guest"]:
            if click(page, GUEST, log, sid, pid, step, retries) == "blocked":
                abandon_reason = "blocked"
                break
            log.emit(sid, pid, "step_complete", step, "guest-checkout")
            continue

        # Rule 5: a typo on a required field, corrected after the app complains.
        typo = bool(required) and rng.random() < persona["typo_rate"]
        if required or visible_fields(page):
            fill_fields(page, session_no, log, sid, pid, step, leave_one_blank=typo)

        if page.query_selector(PRIMARY) is None:
            abandon_reason = "no_primary_action"
            break

        # Rule 4: the click itself may be intercepted by another element.
        if click(page, PRIMARY, log, sid, pid, step, retries) == "blocked":
            abandon_reason = "blocked"
            break

        try:
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except PlaywrightError:
            pass

        if page.query_selector('[data-testid="validation-error"]') is not None:
            errors += 1
            log.emit(sid, pid, "validation_error", step, None, {"corrected": True})
            fill_fields(page, session_no, log, sid, pid, step, leave_one_blank=False)
            if click(page, PRIMARY, log, sid, pid, step, retries) == "blocked":
                abandon_reason = "blocked"
                break
            try:
                page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except PlaywrightError:
                pass
            if page.query_selector('[data-testid="validation-error"]') is not None:
                abandon_reason = "validation_loop"
                break

        log.emit(sid, pid, "step_complete", step, "primary-action")

    if outcome == "abandoned" and abandon_reason:
        log.emit(sid, pid, "abandon", last_step, None, {"reason": abandon_reason})
    log.emit(sid, pid, "session_end", last_step, None, {
        "outcome": outcome,
        "last_step": last_step,
        "total_ms": int((time.time() - started) * 1000),
        "steps": steps_seen,
        "errors": errors,
        "abandon_reason": abandon_reason,
    })
    return {"outcome": outcome, "persona": pid}


def simulate(round_no: int, variant: str, n: int, seed: int, base_url: str,
             out_path: Path, split: str = "train") -> dict[str, Any]:
    cfg = load_personas()
    # Seed is derived from (seed, variant, round) so the two arms get independent but
    # reproducible user streams — reusing one stream across arms would correlate them.
    rng = random.Random(f"{seed}:{variant}:{round_no}:{split}")
    log = EventLog(out_path, round_no, variant)
    results: list[dict[str, Any]] = []

    with sync_playwright() as pw:
        launch_kwargs: dict[str, Any] = {"args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if CHROME_PATH:
            launch_kwargs["executable_path"] = CHROME_PATH
        browser = pw.chromium.launch(**launch_kwargs)
        # One context per persona, reused across that persona's sessions. Creating a
        # context costs about as much as a whole session, and cookies are cleared
        # between sessions so the sessions stay independent.
        contexts: dict[str, Any] = {}
        try:
            for i, persona in enumerate(weighted_personas(cfg, split, n, rng)):
                pid = persona["id"]
                if pid not in contexts:
                    w, h = persona["viewport"]
                    ctx = browser.new_context(
                        viewport={"width": w, "height": h},
                        is_mobile=persona["device"] == "mobile",
                        has_touch=persona["device"] in ("mobile", "tablet"),
                    )
                    pg = ctx.new_page()
                    pg.set_default_timeout(CLICK_TIMEOUT_MS)
                    contexts[pid] = (ctx, pg)
                context, page = contexts[pid]
                context.clear_cookies()
                sid = f"s_{i:04d}"
                try:
                    results.append(run_session(page, persona, sid, i, base_url, log, cfg, rng))
                except Exception as exc:  # noqa: BLE001 — one bad session must not kill the round
                    log.emit(sid, pid, "http_error", "unknown", None, {"error": str(exc)[:200]})
                    log.emit(sid, pid, "session_end", "unknown", None, {
                        "outcome": "abandoned", "last_step": "unknown", "total_ms": 0,
                        "steps": 0, "errors": 1, "abandon_reason": "simulator_error",
                    })
                    results.append({"outcome": "abandoned", "persona": pid})
        finally:
            for ctx, _pg in contexts.values():
                try:
                    ctx.close()
                except PlaywrightError:
                    pass
            browser.close()
            log.close()

    converted = sum(1 for r in results if r["outcome"] == "converted")
    return {
        "variant": variant,
        "n": len(results),
        "conversions": converted,
        "conversion_rate": round(converted / max(1, len(results)), 4),
        "log": str(out_path),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Persona-driven Playwright simulator")
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--variant", choices=["control", "treatment"], required=True)
    ap.add_argument("--n", type=int, default=80)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--split", choices=["train", "holdout"], default="train")
    ap.add_argument("--out")
    args = ap.parse_args()

    suffix = "" if args.split == "train" else f".{args.split}"
    out = Path(args.out) if args.out else ROOT / "data" / "logs" / f"round_{args.round}" / f"{args.variant}{suffix}.jsonl"
    summary = simulate(args.round, args.variant, args.n, args.seed, args.base_url, out, args.split)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
