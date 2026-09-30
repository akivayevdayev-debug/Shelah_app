"""
Live progress for one AI answer.

The /ask pipeline does several independent things before and while it asks the
model: look up primary sources, pull Halachipedia / Wikipedia context, read
the reader's community customs, look up today's zmanim and Hebrew date, and
finally wait on the model. Until now the browser saw a silent skeleton for the
whole time. This module lets each of those steps say when it starts and ends,
so the client can show what is actually happening.

Design constraints:

* Reporting is optional and free when unused. Nothing is bound on the plain
  JSON path, so begin()/end() return immediately there.
* The reporter is carried in a ContextVar. asyncio tasks, asyncio.to_thread
  and backend.logging_setup.submit_with_context all copy the caller's context,
  so a step that runs on a worker still reports to the request that started
  it. Work that has no bound reporter (a background job, another request)
  reports to nobody.
* Reporting can never fail a request: every send is guarded.
* Steps that fan out into several lookups (Halachipedia and Wikipedia are one
  "commentary" step) are reference-counted, so the client sees one start when
  the first lookup begins and one done when the last one ends.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

from backend.logging_setup import get_logger

logger = get_logger(__name__)

# Stage ids the client knows how to label (static/js/ask-progress.js). Kept as
# a closed set so a typo here is a loud test failure, not a silent no-op.
STAGE_SOURCES = "sources"
STAGE_COMMENTARY = "commentary"
STAGE_CUSTOMS = "customs"
STAGE_TIMES = "times"
STAGE_THINKING = "thinking"
STAGES = frozenset({
    STAGE_SOURCES, STAGE_COMMENTARY, STAGE_CUSTOMS, STAGE_TIMES, STAGE_THINKING,
})

NDJSON_MIMETYPE = "application/x-ndjson"


class ProgressReporter:
    """Turns begin()/end() calls from any thread into ordered stage events."""

    def __init__(self, send: Callable[[dict[str, Any]], None]):
        self._send = send
        self._lock = threading.Lock()
        self._depth: dict[str, int] = {}

    def begin(self, stage: str) -> None:
        with self._lock:
            depth = self._depth.get(stage, 0)
            self._depth[stage] = depth + 1
            if depth:
                return
            self._deliver({"type": "stage", "stage": stage, "state": "start"})

    def end(self, stage: str) -> None:
        with self._lock:
            depth = self._depth.get(stage, 0)
            if depth <= 0:
                return
            self._depth[stage] = depth - 1
            if depth > 1:
                return
            self._deliver({"type": "stage", "stage": stage, "state": "done"})

    def _deliver(self, event: dict[str, Any]) -> None:
        try:
            self._send(event)
        except Exception:  # noqa: BLE001 -- progress is best-effort by design
            logger.debug("ask progress send failed", exc_info=True)


_reporter: ContextVar[ProgressReporter | None] = ContextVar("ask_progress_reporter", default=None)


def bind(send: Callable[[dict[str, Any]], None]) -> Token:
    """Start reporting this context (and every context copied from it) to
    `send`. `send` may be called from any thread and must not block."""
    return _reporter.set(ProgressReporter(send))


def unbind(token: Token) -> None:
    _reporter.reset(token)


def begin(stage: str) -> None:
    reporter = _reporter.get()
    if reporter is not None and stage in STAGES:
        reporter.begin(stage)


def end(stage: str) -> None:
    reporter = _reporter.get()
    if reporter is not None and stage in STAGES:
        reporter.end(stage)


@contextmanager
def stage(stage_id: str):
    """`with ask_progress.stage("sources"):` around a blocking step."""
    begin(stage_id)
    try:
        yield
    finally:
        end(stage_id)


async def track(stage_id: str, awaitable):
    """Await `awaitable` as one progress step."""
    begin(stage_id)
    try:
        return await awaitable
    finally:
        end(stage_id)


def wants_stream(accept_header: str | None) -> bool:
    """True when the client asked for the NDJSON progress stream. A client
    that does not name it (curl, tests, the service worker, older cached
    JS) gets the unchanged single JSON response."""
    return NDJSON_MIMETYPE in str(accept_header or "").lower()


def encode_event(event: dict[str, Any]) -> bytes:
    """One NDJSON line. ensure_ascii=False keeps Hebrew readable on the wire;
    the newline-delimited framing is safe because json.dumps escapes any
    newline inside a string."""
    return (json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")
