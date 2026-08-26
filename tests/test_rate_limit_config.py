"""
Regression test for the RATELIMIT_ENABLED dead-config bug.

Originally this guarded against app.py never reading the RATELIMIT_ENABLED
env var (Flask-Limiter's own switch was scoped to the Limiter constructor's
`enabled` kwarg, not raw os.environ, so setting the env var did nothing).
Flask-Limiter and asgi.py's separate in-process limiter have since been
unified into backend/rate_limit.py (plan.md §16.3-L2 / §16.8.1), which reads
RATELIMIT_ENABLED itself as a plain module-level `os.environ.get(...)` at
import time -- this test now guards that read directly.

RATELIMIT_ENABLED is still a boot-time property: backend/rate_limit.py's
RateLimitMiddleware.dispatch() consults the module-level RATELIMIT_ENABLED
global captured once at import, so toggling the attribute after import can't
retroactively change already-registered behavior. That makes this a
subprocess-boot test (controlled environment), same as before.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_BOOT_SNIPPET = """
import backend.rate_limit as rate_limit
print(rate_limit.RATELIMIT_ENABLED)
"""

_BASE_ENV = {
    "FLASK_ENV": "testing",
    "SEFARIA_API": "https://mock.sefaria.org/api",
    "SEFARIA_V3_API": "https://mock.sefaria.org/api/v3",
    "SUPABASE_URL": "https://mock.supabase.co",
    "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_mock-key",
    "SUPABASE_SECRET_KEY": "sb_secret_mock-key",
    "ANTHROPIC_API_KEY": "mock-anthropic-key",
    "GEMINI_API_KEY": "mock-gemini-key",
    "LOG_LEVEL": "ERROR",
}


def _boot_and_read_ratelimit_enabled(ratelimit_enabled_value):
    env = {**os.environ, **_BASE_ENV}
    if ratelimit_enabled_value is None:
        env.pop("RATELIMIT_ENABLED", None)
    else:
        env["RATELIMIT_ENABLED"] = ratelimit_enabled_value

    result = subprocess.run(
        [sys.executable, "-c", _BOOT_SNIPPET],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"backend.rate_limit failed to boot (RATELIMIT_ENABLED={ratelimit_enabled_value!r}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout.strip() == "True"


class TestRatelimitEnabledIsWired:
    def test_ratelimit_enabled_false_actually_disables_the_limiter(self):
        assert _boot_and_read_ratelimit_enabled("false") is False

    def test_ratelimit_enabled_unset_defaults_to_enabled(self):
        # Preserves today's production behavior: rate limiting is on unless
        # something explicitly turns it off.
        assert _boot_and_read_ratelimit_enabled(None) is True
