"""
Device identity for signed-out AI answers.

A signed-in answer is saved under the Clerk ``sub`` and its private link
(``/answer/<id>``) opens only for that account. A signed-out visitor has no
account, so the same link is tied to their *device* instead: the first signed-out
``/ask`` sets a random ``shelah_device`` cookie, and the answer is saved under
``device:<sha256 of that cookie>``. The link then opens in that browser and
nowhere else; signing in later keeps those answers reachable too (the routes
accept the account and the device together, see ``history_owners``).

The cookie is the secret, so it is HttpOnly and SameSite=Lax (a cross-site
request never carries it, which is what keeps the state-changing routes below
safe from CSRF), and only its hash is stored: a leaked table does not hand out
anyone's device. It carries no profile and no tracking id, and is never sent
anywhere but this site (docs/PRIVACY_OPERATIONS.md, templates/privacy.html).

``ask_history.user_id`` is plain TEXT and Clerk subs are ``user_...``, so the
``device:`` prefix cannot collide with an account and needs no migration.
"""

import contextvars
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from functools import wraps

from flask import g, has_request_context, jsonify, make_response, request

from backend import auth
from backend.logging_setup import _capture_backend_error

DEVICE_COOKIE = "shelah_device"
DEVICE_COOKIE_MAX_AGE = 365 * 24 * 60 * 60
OWNER_PREFIX = "device:"

_TOKEN_BYTES = 24  # 32 url-safe characters
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


def new_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def valid_token(token) -> bool:
    return bool(_TOKEN_RE.match(str(token or "")))


def owner_for_token(token) -> str | None:
    """The ``ask_history.user_id`` a device cookie maps to, or None when the
    cookie is missing or not shaped like one we issued."""
    if not valid_token(token):
        return None
    return OWNER_PREFIX + hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def is_device_owner(owner) -> bool:
    return str(owner or "").startswith(OWNER_PREFIX)


@dataclass
class DeviceBinding:
    """This request's device: its owner id and cookie value, and whether the
    cookie was minted now (so the response has to set it)."""

    owner: str
    token: str
    minted: bool
    reset: object = field(default=None, repr=False)


_binding: contextvars.ContextVar = contextvars.ContextVar("shelah_device_binding", default=None)


def bind_device(cookies) -> DeviceBinding:
    """Resolve the request's device from its cookies, minting one when the
    visitor has none. Makes it the current history owner for signed-out saves
    (``current_history_owner``); ``release_device`` undoes that."""
    token = (cookies or {}).get(DEVICE_COOKIE)
    minted = not valid_token(token)
    if minted:
        token = new_token()
    binding = DeviceBinding(owner=owner_for_token(token), token=token, minted=minted)
    binding.reset = _binding.set(binding)
    return binding


def release_device(binding) -> None:
    if binding is not None and binding.reset is not None:
        try:
            _binding.reset(binding.reset)
        except ValueError:
            # Released from another context (a worker thread): the binding
            # there was a copy and dies with it.
            pass


def current_history_owner() -> str | None:
    """The device owner a signed-out answer is saved under, or None outside a
    bound request. A signed-in answer never asks: the account always wins."""
    binding = _binding.get()
    return binding.owner if binding else None


def cookie_settings(secure: bool) -> dict:
    return {
        "key": DEVICE_COOKIE,
        "max_age": DEVICE_COOKIE_MAX_AGE,
        "path": "/",
        "httponly": True,
        "samesite": "lax",
        "secure": bool(secure),
    }


def request_is_secure(scheme, forwarded_proto) -> bool:
    """HTTPS in production, or whenever the request itself came over it."""
    proto = str(forwarded_proto or scheme or "").split(",")[0].strip().lower()
    return bool(auth._in_prod_runtime) or proto == "https"


def set_device_cookie(response, binding, *, secure: bool) -> None:
    """Attach the cookie to a Flask/Starlette response when this request
    minted it (an existing cookie is left alone)."""
    if binding is not None and binding.minted:
        response.set_cookie(value=binding.token, **cookie_settings(secure))


# ── Flask ──────────────────────────────────────────────────────────────────

def issues_device_cookie(view):
    """Flask /ask view wrapper: a request without a bearer token (a signed-out
    visitor) saves its answer under its device, and gets the cookie."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if auth._extract_bearer_token():
            return view(*args, **kwargs)
        binding = bind_device(request.cookies)
        try:
            response = view(*args, **kwargs)
            # Only a successful answer earns the cookie.
            response = make_response(response)
            if response.status_code < 400:
                set_device_cookie(
                    response, binding,
                    secure=request_is_secure(request.scheme, request.headers.get("X-Forwarded-Proto")),
                )
            return response
        finally:
            release_device(binding)

    return wrapped


def history_owners(user_id=None) -> tuple:
    """Every owner id the caller may open answers for: their account (if
    signed in) and the device their cookie names (if it has one)."""
    owners = []
    if user_id:
        owners.append(user_id)
    if has_request_context():
        device = owner_for_token(request.cookies.get(DEVICE_COOKIE))
        if device:
            owners.append(device)
    return tuple(owners)


def filter_owner(query, owners):
    """``.eq`` for one owner, ``.in_`` for an account plus its device."""
    owners = list(owners)
    if len(owners) == 1:
        return query.eq("user_id", owners[0])
    return query.in_("user_id", owners)


def require_history_owner(route_fn):
    """Like ``require_clerk_auth``, but a signed-out visitor holding a device
    cookie is accepted too. Sets ``g.history_owners`` (never empty) and, for a
    signed-in caller, ``g.clerk_claims``."""

    @wraps(route_fn)
    def wrapped(*args, **kwargs):
        user_id = None
        token = auth._extract_bearer_token()
        if token:
            try:
                g.clerk_claims = auth._verify_clerk_token(token)
            except Exception as exc:
                _capture_backend_error("clerk_auth_verify_failed", exc, {
                    "path": request.path,
                    "enforced": True,
                })
                return jsonify({"error": "Invalid or expired Clerk token"}), 401
            user_id = str(g.clerk_claims.get("sub") or "").strip() or None
        owners = history_owners(user_id)
        if not owners:
            return jsonify({"error": "Authentication required"}), 401
        g.history_owners = owners
        return route_fn(*args, **kwargs)

    return wrapped
