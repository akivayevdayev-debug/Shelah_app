"""Content-Security-Policy construction.

``script-src`` carries no ``'unsafe-inline'``. Two things make that possible:

* There are no inline event-handler attributes any more. Markup uses
  ``data-onclick`` / ``data-oninput``, which static/js/actions.js dispatches
  through an allowlist. ``script-src-attr 'none'`` makes the browser refuse
  any inline handler that gets added back, instead of silently running it
  (tests/test_inline_handlers.py fails the build first).
* The page's own inline ``<script>`` blocks are allowed by SHA-256 hash,
  computed here from the HTML that is actually being sent. A nonce would not
  do: the HTML shell is cached at the CDN (``public, max-age=300``), and a
  cached nonce is a nonce every visitor shares, which is no protection at all.
  A hash is a property of the bytes, so the cached header and the cached body
  stay in step.

Why a hash list and not 'self'-only: the shell has a handful of small inline
bootstraps (theme and language before first paint, Clerk config, the importmap)
that must run before any external file can load, and one large classic script.
Moving the large one out is its own refactor; hashing it costs one header
entry.

``style-src`` still allows ``'unsafe-inline'``. That is the second half of the
original refactor (74 ``style=`` attributes, ``<style>`` blocks, and the
runtime styles Clerk and FullCalendar inject) and is documented as open in
docs/SECURITY.md. It is a much smaller risk than script injection: injected
CSS cannot execute code, and ``default-src 'self'`` plus ``connect-src`` /
``img-src`` limit what it can exfiltrate to.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Iterable

# Hosts a <script src> may load from. The inline bootstraps are NOT here; they
# are admitted by hash.
SCRIPT_SRC_HOSTS = (
    "https://cdn.jsdelivr.net",
    "https://js.clerk.com",
    "https://clerk.com",
    "https://challenges.cloudflare.com",
    "https://browser.sentry-cdn.com",
)

# <script> element types that the browser executes (and CSP therefore checks).
# Anything else -- application/json, application/ld+json, a custom type -- is a
# data block: never run, never subject to script-src.
_EXECUTABLE_TYPES = frozenset({
    "",
    "module",
    "importmap",
    "speculationrules",
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    "text/ecmascript",
    "application/ecmascript",
})

# Matches the HTML parser's own rule: the element ends at the first "</script"
# followed by whitespace, "/" or ">", so a non-greedy match is the same cut.
_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script(?=[\s/>])", re.IGNORECASE | re.DOTALL)
_SRC_ATTR_RE = re.compile(r"(?<![\w-])src\s*=", re.IGNORECASE)
_TYPE_ATTR_RE = re.compile(
    r"""(?<![\w-])type\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE
)


def _script_type(attrs: str) -> str:
    match = _TYPE_ATTR_RE.search(attrs)
    if not match:
        return ""
    value = next(group for group in match.groups() if group is not None)
    return value.strip().lower()


def inline_script_hashes(html: str) -> list[str]:
    """SHA-256 (base64) of every executable inline <script> body in ``html``.

    The browser hashes the element's text content after the HTML parser has
    normalised newlines, so CR and CRLF become LF here too. Document order,
    de-duplicated.
    """
    hashes: dict[str, None] = {}
    for match in _SCRIPT_RE.finditer(html):
        attrs, body = match.group(1), match.group(2)
        if _SRC_ATTR_RE.search(attrs) or body == "":
            continue
        if _script_type(attrs) not in _EXECUTABLE_TYPES:
            continue
        text = body.replace("\r\n", "\n").replace("\r", "\n")
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        hashes[base64.b64encode(digest).decode("ascii")] = None
    return list(hashes)


def build_content_security_policy(script_hashes: Iterable[str] = ()) -> str:
    """The full policy, with ``script_hashes`` admitted for inline scripts."""
    script_src = " ".join((
        "'self'",
        *(f"'sha256-{digest}'" for digest in script_hashes),
        *SCRIPT_SRC_HOSTS,
    ))
    return "; ".join((
        "default-src 'self'",
        f"script-src {script_src}",
        # Inline event-handler attributes (onclick=...) never run. Listed
        # after script-src: tests/test_routes_core.py reads the first
        # directive containing "script-src".
        "script-src-attr 'none'",
        "worker-src 'self' blob:",
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com",
        "font-src 'self' data: https://fonts.gstatic.com",
        "img-src 'self' data: https:",
        # clerk.shelah.org: this project's custom Clerk Frontend API domain
        # (Clerk Dashboard -> Domains). Without this, every ClerkJS
        # fetch() -- sign-in, sign-up, session refresh -- is silently
        # blocked by the browser's CSP before it leaves the page, since the
        # publishable key routes ClerkJS at the custom domain, not the
        # *.clerk.accounts.dev default already allowlisted below. Found via
        # 2026-08-24 incident: CSP blocks are invisible to curl (which
        # doesn't enforce CSP), so server-side probes looked healthy while
        # every real browser failed identically.
        "connect-src 'self' https://clerk.com https://*.clerk.accounts.dev "
        "https://clerk.shelah.org "
        "https://api.clerk.com https://clerk-telemetry.com "
        "https://challenges.cloudflare.com "
        "https://o4511830797975553.ingest.us.sentry.io",
        # frame-src: Clerk's Bot Sign-up Protection renders a Cloudflare
        # Turnstile challenge in an iframe. With no frame-src directive this
        # fell back to default-src 'self', blocking the iframe outright --
        # confirmed live via a direct API probe returning
        # "code":"captcha_missing_token" for a request with no token.
        "frame-src https://challenges.cloudflare.com",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    )) + ";"


# For everything that is not an HTML document with inline scripts: JSON, CSS,
# JS files, the service worker, native FastAPI routes. No inline script is
# admitted, which is correct for all of them.
DEFAULT_CONTENT_SECURITY_POLICY = build_content_security_policy()


def content_security_policy_for(response) -> str:
    """The policy for a Flask ``response``.

    An HTML document gets its own inline scripts hashed in. Anything that is
    streamed or a file passthrough (send_file) is left on the default policy:
    reading its body here would consume it.
    """
    if (
        response.mimetype == "text/html"
        and not response.direct_passthrough
        and not response.is_streamed
    ):
        hashes = inline_script_hashes(response.get_data(as_text=True))
        if hashes:
            return build_content_security_policy(hashes)
    return DEFAULT_CONTENT_SECURITY_POLICY
