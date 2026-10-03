"""
The Flask app is mounted inside FastAPI through a2wsgi's WSGIMiddleware.

Two properties of that middleware are load-bearing for this app, and neither
is visible from the route tests, so they are pinned here:

* Each request runs in a *copy* of the calling context. backend/cost_gates.py
  binds the caller's identity and budget reservation in contextvars; if a
  worker thread kept one request's context for the next, a later request's
  model spend would be attributed to (and settle the reservation of) the
  wrong caller.
* The worker count is the Flask side's concurrency ceiling. a2wsgi defaults
  to 10; the Starlette middleware it replaced was limited by anyio's default
  of 40, so asgi.py passes WSGI_WORKERS explicitly.
"""

from __future__ import annotations

import contextvars
import sys

from a2wsgi import WSGIMiddleware
from flask import Flask
from starlette.testclient import TestClient

import app as flask_app_module
import asgi

_marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="clean")


def test_flask_is_mounted_through_a2wsgi_with_the_documented_worker_ceiling() -> None:
    mount = asgi.fastapi_app.routes[-1]
    assert mount.path == ""  # Mount("/") normalises to the empty prefix
    assert isinstance(mount.app, WSGIMiddleware)
    assert mount.app.app is flask_app_module.app
    assert asgi.WSGI_WORKERS == 40
    assert mount.app.executor._max_workers == asgi.WSGI_WORKERS


def test_the_deprecated_starlette_wsgi_module_is_not_imported() -> None:
    assert "starlette.middleware.wsgi" not in sys.modules
    assert "fastapi.middleware.wsgi" not in sys.modules


def test_a_reused_worker_thread_does_not_carry_context_between_requests() -> None:
    probe_app = Flask(__name__)

    @probe_app.route("/probe")
    def probe() -> str:
        seen = _marker.get()
        _marker.set("dirty")
        return seen

    # One worker, so every request lands on the same thread. Without a
    # per-request context copy the second request would see "dirty".
    client = TestClient(WSGIMiddleware(probe_app, workers=1))
    assert client.get("/probe").text == "clean"
    assert client.get("/probe").text == "clean"
    assert client.get("/probe").text == "clean"
