"""Vercel serverless entrypoint.

Thin re-export of the canonical ASGI app in `asgi.py` at the repo root.
Vercel's `functions` config requires the entrypoint to live inside `api/`;
all application logic stays in `asgi.py` / `app.py` / `backend/*`.
"""

import os
import sys

# Ensure repo root is importable so `asgi`, `app`, and `backend/*` resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asgi import app  # noqa: E402,F401  (Vercel's Python runtime serves this ASGI `app`)
