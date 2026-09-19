"""Checks that the eval gate actually blocks. Run with: python sim/test_gate.py

Every other test in this repo runs on arithmetic. This one runs a browser, because
the claim it checks is not arithmetic: that a candidate which breaks the funnel never
reaches live traffic.

A gate nobody has watched fail is a gate nobody knows works, and this one has never
failed in a real round — the playbook only produces well-formed edits, which is the
point of the playbook. So the failures are manufactured here, in temporary copies of
`app/`, and the suite is asked to catch them:

  intact            -> passes, and walks all the way to confirmation
  no forward action -> the DOM contract case fails (the simulator would be blind)
  500 mid-funnel    -> the 5xx case fails
  unmarked field    -> the required-field markers case fails

`app/` itself is never touched. Each variant is a copy in a temp directory, served on
its own ephemeral port.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sim.smoke import run_suite  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  pass  {name}")
    else:
        failures.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Variant:
    """A throwaway copy of the app, optionally damaged, served on its own port."""

    def __init__(self, damage: Callable[[Path], None] | None = None) -> None:
        self.damage = damage
        self.port = free_port()
        self.tmp: tempfile.TemporaryDirectory | None = None
        self.proc: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "Variant":
        self.tmp = tempfile.TemporaryDirectory()
        tree = Path(self.tmp.name) / "app"
        shutil.copytree(ROOT / "app", tree, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if self.damage:
            self.damage(tree)
        env = dict(os.environ, PORT=str(self.port), PYTHONDONTWRITEBYTECODE="1")
        self.proc = subprocess.Popen([sys.executable, str(tree / "server.py")], env=env,
                                     cwd=str(tree), stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.url}/healthz", timeout=2) as r:
                    if r.status == 200:
                        return self
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.3)
        raise RuntimeError("variant did not come up")

    def __exit__(self, *_exc: object) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.tmp:
            self.tmp.cleanup()


def case(result: dict[str, Any], needle: str) -> dict[str, Any]:
    return next((c for c in result["cases"] if needle in c["case"]), {})


# ---------------------------------------------------------------------- damage

def drop_forward_action(tree: Path) -> None:
    """Remove the one thing the simulator clicks. Nothing 500s; the funnel simply
    becomes a dead end, which is the failure mode a human would miss in review."""
    p = tree / "templates" / "cart.html"
    p.write_text(p.read_text().replace('data-testid="primary-action"', 'data-x="was-primary"'))


def break_a_step(tree: Path) -> None:
    """A 500 halfway down the funnel."""
    p = tree / "server.py"
    p.write_text(p.read_text().replace(
        '@app.route("/shipping", methods=["GET", "POST"])\ndef shipping():',
        '@app.route("/shipping", methods=["GET", "POST"])\ndef shipping():\n'
        '    raise RuntimeError("deliberate failure for sim/test_gate.py")',
    ))


def unmark_required_fields(tree: Path) -> None:
    """Drop the field-* testids. The funnel still completes by hand, but the
    simulator can no longer see what a form asks for, so every persona rule that
    depends on field counts silently stops working."""
    p = tree / "templates" / "payment.html"
    p.write_text(p.read_text().replace('data-testid="field-{{ f.name }}"', ""))


# ----------------------------------------------------------------------- checks

print("eval gate")

with Variant() as v:
    intact = run_suite(v.url)
check("an intact candidate passes the gate", intact["passed"], str(intact["pass_count"]) + "/" + str(intact["case_count"]))
check("and reaches confirmation", intact["reached_confirmation"], str(intact["steps_visited"]))
check("and visits the whole funnel", len(intact["steps_visited"]) >= 6, str(intact["steps_visited"]))

with Variant(drop_forward_action) as v:
    dead_end = run_suite(v.url)
check("a candidate with no forward action is blocked", not dead_end["passed"], str(dead_end["pass_count"]))
check("and the DOM contract case is the one that fails",
      not case(dead_end, "DOM contract")["passed"], str(case(dead_end, "DOM contract")))
check("and it never reached confirmation", not dead_end["reached_confirmation"])

with Variant(break_a_step) as v:
    five_hundred = run_suite(v.url)
check("a candidate that 500s mid-funnel is blocked", not five_hundred["passed"], str(five_hundred["pass_count"]))
check("and the 5xx case is the one that fails",
      not case(five_hundred, "5xx")["passed"], str(case(five_hundred, "5xx")))

with Variant(unmark_required_fields) as v:
    unmarked = run_suite(v.url)
check("a candidate that breaks the field markers is blocked", not unmarked["passed"], str(unmarked["pass_count"]))
check("and the gate names the input it cannot see",
      "cannot see" in case(unmarked, "DOM contract").get("detail", ""),
      str(case(unmarked, "DOM contract")))


print()
if failures:
    print(f"{len(failures)} check(s) failed")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all checks passed")
