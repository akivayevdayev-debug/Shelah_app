"""
Live progress for an AI answer (backend/ask_progress.py) and the two routes
that can stream it: POST /ask (asgi.py) and POST /api/conversations/<id>/ask
(backend/routes_conversations.py).

The contract worth pinning down:
  * a client that does not ask for the stream gets the unchanged JSON;
  * a streamed answer still reports real HTTP statuses for every refusal,
    because the response only commits to streaming at the first step;
  * a stream that started always ends with exactly one result/error line;
  * reporting can never break a request.
"""

import asyncio
import json
import threading

import pytest
from fastapi import HTTPException

import asgi
from backend import ask_progress
# The conversation-route tests reuse that module's fake Supabase client and its
# auth / cost-gate fixtures (cost_gates is autouse there, so it must be here).
from tests import test_routes_conversations as conv_tests
from tests.test_routes_conversations import authed, cost_gates  # noqa: F401

NDJSON = {"Accept": "application/x-ndjson"}


def _lines(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class TestProgressReporter:
    def test_unbound_reporting_is_a_silent_no_op(self):
        ask_progress.begin("sources")
        ask_progress.end("sources")
        with ask_progress.stage("times"):
            pass

    def test_start_and_done_are_reported_in_order(self):
        events = []
        token = ask_progress.bind(events.append)
        try:
            with ask_progress.stage("sources"):
                events.append("work")
        finally:
            ask_progress.unbind(token)
        assert events == [
            {"type": "stage", "stage": "sources", "state": "start"},
            "work",
            {"type": "stage", "stage": "sources", "state": "done"},
        ]

    def test_overlapping_lookups_are_one_step_until_the_last_finishes(self):
        events = []
        token = ask_progress.bind(events.append)
        try:
            ask_progress.begin("commentary")
            ask_progress.begin("commentary")
            ask_progress.end("commentary")
            assert [e["state"] for e in events] == ["start"]
            ask_progress.end("commentary")
        finally:
            ask_progress.unbind(token)
        assert [e["state"] for e in events] == ["start", "done"]

    def test_an_unmatched_end_is_ignored(self):
        events = []
        token = ask_progress.bind(events.append)
        try:
            ask_progress.end("sources")
        finally:
            ask_progress.unbind(token)
        assert events == []

    def test_unknown_stage_ids_are_ignored(self):
        events = []
        token = ask_progress.bind(events.append)
        try:
            ask_progress.begin("not-a-stage")
        finally:
            ask_progress.unbind(token)
        assert events == []

    def test_a_failing_sink_never_breaks_the_request(self):
        def broken(_event):
            raise RuntimeError("socket closed")

        token = ask_progress.bind(broken)
        try:
            with ask_progress.stage("sources"):
                result = "still ran"
        finally:
            ask_progress.unbind(token)
        assert result == "still ran"

    def test_a_step_that_raises_still_reports_done(self):
        events = []
        token = ask_progress.bind(events.append)
        try:
            with pytest.raises(ValueError):
                with ask_progress.stage("customs"):
                    raise ValueError("boom")
        finally:
            ask_progress.unbind(token)
        assert [e["state"] for e in events] == ["start", "done"]

    async def test_worker_threads_report_to_the_request_that_started_them(self):
        events = []
        token = ask_progress.bind(events.append)
        try:
            await asyncio.to_thread(ask_progress.begin, "customs")
        finally:
            ask_progress.unbind(token)
        assert events == [{"type": "stage", "stage": "customs", "state": "start"}]

    def test_every_step_the_pipeline_reports_is_a_known_stage(self):
        assert ask_progress.STAGES == {"sources", "commentary", "customs", "times", "thinking"}


class TestWireFormat:
    @pytest.mark.parametrize("accept, expected", [
        ("application/x-ndjson", True),
        ("application/x-ndjson, application/json;q=0.5", True),
        ("Application/X-NDJSON", True),
        ("application/json", False),
        ("*/*", False),
        ("", False),
        (None, False),
    ])
    def test_only_an_explicit_ndjson_accept_opts_in(self, accept, expected):
        assert ask_progress.wants_stream(accept) is expected

    def test_a_line_is_compact_json_with_readable_hebrew_and_one_newline(self):
        line = ask_progress.encode_event({"type": "result", "text": "שלום\nworld"})
        assert line.endswith(b"\n")
        assert line.count(b"\n") == 1
        assert "שלום" in line.decode("utf-8")
        assert json.loads(line) == {"type": "result", "text": "שלום\nworld"}


class TestCollectContextReportsSteps:
    @pytest.fixture
    def stubbed(self, monkeypatch):
        async def primary(question):
            return ["Berakhot 1:1"], [{"ref": "Berakhot 1:1", "lines": [{"en": "Text", "he": "טקסט"}]}]

        async def tool_context():
            return {"route": "/ask", "async": True}

        async def halachipedia(question):
            return {"title": "[Halachipedia] Shabbat", "summary": "s"}

        async def wiki(question):
            return {"title": "Shabbat", "summary": "w"}

        monkeypatch.setattr(asgi, "_collect_primary_sources", primary)
        monkeypatch.setattr(asgi, "_build_tool_context", tool_context)
        monkeypatch.setattr(asgi, "_retrieve_community_knowledge", lambda *a, **k: [])
        monkeypatch.setattr(asgi, "_fetch_user_memory_summaries", lambda *a, **k: [])
        monkeypatch.setattr(asgi.search, "async_search_halachipedia", halachipedia)
        monkeypatch.setattr(asgi.search, "async_search_wikipedia", wiki)

    async def test_each_lookup_reports_one_step_and_finishes_it(self, stubbed):
        events = []
        token = ask_progress.bind(events.append)
        try:
            await asgi._collect_ask_async_context("q", "All", None, "en")
        finally:
            ask_progress.unbind(token)

        started = [e["stage"] for e in events if e["state"] == "start"]
        done = [e["stage"] for e in events if e["state"] == "done"]
        # Stubs that answer instantly can open and close a step more than
        # once (its second lookup starts after its first already finished), so
        # compare first appearances, not raw counts.
        assert list(dict.fromkeys(started)) == ["sources", "commentary", "customs", "times"]
        assert set(started) == set(done)
        # No step is left open: every start has a matching done.
        for stage in set(started):
            assert started.count(stage) == done.count(stage)

    async def test_a_timed_out_lookup_still_closes_its_step(self, stubbed, monkeypatch):
        async def hung(question):
            await asyncio.sleep(30)

        monkeypatch.setattr(asgi.search, "async_search_halachipedia", hung)
        monkeypatch.setitem(asgi._ASK_CONTEXT_TIMEOUT_SECONDS, "halachipedia", 0.05)
        events = []
        token = ask_progress.bind(events.append)
        try:
            await asgi._collect_ask_async_context("q", "All", None, "en")
        finally:
            ask_progress.unbind(token)
        assert {"type": "stage", "stage": "commentary", "state": "done"} in events


class TestAskStreamRoute:
    @pytest.fixture
    def pipeline(self, monkeypatch):
        """Replace the whole pipeline body with one that reports steps."""
        state = {"steps": ["sources", "thinking"], "result": {"answer": "Yes.", "sources": []}, "raise": None}

        async def fake_impl(request, payload, authorization):
            for step in state["steps"]:
                ask_progress.begin(step)
                await asyncio.sleep(0)
            if state["raise"] is not None:
                raise state["raise"]
            return state["result"]

        monkeypatch.setattr(asgi, "_ask_async_impl", fake_impl)
        return state

    async def test_a_plain_request_gets_plain_json(self, fastapi_client, pipeline):
        response = await fastapi_client.post(
            "/ask", json={"question": "q"}, headers={"X-Forwarded-For": "203.0.113.50"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"answer": "Yes.", "sources": []}

    async def test_the_stream_reports_each_step_then_the_answer(self, fastapi_client, pipeline):
        response = await fastapi_client.post(
            "/ask", json={"question": "q"}, headers={**NDJSON, "X-Forwarded-For": "203.0.113.51"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        assert "no-store" in response.headers["cache-control"]
        assert _lines(response.text) == [
            {"type": "stage", "stage": "sources", "state": "start"},
            {"type": "stage", "stage": "thinking", "state": "start"},
            {"type": "result", "payload": {"answer": "Yes.", "sources": []}},
        ]

    async def test_an_answer_that_reported_no_steps_is_plain_json(self, fastapi_client, pipeline):
        pipeline["steps"] = []
        response = await fastapi_client.post(
            "/ask", json={"question": "q"}, headers={**NDJSON, "X-Forwarded-For": "203.0.113.52"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"answer": "Yes.", "sources": []}

    async def test_a_refusal_before_any_step_keeps_its_real_status(self, fastapi_client, pipeline):
        pipeline["steps"] = []
        pipeline["raise"] = HTTPException(status_code=402, detail="Daily AI usage limit reached")
        response = await fastapi_client.post(
            "/ask", json={"question": "q"}, headers={**NDJSON, "X-Forwarded-For": "203.0.113.53"})
        assert response.status_code == 402
        assert response.json() == {"detail": "Daily AI usage limit reached"}

    async def test_a_structured_refusal_keeps_its_code(self, fastapi_client, pipeline):
        pipeline["steps"] = []
        pipeline["raise"] = HTTPException(
            status_code=403, detail={"error": "Verification required", "code": "turnstile_required"})
        response = await fastapi_client.post(
            "/ask", json={"question": "q"}, headers={**NDJSON, "X-Forwarded-For": "203.0.113.54"})
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "turnstile_required"

    async def test_a_failure_after_the_stream_started_is_an_error_line(self, fastapi_client, pipeline):
        pipeline["raise"] = HTTPException(status_code=500, detail="An internal error occurred.")
        response = await fastapi_client.post(
            "/ask", json={"question": "q"}, headers={**NDJSON, "X-Forwarded-For": "203.0.113.55"})
        assert response.status_code == 200
        lines = _lines(response.text)
        assert lines[-1] == {"type": "error", "status": 500, "detail": "An internal error occurred."}

    async def test_an_unexpected_crash_still_ends_the_stream(self, fastapi_client, pipeline):
        pipeline["raise"] = RuntimeError("not an HTTPException")
        response = await fastapi_client.post(
            "/ask", json={"question": "q"}, headers={**NDJSON, "X-Forwarded-For": "203.0.113.56"})
        lines = _lines(response.text)
        assert lines[-1]["type"] == "error"
        assert lines[-1]["status"] == 500
        assert "not an HTTPException" not in response.text

    async def test_the_real_preamble_still_rejects_an_empty_question(self, fastapi_client):
        response = await fastapi_client.post(
            "/ask", json={"question": "   "}, headers={**NDJSON, "X-Forwarded-For": "203.0.113.57"})
        assert response.status_code == 400

    async def test_a_quiet_queue_yields_keep_alive_lines(self, monkeypatch):
        monkeypatch.setattr(asgi, "_ASK_STREAM_KEEPALIVE_SECONDS", 0.01)
        queue = asyncio.Queue()

        async def finish_later():
            await asyncio.sleep(0.06)
            queue.put_nowait({"type": "result", "payload": {}})

        finisher = asyncio.create_task(finish_later())
        lines = [
            json.loads(chunk)
            async for chunk in asgi._ask_stream_events({"type": "stage", "stage": "sources", "state": "start"}, queue)
        ]
        await finisher
        assert lines[0]["type"] == "stage"
        assert lines[-1]["type"] == "result"
        assert any(line["type"] == "ping" for line in lines)


@pytest.mark.usefixtures("authed")  # fakes the Clerk token; its value is never read
class TestConversationAskStream:
    """Drives POST /api/conversations/<id>/ask through the real view with the
    retrieval/synthesis helpers replaced by ones that report steps."""

    @pytest.fixture
    def conv(self):
        return conv_tests

    def _setup(self, t, monkeypatch, *, steps=("sources", "times"), dispatch_error=None):
        client = t._ask_client()
        monkeypatch.setattr(t.routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        t._patch_ai_pipeline(monkeypatch)

        def collect(question, canonical_lens, user_id, answer_language, engine, retrieval_context=()):
            for step in steps:
                ask_progress.begin(step)
            return {"primary_sources": []}

        def dispatch(*a, **k):
            ask_progress.begin("thinking")
            if dispatch_error:
                raise dispatch_error
            return {"raw": True}

        monkeypatch.setattr(t.routes_conversations_module, "_collect_ask_question_context", collect)
        monkeypatch.setattr(t.routes_conversations_module, "_dispatch_ask_ai_synthesis_call", dispatch)
        return client

    def test_plain_request_is_unchanged_json(self, test_client, conv, monkeypatch):
        self._setup(conv, monkeypatch)
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=conv.AUTH_HEADERS)
        assert response.status_code == 201
        assert response.get_json()["assistant_message"]["id"] == "msg-assistant-1"

    def test_the_stream_reports_steps_then_the_same_body(self, test_client, conv, monkeypatch):
        self._setup(conv, monkeypatch)
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"},
            headers={**conv.AUTH_HEADERS, **NDJSON})
        assert response.status_code == 200
        assert response.mimetype == "application/x-ndjson"
        lines = _lines(response.get_data(as_text=True))
        assert [(e.get("stage"), e.get("state")) for e in lines[:-1]] == [
            ("sources", "start"), ("times", "start"), ("thinking", "start")]
        last = lines[-1]
        assert last["type"] == "result"
        assert last["status"] == 201
        assert last["body"]["user_message"]["id"] == "msg-user-1"
        assert last["body"]["assistant_message"]["id"] == "msg-assistant-1"

    def test_a_synthesis_failure_is_still_a_result_with_a_retryable_error_turn(
        self, test_client, conv, monkeypatch,
    ):
        client = self._setup(conv, monkeypatch, dispatch_error=RuntimeError("model down"))
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"},
            headers={**conv.AUTH_HEADERS, **NDJSON})
        last = _lines(response.get_data(as_text=True))[-1]
        assert last["type"] == "result"
        stored_assistant_turns = [
            args[0]["status"]
            for args, _kwargs in client.table(conv.MSG_TABLE).insert_calls
            if args[0].get("role") == "assistant"
        ]
        assert stored_assistant_turns == ["error"]

    def test_a_refusal_before_any_step_is_the_ordinary_response(
        self, test_client, conv, monkeypatch,
    ):
        client = conv._FakeSupabaseClient({conv.CONV_TABLE: conv._FakeQuery(data=[])})
        monkeypatch.setattr(conv.routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/nope/ask", json={"question": "q"},
            headers={**conv.AUTH_HEADERS, **NDJSON})
        assert response.status_code == 404
        assert response.mimetype == "application/json"

    def test_an_ask_that_reports_no_steps_is_plain_json(self, test_client, conv, monkeypatch):
        self._setup(conv, monkeypatch, steps=())
        monkeypatch.setattr(
            conv.routes_conversations_module, "_dispatch_ask_ai_synthesis_call",
            lambda *a, **k: {"raw": True})
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"},
            headers={**conv.AUTH_HEADERS, **NDJSON})
        assert response.status_code == 201
        assert response.mimetype == "application/json"

    def test_the_worker_can_still_read_the_request_and_flask_globals(
        self, test_client, conv, monkeypatch,
    ):
        seen = {}
        self._setup(conv, monkeypatch)

        def collect(question, canonical_lens, user_id, answer_language, engine, retrieval_context=()):
            from flask import g, request

            ask_progress.begin("sources")
            seen["thread"] = threading.current_thread().name
            seen["path"] = request.path
            seen["claims"] = dict(getattr(g, "clerk_claims", {}) or {})
            return {"primary_sources": []}

        monkeypatch.setattr(conv.routes_conversations_module, "_collect_ask_question_context", collect)
        test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"},
            headers={**conv.AUTH_HEADERS, **NDJSON}).get_data()
        assert seen["path"] == "/api/conversations/conv-1/ask"
        assert seen["claims"].get("sub") == conv.FAKE_USER_ID
        assert seen["thread"] != threading.main_thread().name

    def test_a_keepalive_is_sent_while_the_worker_is_quiet(self, monkeypatch):
        import queue

        from backend import routes_conversations as rc

        monkeypatch.setattr(rc, "_STREAM_KEEPALIVE_SECONDS", 0.01)
        events = queue.Queue()
        threading.Timer(0.06, lambda: events.put({
            "type": rc._WORKER_DONE, "terminal": {"type": "result", "status": 201, "body": {}},
        })).start()
        lines = [json.loads(chunk) for chunk in rc._conversation_stream_body(
            {"type": "stage", "stage": "sources", "state": "start"}, events)]
        assert lines[0]["type"] == "stage"
        assert lines[-1]["type"] == "result"
        assert any(line["type"] == "ping" for line in lines)
