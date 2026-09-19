"""Dashboard server. PROTECTED — not agent-editable.

Intentionally tiny: the orchestrator owns all state, and `data/state.json` is the
single source of truth the page polls. The only write endpoints are the two human
decisions the policy requires — approve and reject — and both delegate to the
orchestrator so that a merge always happens through the same code path.

  python dashboard/server.py            # http://127.0.0.1:8080
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
from pathlib import Path

from flask import Flask, Response, jsonify, send_from_directory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator.run_loop import apply_approval  # noqa: E402

DATA = ROOT / "data"
HERE = Path(__file__).resolve().parent

app = Flask(__name__, static_folder=None)


@app.after_request
def no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/")
def index() -> Response:
    return send_from_directory(HERE, "index.html")


@app.route("/app.css")
def css() -> Response:
    return send_from_directory(HERE, "app.css")


@app.route("/app.js")
def js() -> Response:
    return send_from_directory(HERE, "app.js")


@app.route("/api/state")
def state() -> Response:
    path = DATA / "state.json"
    if not path.exists():
        return jsonify({"current_round": 0, "phase": "idle", "conversion_series": [],
                        "rounds": [], "timeline": [], "empty": True})
    return Response(path.read_text(), mimetype="application/json")


@app.route("/api/metrics/<int:round_no>")
def metrics(round_no: int) -> Response:
    path = DATA / "metrics" / f"round_{round_no}.json"
    if not path.exists():
        return jsonify({"error": "not found"}), 404
    return Response(path.read_text(), mimetype="application/json")


@app.route("/api/file/<path:rel>")
def data_file(rel: str) -> Response:
    """Serve a run artifact (diff, change.md, screenshot) from data/ read-only."""
    target = (DATA / rel).resolve()
    if not str(target).startswith(str(DATA.resolve()) + os.sep) or not target.exists():
        return jsonify({"error": "not found"}), 404
    guessed = mimetypes.guess_type(target.name)[0]
    if target.suffix in (".patch", ".md", ".diff", ".txt"):
        guessed = "text/plain; charset=utf-8"
    return Response(target.read_bytes(), mimetype=guessed or "application/octet-stream")


@app.route("/api/approve/<int:round_no>", methods=["POST"])
def approve(round_no: int) -> Response:
    result = apply_approval(round_no, approve=True)
    return jsonify(result), (200 if result.get("ok") else 409)


@app.route("/api/reject/<int:round_no>", methods=["POST"])
def reject(round_no: int) -> Response:
    result = apply_approval(round_no, approve=False)
    return jsonify(result), (200 if result.get("ok") else 409)


if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    print(f"dashboard on http://127.0.0.1:{port}", flush=True)
    app.run(host="127.0.0.1", port=port, threaded=True)
