"""api/index.py is Vercel's serverless entrypoint: a thin re-export of the
canonical ASGI app. Executing it must expose that exact object as `app`
(Vercel's Python runtime serves whatever `app` is) and leave the repo root
importable."""

from __future__ import annotations

import os
import runpy
import sys

import asgi

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_entrypoint_exposes_the_canonical_asgi_app(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))

    namespace = runpy.run_path(os.path.join(_REPO_ROOT, "api", "index.py"))

    assert namespace["app"] is asgi.app


def test_the_entrypoint_puts_the_repo_root_first_on_sys_path(monkeypatch):
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != _REPO_ROOT])

    runpy.run_path(os.path.join(_REPO_ROOT, "api", "index.py"))

    assert sys.path[0] == _REPO_ROOT
