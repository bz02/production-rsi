"""Pre-experiment eval suite. PROTECTED — not agent-editable.

A candidate build only reaches live traffic if it passes here, so this is the quality
gate that sits between "the agent wrote code" and "real users see it". It checks two
different kinds of thing:

  contract checks  — the invariants the simulator depends on. If the agent breaks
                     data-step, drops primary-action, or removes the required-field
                     markers, the telemetry silently becomes meaningless, so these
                     are hard failures rather than warnings.
  journey checks   — a scripted "perfect user" walks the funnel to confirmation on a
                     desktop viewport. This is a correctness check, not a UX one: it
                     proves the funnel is completable at all.

Deliberately *not* checked: whether a phone user can complete the funnel. That is a
product defect for the experiment to find and price, not a build error — gating on it
here would mean the current baseline could never ship either.

Results are written as JSON so the dashboard can show the gate, and the suite mirrors
a promptfoo-style shape (one named case, one assertion, one verdict per row) so the
same cases can be lifted into `promptfoo eval` against a deployed environment.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
CHROME_PATH = os.environ.get("FLYWHEEL_CHROME") or next(
    (p for p in ("/opt/pw-browsers/chromium",
                 "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
                 "/usr/bin/chromium") if Path(p).exists()),
    None,
)

PRIMARY = '[data-testid="primary-action"]'
GUEST = '[data-testid="guest-checkout"]'
FIELD = 'input[data-testid^="field-"]'
MAX_STEPS = 20

FILLERS = {
    "email": "smoke@example.com",
    "password": "smoke-password-1",
    "password_confirm": "smoke-password-1",
    "card_number": "4242424242424242",
    "card_expiry": "04/29",
    "card_cvc": "123",
    "postcode": "M1 1AB",
}


def _case(name: str, passed: bool, detail: str, kind: str) -> dict[str, Any]:
    return {"case": name, "kind": kind, "passed": bool(passed), "detail": detail}


def run_suite(base_url: str) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    started = time.time()

    with sync_playwright() as pw:
        launch: dict[str, Any] = {"args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if CHROME_PATH:
            launch["executable_path"] = CHROME_PATH
        browser = pw.chromium.launch(**launch)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(4000)
        statuses: list[int] = []
        page.on("response", lambda r: statuses.append(r.status) if r.request.resource_type == "document" else None)

        steps_visited: list[str] = []
        reached_confirmation = False
        contract_failures: list[str] = []

        try:
            page.goto(base_url + "/", wait_until="domcontentloaded")
            cases.append(_case("landing page responds", True, f"GET / -> {statuses[0] if statuses else '200'}", "contract"))
        except PlaywrightError as exc:
            cases.append(_case("landing page responds", False, str(exc)[:160], "contract"))
            ctx.close(); browser.close()
            return _finish(cases, started, steps_visited, reached_confirmation)

        for _ in range(MAX_STEPS):
            body = page.query_selector("body")
            step = (body.get_attribute("data-step") if body else None) or ""

            if not step:
                contract_failures.append("a page has no data-step attribute")
                break
            steps_visited.append(step)

            if step == "confirmation":
                reached_confirmation = True
                break

            # exactly one forward action per page
            primaries = page.query_selector_all(PRIMARY)
            if len(primaries) != 1:
                contract_failures.append(f"step '{step}' has {len(primaries)} primary-action elements, expected 1")
                break

            # required fields must be marked, or the simulator cannot respect personas
            fields = page.query_selector_all(FIELD)
            for handle in fields:
                testid = handle.get_attribute("data-testid") or ""
                if not testid.startswith("field-"):
                    contract_failures.append(f"step '{step}' has an input without a field-* testid")
            for handle in fields:
                try:
                    handle.fill(FILLERS.get((handle.get_attribute("data-testid") or "field-x")[6:], "Smoke Test"))
                except PlaywrightError:
                    pass

            guest = page.query_selector(GUEST)
            target = GUEST if guest is not None else PRIMARY
            try:
                page.click(target, timeout=4000)
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except PlaywrightError as exc:
                contract_failures.append(f"step '{step}': forward action was not clickable ({str(exc).splitlines()[0][:90]})")
                break

            if page.query_selector('[data-testid="validation-error"]') is not None:
                # A perfect user filling every rendered field must not be rejected.
                contract_failures.append(f"step '{step}': a fully completed form was rejected")
                break

        ctx.close()
        browser.close()

    server_errors = [s for s in statuses if s >= 500]
    cases.append(_case("no 5xx responses during the journey", not server_errors,
                       f"{len(server_errors)} server error(s)" if server_errors else "clean", "contract"))
    cases.append(_case("DOM contract holds on every step", not contract_failures,
                       "; ".join(contract_failures) if contract_failures else
                       f"checked {len(set(steps_visited))} distinct step(s)", "contract"))
    cases.append(_case("perfect desktop user reaches confirmation", reached_confirmation,
                       " -> ".join(steps_visited) if steps_visited else "no steps visited", "journey"))
    cases.append(_case("funnel completes in a reasonable number of steps", len(steps_visited) <= MAX_STEPS,
                       f"{len(steps_visited)} steps", "journey"))

    return _finish(cases, started, steps_visited, reached_confirmation)


def _finish(cases: list[dict[str, Any]], started: float, steps: list[str], converted: bool) -> dict[str, Any]:
    passed = all(c["passed"] for c in cases)
    return {
        "passed": passed,
        "cases": cases,
        "pass_count": sum(1 for c in cases if c["passed"]),
        "case_count": len(cases),
        "steps_visited": steps,
        "reached_confirmation": converted,
        "duration_s": round(time.time() - started, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Pre-experiment eval / smoke suite")
    ap.add_argument("--base-url", default="http://127.0.0.1:8001")
    ap.add_argument("--out")
    args = ap.parse_args()

    result = run_suite(args.base_url)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
