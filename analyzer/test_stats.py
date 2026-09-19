"""Checks on the decision-critical arithmetic. Run with: python analyzer/test_stats.py

The interface document fixes n=80 per arm on the grounds that a 30% -> 45% move
lands at p ~= 0.05. That claim is the reason the sample size is what it is, so it is
worth asserting rather than trusting.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.analyze import classify_autonomy, load_policy, two_proportion_z  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  pass  {name}")
    else:
        failures.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


print("two-proportion z-test")

# The sample-size claim the interface document is built on.
r = two_proportion_z(24, 80, 36, 80)
check("30% vs 45% at n=80 is significant at ~0.05",
      0.045 <= r["p_value"] <= 0.055, f"p={r['p_value']}")
check("absolute lift is +15pp", abs(r["lift_abs"] - 0.15) < 1e-9, f"{r['lift_abs']}")
check("relative lift is +50%", abs(r["lift_rel"] - 0.5) < 1e-9, f"{r['lift_rel']}")
check("CI excludes zero when significant", r["ci95"][0] > 0, f"{r['ci95']}")

# No difference at all must not be significant.
flat = two_proportion_z(24, 80, 24, 80)
check("identical arms give p=1", abs(flat["p_value"] - 1.0) < 1e-6, f"p={flat['p_value']}")
check("identical arms give zero lift", flat["lift_abs"] == 0.0, f"{flat['lift_abs']}")
check("identical arms CI spans zero", flat["ci95"][0] < 0 < flat["ci95"][1], f"{flat['ci95']}")

# A real move that is simply underpowered must not clear the gate.
small = two_proportion_z(3, 16, 6, 16)
check("+18.8pp at n=16 is not significant", small["p_value"] > 0.10, f"p={small['p_value']}")

# Direction is preserved.
down = two_proportion_z(36, 80, 24, 80)
check("a negative move reports a negative lift", down["lift_abs"] < 0, f"{down['lift_abs']}")
check("p-value is symmetric in direction", abs(down["p_value"] - r["p_value"]) < 1e-9,
      f"{down['p_value']} vs {r['p_value']}")

# Degenerate inputs must not raise.
check("empty arms are handled", two_proportion_z(0, 0, 0, 0)["p_value"] == 1.0)
check("zero-conversion control is handled", two_proportion_z(0, 80, 8, 80)["lift_rel"] == 0.0)


print("\npolicy classification")
policy = load_policy()
tmp = Path(__file__).resolve().parent / "_test_diff.patch"


def classify(diff_text: str) -> dict:
    tmp.write_text(diff_text)
    try:
        return classify_autonomy(tmp, policy)
    finally:
        tmp.unlink(missing_ok=True)


template_only = """diff -ru app/templates/signup.html candidate/templates/signup.html
--- app/templates/signup.html
+++ candidate/templates/signup.html
@@ -1,3 +1,3 @@
-  {% for f in fields %}
+  {% for f in fields[:4] %}
"""
v = classify(template_only)
check("a template-only diff auto-adopts", v["verdict"] == "auto", v["verdict"])
check("and reports no protected hits", not v["protected_paths_touched"])

server_change = template_only + """diff -ru app/server.py candidate/server.py
--- app/server.py
+++ candidate/server.py
@@ -1,2 +1,6 @@
+@app.route("/guest")
+def guest():
+    session["guest"] = True
"""
v = classify(server_change)
check("a server.py diff needs approval", v["verdict"] == "pending_approval", v["verdict"])
check("and names the path that triggered it",
      any("server.py" in p for p in v["needs_approval_hits"]), str(v["needs_approval_hits"]))

# The anti-cheat case: the agent must not be able to edit its own evaluator.
evaluator_change = """diff -ru analyzer/analyze.py candidate/analyze.py
--- analyzer/analyze.py
+++ candidate/analyze.py
@@ -1,2 +1,2 @@
-P_THRESHOLD = 0.10
+P_THRESHOLD = 1.00
"""
v = classify(evaluator_change)
check("editing the analyzer is invalid", v["verdict"] == "invalid", v["verdict"])
check("and is reported as a protected-path hit", v["protected_paths_touched"])

# The header line a real run produces, flags and all. Reading the first token after
# `diff -ru` as a filename invented `--exclude=__pycache__`, which is outside the
# auto-adopt allowlist, so a clean templates-only round asked for a human instead of
# shipping itself. That is the autonomy claim failing quietly, so it gets a test.
real_header = """diff -ru --exclude=__pycache__ --exclude=*.pyc app/templates/signup.html candidate/templates/signup.html
--- app/templates/signup.html	2026-09-19 12:00:00.000000000 +0100
+++ candidate/templates/signup.html	2026-09-19 12:05:00.000000000 +0100
@@ -1,3 +1,3 @@
-  {% for f in fields %}
+  {% for f in fields[:4] %}
"""
v = classify(real_header)
check("diff flags are not mistaken for filenames",
      v["files"] == ["app/templates/signup.html"], str(v["files"]))
check("a real diff header still auto-adopts", v["verdict"] == "auto", v["verdict"])

oversized = "".join(
    f"diff -ru app/templates/p{i}.html candidate/templates/p{i}.html\n+line\n" for i in range(9)
)
v = classify(oversized)
check("too many files needs approval", v["verdict"] == "pending_approval", v["verdict"])
check("and flags the file limit", v["over_file_limit"])


print()
if failures:
    print(f"{len(failures)} check(s) failed")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all checks passed")
