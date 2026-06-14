"""
Clerk authentication helpers for Sh'elah.

Extracted verbatim from ``app.py`` (Phase 1, Step 1 of the zero-breakage
backend split). This module owns direct Clerk JWT verification/handling:
the cached JWKS client and bearer-token verification.

``app.py`` re-imports these symbols so existing consumers — including the
async path in ``asgi.py`` (``flask_app_module._verify_clerk_token``) — keep
working unchanged.

Circular-import note: the previous version imported CLERK_JWT_ISSUER and
CLERK_AUDIENCE from ``app``, creating a fragile load-order dependency.
Both values are straightforward env-var reads, so this module reads them
directly — identical logic, zero coupling to ``app.py``.
"""

import os
from functools import wraps

import jwt
from flask import g, jsonify, request

# Read Clerk config directly from env — mirrors the identically-named
# constants in app.py (same env-var names, same fallback logic).
CLERK_JWT_ISSUER: str = (os.environ.get("CLERK_JWT_ISSUER") or "").strip().rstrip("/")
CLERK_AUDIENCE: str = (os.environ.get("CLERK_AUDIENCE") or "").strip()

_clerk_jwks_client = None


def _get_clerk_jwks_client():
    global _clerk_jwks_client
    # Re-read env on each miss so that a late-loaded .env (e.g. via dotenv in
    # app.py) is picked up without a module reload.
    issuer = (os.environ.get("CLERK_JWT_ISSUER") or "").strip().rstrip("/")
    if not issuer:
        return None
    if _clerk_jwks_client is None:
        jwks_url = f"{issuer}/.well-known/jwks.json"
        _clerk_jwks_client = jwt.PyJWKClient(jwks_url)
    return _clerk_jwks_client


def _verify_clerk_token(token):
    if not token:
        raise ValueError("Missing bearer token")
    issuer = (os.environ.get("CLERK_JWT_ISSUER") or "").strip().rstrip("/")
    if not issuer:
        raise ValueError("Server missing CLERK_JWT_ISSUER")

    jwks_client = _get_clerk_jwks_client()
    if jwks_client is None:
        raise ValueError("Clerk JWKS client unavailable")

    signing_key = jwks_client.get_signing_key_from_jwt(token).key
    audience = (os.environ.get("CLERK_AUDIENCE") or "").strip()
    decode_kwargs = {
        "algorithms": ["RS256"],
        "issuer": issuer,
    }
    if audience:
        decode_kwargs["audience"] = audience
    else:
        decode_kwargs["options"] = {"verify_aud": False}

    return jwt.decode(token, signing_key, **decode_kwargs)


# ── Auth enforcement helpers ──────────────────────────────────────────────────

_in_prod_runtime = (
    os.environ.get("VERCEL") == "1"
    or os.environ.get("FLASK_ENV", "").strip().lower() == "production"
)
CLERK_ENFORCE_AUTH: bool = (
    os.environ.get("CLERK_ENFORCE_AUTH")
    or ("true" if _in_prod_runtime else "false")
).strip().lower() == "true"


def _extract_bearer_token():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    return token or None


def maybe_require_clerk_auth(route_fn):
    @wraps(route_fn)
    def wrapped(*args, **kwargs):
        token = _extract_bearer_token()
        if not token:
            if CLERK_ENFORCE_AUTH:
                return jsonify({"error": "Authentication required"}), 401
            return route_fn(*args, **kwargs)
        try:
            g.clerk_claims = _verify_clerk_token(token)
        except Exception:
            return jsonify({"error": "Invalid or expired Clerk token"}), 401
        return route_fn(*args, **kwargs)
    return wrapped


def require_clerk_auth(route_fn):
    @wraps(route_fn)
    def wrapped(*args, **kwargs):
        token = _extract_bearer_token()
        if not token:
            return jsonify({"error": "Authentication required"}), 401
        try:
            g.clerk_claims = _verify_clerk_token(token)
        except Exception:
            return jsonify({"error": "Invalid or expired Clerk token"}), 401
        return route_fn(*args, **kwargs)
    return wrapped
