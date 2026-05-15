"""
External API health-check and circuit-breaker for Sh'elah.

Maintains lightweight in-process state about the health of each external
service (Sefaria, Hebcal, Gemini, Claude).  A circuit opens after
FAIL_THRESHOLD consecutive failures; it half-opens after RECOVERY_INTERVAL
seconds to allow re-probing.

Usage (in data_service.py / app.py):
    from backend.health_check import health

    if not health.is_healthy('sefaria'):
        return {'error': 'Sefaria is temporarily unavailable.'}

    result = sefaria_client.get_text(reference)

The module is intentionally stateless across process restarts — it starts
with all services assumed healthy and builds evidence at runtime.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

FAIL_THRESHOLD = 3           # Consecutive failures before circuit opens
RECOVERY_INTERVAL = 120      # Seconds before half-opening the circuit
REQUEST_TIMEOUT = 5          # Seconds for each health probe


@dataclass
class _CircuitState:
    """Per-service circuit-breaker state."""
    status: str = "up"          # "up" | "down" | "half-open"
    failures: int = 0
    last_failure_ts: float = 0.0
    last_success_ts: float = field(default_factory=time.time)

    def record_success(self) -> None:
        self.failures = 0
        self.status = "up"
        self.last_success_ts = time.time()

    def record_failure(self) -> None:
        self.failures += 1
        self.last_failure_ts = time.time()
        if self.failures >= FAIL_THRESHOLD:
            self.status = "down"

    def should_probe(self) -> bool:
        """True when circuit is open but recovery interval has elapsed."""
        if self.status == "down":
            return (time.time() - self.last_failure_ts) >= RECOVERY_INTERVAL
        return self.status != "down"


# ─── Health probe functions ────────────────────────────────────────────────────

def _probe_sefaria() -> bool:
    """Lightweight read of a small Sefaria text endpoint."""
    r = requests.get(
        "https://www.sefaria.org/api/texts/Berakhot.2a?pad=0&commentary=0",
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "Shelah-HealthCheck/1.0"},
    )
    return r.status_code == 200


def _probe_hebcal() -> bool:
    """Ping Hebcal's JSON API."""
    r = requests.get(
        "https://www.hebcal.com/api/holidays?v=1&year=2026&cfg=json",
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "Shelah-HealthCheck/1.0"},
    )
    return r.status_code == 200


def _probe_gemini() -> bool:
    """Gemini availability — just check that the models list endpoint responds."""
    import os
    api_key = os.environ.get(
        "GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return True  # Can't probe without key; assume up
    r = requests.get(
        f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "Shelah-HealthCheck/1.0"},
    )
    return r.status_code == 200


def _probe_claude() -> bool:
    """Anthropic availability — just check that the models list endpoint responds."""
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return True  # Can't probe without key; assume up
    r = requests.get(
        "https://api.anthropic.com/v1/models",
        timeout=REQUEST_TIMEOUT,
        headers={
            "anthropic-version": "2023-06-01",
            "x-api-key": api_key,
            "User-Agent": "Shelah-HealthCheck/1.0",
        },
    )
    return r.status_code in {200, 403}  # 403 = authed endpoint, API is up


# ─── APIHealth class ───────────────────────────────────────────────────────────

class APIHealth:
    """Thread-safe, in-process circuit breaker for external APIs."""

    _PROBES: Dict[str, object] = {
        "sefaria": _probe_sefaria,
        "hebcal": _probe_hebcal,
        "gemini": _probe_gemini,
        "claude": _probe_claude,
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._circuits: Dict[str, _CircuitState] = {
            name: _CircuitState() for name in self._PROBES
        }

    # ── Public API ────────────────────────────────────────────────────────────

    def is_healthy(self, service: str) -> bool:
        """Return True if the service is considered available.

        If the circuit is half-open (recovery interval elapsed), a probe is
        attempted inline to update state before returning.
        """
        with self._lock:
            circuit = self._circuits.get(service)
        if circuit is None:
            return True  # Unknown service — optimistically assume up

        if circuit.status == "up":
            return True
        if circuit.status == "down" and circuit.should_probe():
            self._probe(service)
        return circuit.status != "down"

    def check(self, service: str) -> bool:
        """Explicitly probe a service and update circuit state. Returns bool."""
        return self._probe(service)

    def status_summary(self) -> Dict[str, str]:
        """Return a dict of {service: status} for the health dashboard."""
        with self._lock:
            return {name: c.status for name, c in self._circuits.items()}

    # ── Internal ──────────────────────────────────────────────────────────────

    def _probe(self, service: str) -> bool:
        probe_fn = self._PROBES.get(service)
        if probe_fn is None:
            return True
        try:
            ok = probe_fn()  # type: ignore[call-arg]
        except Exception as exc:
            logger.warning("health_check[%s] probe error: %s", service, exc)
            ok = False

        with self._lock:
            circuit = self._circuits.get(service)
            if circuit is None:
                return ok
            if ok:
                circuit.record_success()
                logger.info(
                    "health_check[%s] probe OK — circuit closed", service)
            else:
                circuit.record_failure()
                logger.warning(
                    "health_check[%s] probe FAILED (consecutive=%d, status=%s)",
                    service, circuit.failures, circuit.status,
                )
        return ok


# ─── Module-level singleton ───────────────────────────────────────────────────

health = APIHealth()
"""
Shared singleton.  Import this in any module that needs to gate on service health:

    from backend.health_check import health

    if not health.is_healthy('sefaria'):
        raise ServiceUnavailableError("Sefaria is temporarily unavailable")
"""
