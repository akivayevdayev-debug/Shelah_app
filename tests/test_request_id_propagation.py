"""
Integration tests for request_id propagation — plan.md §7 / §8.E.1:
"structured JSON logging with a request ID generated per request and
propagated across Flask, thread-pool work (_THREAD_POOL), and asyncio
tasks (contextvars)."

Unit-level coverage for the underlying primitives (submit_with_context,
asyncio's own context-copy behavior) lives in test_logging_setup.py. This
file verifies request_id actually shows up in *formatted log records* for
a threaded path and an async path, plus the end-to-end HTTP contract
(X-Request-Id echoed on both the Flask and FastAPI surfaces).
"""

from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

import backend.logging_setup as logging_setup


@pytest.fixture(autouse=True)
def _reset_request_id():
    logging_setup._request_id_var.set("")
    yield
    logging_setup._request_id_var.set("")


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def json_capture():
    """Capture formatted JSON log output on the root logger, so assertions
    exercise the real _JSONFormatter (the thing that actually attaches
    request_id), not just the raw LogRecord attributes."""
    handler = _ListHandler()
    handler.setFormatter(logging_setup._JSONFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def _formatted(handler: _ListHandler) -> list[dict]:
    return [json.loads(handler.format(r)) for r in handler.records]


def _log_from_worker_thread():
    logging.getLogger("test.threaded").info("threaded work log line")
    return logging_setup.get_request_id()


class TestThreadedPathCarriesRequestId:
    def test_log_record_from_pool_worker_has_request_id(self, json_capture):
        logging_setup.bind_request_id("thread-req-id")
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            future = logging_setup.submit_with_context(
                pool, _log_from_worker_thread)
            seen_request_id = future.result(timeout=5)
        finally:
            pool.shutdown(wait=True)

        assert seen_request_id == "thread-req-id"
        matching = [
            rec for rec in _formatted(json_capture)
            if rec.get("message") == "threaded work log line"
        ]
        assert matching, "expected a log record emitted from the worker thread"
        assert matching[0]["request_id"] == "thread-req-id"


async def _log_from_task():
    logging.getLogger("test.async").info("async task log line")
    return logging_setup.get_request_id()


class TestAsyncPathCarriesRequestId:
    async def test_log_record_from_create_task_has_request_id(self, json_capture):
        logging_setup.bind_request_id("async-task-req-id")
        seen_request_id = await asyncio.create_task(_log_from_task())

        assert seen_request_id == "async-task-req-id"
        matching = [
            rec for rec in _formatted(json_capture)
            if rec.get("message") == "async task log line"
        ]
        assert matching, "expected a log record emitted from the asyncio task"
        assert matching[0]["request_id"] == "async-task-req-id"

    async def test_to_thread_inherits_request_id(self, json_capture):
        logging_setup.bind_request_id("async-to-thread-req-id")
        seen_request_id = await asyncio.to_thread(_log_from_worker_thread)

        assert seen_request_id == "async-to-thread-req-id"
        matching = [
            rec for rec in _formatted(json_capture)
            if rec.get("message") == "threaded work log line"
        ]
        assert matching
        assert matching[0]["request_id"] == "async-to-thread-req-id"


class TestFlaskRequestIdHeaderContract:
    def test_generates_id_when_client_sends_none(self, test_client):
        response = test_client.get("/api/stack/health")
        assert response.headers.get("X-Request-Id")

    def test_echoes_client_supplied_id(self, test_client):
        response = test_client.get(
            "/api/stack/health", headers={"X-Request-Id": "client-supplied-flask-id"})
        assert response.headers.get("X-Request-Id") == "client-supplied-flask-id"


class TestFastAPIRequestIdHeaderContract:
    async def test_generates_id_when_client_sends_none(self, fastapi_client):
        response = await fastapi_client.get("/api/async/health")
        assert response.headers.get("X-Request-Id")

    async def test_echoes_client_supplied_id(self, fastapi_client):
        response = await fastapi_client.get(
            "/api/async/health", headers={"X-Request-Id": "client-supplied-asgi-id"})
        assert response.headers.get("X-Request-Id") == "client-supplied-asgi-id"

    async def test_wsgi_mounted_flask_route_also_gets_an_id(self, fastapi_client):
        """Routes served by the WSGI-mounted Flask app (everything except
        the native FastAPI routes) go through both the ASGI middleware and
        Flask's own before_request — must still resolve to exactly one id."""
        response = await fastapi_client.get("/api/stack/health")
        assert response.headers.get("X-Request-Id")

    async def test_client_supplied_id_reaches_the_mounted_flask_route(self, fastapi_client):
        response = await fastapi_client.get(
            "/api/stack/health", headers={"X-Request-Id": "explicit-mounted-flask-id"})
        assert response.headers.get("X-Request-Id") == "explicit-mounted-flask-id"

    async def test_asgi_and_flask_layers_agree_on_one_generated_id(self, fastapi_client, json_capture):
        """The response header is set last by the ASGI middleware (it
        overwrites whatever Flask's after_request wrote), so a header-only
        assertion can't catch the two layers disagreeing. Instead, capture
        the structured log line Flask's own after_request hook emits and
        confirm *its* request_id matches what the client sees — proving
        the id generated at the ASGI boundary is the same one Flask's
        before_request bound for its own log records, not a second,
        independently-generated one."""
        response = await fastapi_client.get("/api/stack/health")
        response_request_id = response.headers.get("X-Request-Id")
        assert response_request_id

        flask_log_records = [
            rec for rec in _formatted(json_capture)
            if rec.get("message") == "request_complete"
            and rec.get("logger") == "shelah.request.flask"
            and rec.get("http_path") == "/api/stack/health"
        ]
        assert flask_log_records, "expected Flask's request_complete log line"
        assert flask_log_records[-1]["request_id"] == response_request_id
