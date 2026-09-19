"""Checks on the agent's sandbox. Run with: python agent/test_tools.py

The whole credibility of the loop rests on one claim: the agent cannot reach its own
evaluator. `analyzer/test_stats.py` asserts the second enforcement point — the
analyzer marks a diff that touched a protected path `invalid`. This file asserts the
first one: `write_file` refuses the path before anything is written at all.

Two enforcement points in two processes only count as two if both are tested. The
escape attempts below are the ones that actually work against a naive prefix check:
`..` traversal, an absolute path, and a symlink pointing out of the sandbox.

No Flask, no Playwright, no network — this runs anywhere `python` does.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Loaded by path rather than by name: running this file puts `agent/` on sys.path, so
# `import agent` would find agent.py itself rather than the directory it lives in.
_spec = importlib.util.spec_from_file_location("flywheel_agent", ROOT / "agent" / "agent.py")
_agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_agent)
Tools = _agent.Tools

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  pass  {name}")
    else:
        failures.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


def refuses(tools: Tools, path: str) -> bool:
    """A refusal is a raised PermissionError *and* nothing written to disk."""
    try:
        tools.write_file(path, "pwned")
    except (PermissionError, OSError):
        return True
    return False


def read_refused(tools: Tools, path: str) -> bool:
    """`read_file` goes through the same guard; an unreadable path must raise rather
    than hand back the contents of a file outside the sandbox."""
    try:
        tools.read_file(path)
    except (PermissionError, OSError):
        return True
    return False


print("write_file sandbox")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    candidate = root / "candidate"
    (candidate / "templates").mkdir(parents=True)
    (candidate / "templates" / "signup.html").write_text("<form></form>")

    # Two files outside the sandbox that a real escape would land on.
    outside = root / "analyze.py"
    outside.write_text("P_THRESHOLD = 0.10\n")
    (root / "personas.json").write_text("{}\n")

    tools = Tools(candidate)

    # The paths the README names, plus the two that a prefix-only check would miss.
    escapes = [
        ("../analyze.py", "../analyze.py"),
        ("../personas.json", "../personas.json"),
        ("../../etc/passwd", "../../etc/passwd"),
        ("/tmp/pwned.txt", "/tmp/pwned.txt"),
        ("templates/../../analyze.py", "traversal back out of a real subdirectory"),
        (str(outside), "an absolute path outside the sandbox"),
    ]
    for path, label in escapes:
        check(f"refuses {label}", refuses(tools, path))

    check("the file outside the sandbox is untouched",
          outside.read_text() == "P_THRESHOLD = 0.10\n", outside.read_text()[:40])
    check("no refusal was recorded as a write", tools.writes == [], str(tools.writes))

    # A symlink inside the sandbox pointing out of it: the guard resolves before it
    # compares, so the target is what is checked, not the link's own path.
    try:
        os.symlink(root, candidate / "escape")
    except OSError:
        print("  skip  symlinks unavailable on this filesystem")
    else:
        check("refuses a symlink that leaves the sandbox", refuses(tools, "escape/analyze.py"))
        check("the symlink target is still untouched", outside.read_text() == "P_THRESHOLD = 0.10\n")

    # And the permitted case still works, or the guard would be useless in a different way.
    tools.write_file("templates/signup.html", "<form>trimmed</form>")
    check("allows a write inside the sandbox",
          (candidate / "templates" / "signup.html").read_text() == "<form>trimmed</form>")
    tools.write_file("static/new.css", ".a{}")
    check("creates missing directories inside the sandbox",
          (candidate / "static" / "new.css").exists())
    check("records exactly the writes it allowed",
          tools.writes == ["templates/signup.html", "static/new.css"], str(tools.writes))

    print("\nread_file and list_dir observe the same boundary")
    check("read_file refuses to read outside the sandbox", read_refused(tools, "../analyze.py"))
    check("list_dir stays inside the sandbox",
          all(not p.startswith("..") for p in tools.list_dir(".")), str(tools.list_dir(".")))


print()
if failures:
    print(f"{len(failures)} check(s) failed")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all checks passed")
