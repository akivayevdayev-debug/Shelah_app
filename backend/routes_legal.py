"""
Legal-pages blueprint for Sh'elah.

plan.md §8.A: routes for the legal/disclosure pages added alongside the
existing terms/privacy/accessibility pages (which still live in app.py —
moving those is out of scope here). New routes belong in backend/ per
.agents/ENGINEERING_RULES.md, so these four follow that rule from the
start rather than being added to app.py.
"""

from flask import Blueprint, render_template

from app import (
    CLERK_PUBLISHABLE_KEY,
    CLERK_ENFORCE_AUTH,
)

routes_legal = Blueprint("legal", __name__)


@routes_legal.route("/ai-disclosure")
def ai_disclosure():
    return render_template(
        "ai-disclosure.html",
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
    )


@routes_legal.route("/acceptable-use")
def acceptable_use():
    return render_template(
        "acceptable-use.html",
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
    )


@routes_legal.route("/dmca")
def dmca():
    return render_template(
        "dmca.html",
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
    )


@routes_legal.route("/licenses")
def licenses():
    return render_template(
        "licenses.html",
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
    )
