"""Demo app under improvement: the Nimbus Supply checkout funnel.

This is the ONLY tree the agent is allowed to edit (see agent/policy.json). It is a
server-rendered Flask app on purpose: templates are the easiest thing for an agent to
change safely, and every step of the funnel is one file.

DOM contract (sim/simulator.py depends on it; do not break it):
  * <body data-step="..."> matches the current funnel step
  * exactly one [data-testid="primary-action"] per page — the forward button
  * inputs are input[data-testid^="field-"]; required ones carry data-required="true"
  * [data-testid="guest-checkout"] is optional and absent in the baseline
  * the success page is data-step="confirmation"

Steps may be added or removed as long as data-step stays truthful; the simulator
falls back to "fill every required field, click primary-action" for unknown steps.
"""

from __future__ import annotations

import os
from flask import Flask, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET", "nimbus-demo-secret")

# Funnel order. The simulator does not hardcode this — it follows data-step — but the
# app uses it to decide where "next" goes.
FUNNEL = ["landing", "product", "cart", "signup", "shipping", "payment", "confirmation"]

PRODUCT = {
    "name": "Aurora Desk Lamp",
    "price": 68.00,
    "blurb": "Warm dimmable light with a matte aluminium arm. Ships in two days.",
    "sku": "NS-AUR-01",
}

# --- signup fields -----------------------------------------------------------------
# The baseline asks for nine fields before a customer may check out. Every one of them
# looked reasonable to somebody, which is exactly how forms end up like this.
SIGNUP_FIELDS = [
    {"name": "email", "label": "Email address", "type": "email", "required": True},
    {"name": "password", "label": "Choose a password", "type": "password", "required": True},
    {"name": "password_confirm", "label": "Confirm password", "type": "password", "required": True},
    {"name": "first_name", "label": "First name", "type": "text", "required": True},
    {"name": "last_name", "label": "Last name", "type": "text", "required": True},
    {"name": "company", "label": "Company (optional)", "type": "text", "required": False},
    {"name": "phone", "label": "Phone number", "type": "tel", "required": True},
    {"name": "referral", "label": "How did you hear about us?", "type": "text", "required": True},
    {"name": "newsletter_pref", "label": "Newsletter preference", "type": "text", "required": True},
]

SHIPPING_FIELDS = [
    {"name": "address1", "label": "Street address", "type": "text", "required": True},
    {"name": "city", "label": "City", "type": "text", "required": True},
    {"name": "postcode", "label": "Postcode", "type": "text", "required": True},
]

PAYMENT_FIELDS = [
    {"name": "card_number", "label": "Card number", "type": "text", "required": True},
    {"name": "card_expiry", "label": "Expiry (MM/YY)", "type": "text", "required": True},
    {"name": "card_cvc", "label": "CVC", "type": "text", "required": True},
]


def _ctx(step: str, **extra: object) -> dict[str, object]:
    return {
        "step": step,
        "product": PRODUCT,
        "cart_qty": session.get("qty", 1),
        "total": round(PRODUCT["price"] * session.get("qty", 1), 2),
        "signed_in": session.get("signed_in", False),
        "guest": session.get("guest", False),
        **extra,
    }


def _missing(fields: list[dict], form: dict) -> list[str]:
    return [f["name"] for f in fields if f["required"] and not (form.get(f["name"]) or "").strip()]


@app.after_request
def no_store(resp):  # noqa: ANN001, ANN201
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/")
def landing():
    session.clear()
    return render_template("landing.html", **_ctx("landing"))


@app.route("/product")
def product():
    return render_template("product.html", **_ctx("product"))


@app.route("/cart", methods=["GET", "POST"])
def cart():
    if request.method == "POST":
        session["qty"] = max(1, int(request.form.get("qty", 1) or 1))
        # D2: checkout is gated behind account creation. There is no guest path, so a
        # shopper who does not want an account has nowhere to go but away.
        if session.get("signed_in") or session.get("guest"):
            return redirect(url_for("shipping"))
        return redirect(url_for("signup"))
    session.setdefault("qty", 1)
    return render_template("cart.html", **_ctx("cart"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    errors: list[str] = []
    if request.method == "POST":
        errors = _missing(SIGNUP_FIELDS, request.form)
        if not errors:
            session["signed_in"] = True
            return redirect(url_for("shipping"))
    return render_template("signup.html", **_ctx("signup", fields=SIGNUP_FIELDS, errors=errors))


@app.route("/shipping", methods=["GET", "POST"])
def shipping():
    errors: list[str] = []
    if request.method == "POST":
        errors = _missing(SHIPPING_FIELDS, request.form)
        if not errors:
            return redirect(url_for("payment"))
    return render_template("shipping.html", **_ctx("shipping", fields=SHIPPING_FIELDS, errors=errors))


@app.route("/payment", methods=["GET", "POST"])
def payment():
    errors: list[str] = []
    if request.method == "POST":
        errors = _missing(PAYMENT_FIELDS, request.form)
        if not errors:
            session["order"] = "NS-" + str(abs(hash(session.get("qty", 1))) % 900000 + 100000)
            return redirect(url_for("confirmation"))
    return render_template("payment.html", **_ctx("payment", fields=PAYMENT_FIELDS, errors=errors))


@app.route("/confirmation")
def confirmation():
    return render_template("confirmation.html", **_ctx("confirmation", order=session.get("order", "NS-000000")))


@app.route("/healthz")
def healthz():
    return {"ok": True, "variant": os.environ.get("APP_VARIANT", "baseline")}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
