"""Capture the before/after pair for each round, from that round's own trees.

Run artifacts are gitignored, so the page a judge reads needs its images committed.
They are still real captures rather than mockups: for round N the "before" tree is
`data/runs/round_N/app_before_adopt` — the exact baseline that round's control arm
served — and the "after" tree is `data/runs/round_N/candidate_snapshot`, the exact
candidate its treatment arm served.

Each pair is framed on the thing the round changed, which for the mobile pay bar
means scrolled to the bottom of the payment page: the defect is that the button is
underneath the bar, and a screenshot of the top of the page would not show it.

  python docs/capture_shots.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "data" / "runs"
OUT = Path(__file__).resolve().parent / "shots"

# round -> (path, viewport, mobile?, scroll to bottom?)
FRAMES = {
    1: ("/signup", (1100, 980), False, False),
    2: ("/signup", (390, 844), True, False),
    3: ("/payment", (390, 844), True, True),
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Served:
    def __init__(self, tree: Path) -> None:
        self.tree = tree
        self.port = free_port()
        self.proc: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "Served":
        env = dict(os.environ, PORT=str(self.port), PYTHONDONTWRITEBYTECODE="1")
        self.proc = subprocess.Popen(
            [sys.executable, str(self.tree / "server.py")], env=env, cwd=str(self.tree),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.url}/healthz", timeout=2) as r:
                    if r.status == 200:
                        return self
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.3)
        raise RuntimeError(f"{self.tree} did not come up")

    def __exit__(self, *_exc: object) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)


def shoot(browser, url: str, path: str, size: tuple[int, int], mobile: bool,
          to_bottom: bool, out: Path) -> None:
    w, h = size
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile,
                              has_touch=mobile, device_scale_factor=2)
    page = ctx.new_page()
    page.goto(url + path, wait_until="domcontentloaded")
    page.wait_for_timeout(350)
    if to_bottom:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(250)
    page.screenshot(path=str(out), full_page=not to_bottom and not mobile)
    ctx.close()


def main() -> None:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for rnd, (path, size, mobile, to_bottom) in FRAMES.items():
                for phase, tree in (("before", RUNS / f"round_{rnd}" / "app_before_adopt"),
                                    ("after", RUNS / f"round_{rnd}" / "candidate_snapshot")):
                    if not tree.exists():
                        print(f"skip round {rnd} {phase}: {tree} missing (run the loop first)")
                        continue
                    with Served(tree) as inst:
                        out = OUT / f"r{rnd}_{phase}.png"
                        shoot(browser, inst.url, path, size, mobile, to_bottom, out)
                        print(f"round {rnd} {phase} -> {out.relative_to(ROOT)}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
