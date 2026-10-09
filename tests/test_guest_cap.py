"""
Tests for backend/guest_cap.py -- the limits on a signed-out visitor's
conversation -- and for how asgi.py's /ask applies them.

The fake ask_history table keeps just enough of PostgREST's query surface
(select / eq / in_ / gte / order / limit / insert) to prove the properties that
matter: only rows the device owns are ever used, the day's count is rolling, and
a refusal never reaches the model.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import asgi
import backend.device_identity as device
from backend import guest_cap

TOKEN = "g" * 40
OWNER = device.owner_for_token(TOKEN)
OTHER = device.owner_for_token("h" * 40)
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def _row(n, *, owner=OWNER, question=None, answer="An answer.", age_hours=1.0):
    return {
        "id": f"00000000-0000-0000-0000-{n:012d}",
        "user_id": owner,
        "question": question or f"Question {n}?",
        "answer": answer,
        "created_at": (NOW - timedelta(hours=age_hours, minutes=-n)).isoformat(),
    }


class _Result:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Table:
    def __init__(self, store):
        self.store = store
        self._filters = []
        self._order = None
        self._limit = None
        self._count = False
        self._inserting = None

    def select(self, _cols, count=None):
        self._count = count == "exact"
        return self

    def eq(self, key, value):
        self._filters.append(lambda r: r.get(key) == value)
        return self

    def in_(self, key, values):
        self._filters.append(lambda r: r.get(key) in values)
        return self

    def gte(self, key, value):
        self._filters.append(lambda r: r.get(key) >= value)
        return self

    def order(self, key, desc=False):
        self._order = (key, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def insert(self, payload):
        self._inserting = payload
        return self

    def execute(self):
        if self._inserting is not None:
            self.store.append({**self._inserting, "created_at": NOW.isoformat()})
            return _Result([self._inserting])
        rows = [r for r in self.store if all(f(r) for f in self._filters)]
        total = len(rows)
        if self._order:
            rows = sorted(rows, key=lambda r: r[self._order[0]], reverse=self._order[1])
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Result(rows, count=total if self._count else None)


class _Client:
    def __init__(self, store):
        self.store = store

    def table(self, _name):
        return _Table(self.store)


@pytest.fixture
def history(monkeypatch):
    """The fake ask_history rows, installed as the app's Supabase client."""
    import app as app_module

    rows: list = []
    monkeypatch.setattr(app_module, "_get_supabase_client", lambda: _Client(rows))
    return rows


def _ids(rows):
    return [r["id"] for r in rows]


# ── load_thread ────────────────────────────────────────────────────────────

class TestLoadThread:
    def test_only_rows_the_device_owns_are_used(self, history):
        mine = [_row(1), _row(2)]
        theirs = [_row(3, owner=OTHER), _row(4, owner="user_abc")]
        history.extend(mine + theirs)

        thread = guest_cap.load_thread(OWNER, _ids(mine + theirs), "Next?", now=NOW)

        assert _ids(thread.rows) == _ids(mine)
        assert thread.prior_counted == 2

    def test_a_forged_or_malformed_id_yields_nothing(self, history):
        history.append(_row(1))
        thread = guest_cap.load_thread(OWNER, ["nope", "'; drop table", "", None, "x" * 200], "Q?", now=NOW)
        assert thread.rows == []

    def test_no_ids_is_a_fresh_conversation(self, history):
        history.append(_row(1))
        thread = guest_cap.load_thread(OWNER, None, "Q?", now=NOW)
        assert thread.rows == []
        assert thread.history == []
        assert thread.retrieval_context == ()

    def test_the_day_counts_the_devices_rows_in_the_last_24_hours(self, history):
        history.extend([_row(1, age_hours=1), _row(2, age_hours=23), _row(3, age_hours=25),
                        _row(4, owner=OTHER, age_hours=1)])
        thread = guest_cap.load_thread(OWNER, [], "Q?", now=NOW)
        assert thread.daily_used == 2

    def test_history_is_oldest_first_whatever_order_the_client_sent(self, history):
        rows = [_row(1), _row(2), _row(3)]
        history.extend(rows)
        thread = guest_cap.load_thread(OWNER, list(reversed(_ids(rows))), "Q?", now=NOW)
        assert [t["content"] for t in thread.history if t["role"] == "user"] == [
            "Question 1?", "Question 2?", "Question 3?"]
        assert [t["role"] for t in thread.history] == ["user", "assistant"] * 3

    def test_retrieval_context_is_the_latest_questions_newest_first(self, history):
        rows = [_row(n) for n in range(1, 6)]
        history.extend(rows)
        thread = guest_cap.load_thread(OWNER, _ids(rows), "Q?", now=NOW)
        assert thread.retrieval_context == ("Question 5?", "Question 4?", "Question 3?")

    def test_no_device_or_no_database_means_nothing_to_measure(self, history, monkeypatch):
        assert guest_cap.load_thread(None, ["a" * 10], "Q?") is None
        import app as app_module

        monkeypatch.setattr(app_module, "_get_supabase_client", lambda: None)
        assert guest_cap.load_thread(OWNER, ["a" * 10], "Q?") is None

    def test_a_failing_lookup_fails_open(self, monkeypatch):
        import app as app_module

        class _Broken:
            def table(self, _name):
                raise RuntimeError("database down")

        monkeypatch.setattr(app_module, "_get_supabase_client", lambda: _Broken())
        assert guest_cap.load_thread(OWNER, ["a" * 10], "Q?") is None


# ── the limits ─────────────────────────────────────────────────────────────

def _thread(**kwargs):
    return guest_cap.GuestThread(owner=OWNER, **kwargs)


class TestRefusal:
    def test_a_fresh_conversation_is_allowed(self):
        assert guest_cap.refusal_reason(_thread()) is None

    def test_the_eighth_question_is_allowed_and_the_ninth_is_not(self):
        assert guest_cap.refusal_reason(_thread(prior_counted=7)) is None
        assert guest_cap.refusal_reason(_thread(prior_counted=8)) == "thread_questions"

    def test_tokens_run_out_independently_of_questions(self):
        limit = guest_cap.THREAD_MAX_TOKENS
        assert guest_cap.refusal_reason(_thread(prior_counted=2, prior_tokens=limit - 1)) is None
        assert guest_cap.refusal_reason(_thread(prior_counted=2, prior_tokens=limit)) == "thread_tokens"

    def test_the_day_is_checked_first(self):
        thread = _thread(prior_counted=8, daily_used=guest_cap.DAILY_MAX_QUESTIONS)
        assert guest_cap.refusal_reason(thread) == "daily"

    def test_one_document_request_is_free_even_at_the_cap(self):
        thread = _thread(prior_counted=8, document=True, document_free=True)
        assert guest_cap.refusal_reason(thread) is None

    def test_a_second_document_request_counts_like_any_other(self):
        thread = _thread(prior_counted=8, prior_documents=1, document=True, document_free=False)
        assert guest_cap.refusal_reason(thread) == "thread_questions"

    def test_the_document_brings_its_own_tokens(self):
        base = guest_cap.THREAD_MAX_TOKENS
        used = base + 1000
        assert guest_cap.refusal_reason(_thread(prior_tokens=used)) == "thread_tokens"
        with_doc = _thread(prior_tokens=used, prior_documents=1)
        assert with_doc.tokens_limit == base + guest_cap.DOC_EXTRA_TOKENS
        assert guest_cap.refusal_reason(with_doc) is None

    def test_a_study_question_is_the_document_style_one(self):
        assert guest_cap.is_document_question("Write me a study guide on Shabbat with a table of the 39 melachot")
        assert not guest_cap.is_document_question("Can I turn on a light on Shabbat?")


class TestUsage:
    def test_a_stored_answer_counts_toward_all_three(self):
        thread = _thread(prior_counted=3, prior_tokens=1000, daily_used=5)
        usage = guest_cap.usage_payload(thread, "x" * 300)
        assert usage["questions_used"] == 4
        assert usage["tokens_used"] == 1000 + 100
        assert usage["daily_used"] == 6
        assert usage["remaining"] == guest_cap.THREAD_MAX_QUESTIONS - 4
        assert usage["binding"] == "thread"

    def test_an_answer_that_was_not_stored_is_not_counted(self):
        thread = _thread(prior_counted=3, daily_used=5)
        usage = guest_cap.usage_payload(thread, "x" * 300, stored=False)
        assert (usage["questions_used"], usage["daily_used"]) == (3, 5)

    def test_the_free_document_ask_spends_no_question(self):
        thread = _thread(prior_counted=2, document=True, document_free=True)
        assert guest_cap.usage_payload(thread, "a")["questions_used"] == 2

    def test_the_day_can_be_what_runs_out_first(self):
        thread = _thread(prior_counted=1, daily_used=guest_cap.DAILY_MAX_QUESTIONS - 2)
        usage = guest_cap.usage_payload(thread, "a")
        assert usage["remaining"] == 1
        assert usage["binding"] == "daily"

    def test_spent_tokens_leave_nothing_even_with_questions_to_spare(self):
        thread = _thread(prior_counted=1, prior_tokens=guest_cap.THREAD_MAX_TOKENS)
        assert guest_cap.usage_payload(thread, "a")["remaining"] == 0

    def test_the_refusal_names_why_and_carries_the_counters(self):
        thread = _thread(prior_counted=8)
        detail = guest_cap.refusal_payload(thread, "thread_questions")
        assert detail["code"] == "guest_cap_reached"
        assert detail["reason"] == "thread_questions"
        assert detail["usage"]["remaining"] == 0
        assert "Sign in" in detail["error"]


# ── /ask ───────────────────────────────────────────────────────────────────

class TestGate:
    async def test_a_signed_in_caller_is_never_measured(self, history):
        history.extend(_row(n) for n in range(1, 12))
        assert await asgi._enforce_ask_async_guest_cap("user_abc", "Q?", _ids(history)) is None

    async def test_a_full_conversation_is_refused_with_403(self, history):
        history.extend(_row(n) for n in range(1, 9))
        binding = device.bind_device({device.DEVICE_COOKIE: TOKEN})
        try:
            with pytest.raises(HTTPException) as caught:
                await asgi._enforce_ask_async_guest_cap(None, "One more?", _ids(history))
        finally:
            device.release_device(binding)
        assert caught.value.status_code == 403
        assert caught.value.detail["code"] == "guest_cap_reached"
        assert caught.value.detail["reason"] == "thread_questions"

    def test_usage_goes_on_a_copy_so_a_cached_payload_is_untouched(self):
        thread = _thread(prior_counted=2)
        cached = {"answer": "A.", "history_id": "abc"}
        out = asgi._attach_guest_usage(thread, cached)
        assert "meta" not in cached
        assert out["meta"]["guest"]["questions_used"] == 3
        assert "guest" not in out  # the top-level keys match the Flask /ask
        assert asgi._attach_guest_usage(None, cached) is cached

    def test_an_unsaved_answer_is_not_counted(self):
        out = asgi._attach_guest_usage(_thread(prior_counted=2), {"answer": "A."})
        assert out["meta"]["guest"]["questions_used"] == 2


@pytest.fixture
def model(monkeypatch):
    """Replaces the model with one that records what it was asked."""
    calls = []

    async def fake_ask_ai_async(**kwargs):
        calls.append(kwargs)
        return {"answer": "A sourced answer.", "structured": None}

    monkeypatch.setattr(asgi.claude, "ask_ai_async", fake_ask_ai_async)
    monkeypatch.setattr(asgi.claude, "AI_AGENTIC_TOOLS", False)
    return calls


def _post(client, body, *, forwarded="203.0.113.90"):
    return client.post(
        "/ask", json=body,
        headers={"X-Forwarded-For": forwarded, "Cookie": f"{device.DEVICE_COOKIE}={TOKEN}"})


class TestAskRoute:
    async def test_a_follow_up_sees_the_earlier_turns_and_reports_usage(self, fastapi_client, history, model):
        history.extend([_row(1, answer="First answer."), _row(2, answer="Second answer.")])

        response = await _post(fastapi_client, {"question": "And on Shabbat?", "history_ids": _ids(history)})

        assert response.status_code == 200
        body = response.json()
        assert body["meta"]["guest"]["questions_used"] == 3
        assert body["meta"]["guest"]["questions_limit"] == guest_cap.THREAD_MAX_QUESTIONS
        assert body["meta"]["guest"]["remaining"] == guest_cap.THREAD_MAX_QUESTIONS - 3
        sent = model[-1]["conversation_history"]
        assert [t["content"] for t in sent] == [
            "Question 1?", "First answer.", "Question 2?", "Second answer."]
        # The answer is saved under the device, so the next follow-up can name it.
        assert history[-1]["user_id"] == OWNER

    async def test_another_devices_answers_are_not_shown_to_the_model(self, fastapi_client, history, model):
        history.extend([_row(1, owner=OTHER, answer="Someone else's.")])

        response = await _post(fastapi_client, {"question": "Hello?", "history_ids": _ids(history)})

        assert response.status_code == 200
        assert "conversation_history" not in model[-1]
        assert response.json()["meta"]["guest"]["questions_used"] == 1

    async def test_the_ninth_question_is_refused_before_the_model_runs(self, fastapi_client, history, model):
        history.extend(_row(n) for n in range(1, 9))

        response = await _post(fastapi_client, {"question": "One more?", "history_ids": _ids(history)})

        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["code"] == "guest_cap_reached"
        assert detail["reason"] == "thread_questions"
        assert detail["usage"]["remaining"] == 0
        assert model == []

    async def test_the_refusal_also_arrives_on_a_streamed_ask(self, fastapi_client, history, model):
        history.extend(_row(n) for n in range(1, 9))

        response = await fastapi_client.post(
            "/ask", json={"question": "One more?", "history_ids": _ids(history)},
            headers={"X-Forwarded-For": "203.0.113.91", "Accept": "application/x-ndjson",
                     "Cookie": f"{device.DEVICE_COOKIE}={TOKEN}"})

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "guest_cap_reached"

    async def test_the_day_stops_a_device_that_starts_a_new_conversation_each_time(
            self, fastapi_client, history, model):
        history.extend(_row(n, age_hours=2) for n in range(1, guest_cap.DAILY_MAX_QUESTIONS + 1))

        response = await _post(fastapi_client, {"question": "A brand new question?"}, forwarded="203.0.113.92")

        assert response.status_code == 403
        assert response.json()["detail"]["reason"] == "daily"
        assert model == []

    async def test_a_new_conversation_is_not_given_anyone_elses_history(self, fastapi_client, history, model):
        history.append(_row(1))

        response = await _post(fastapi_client, {"question": "Fresh?"}, forwarded="203.0.113.93")

        assert response.status_code == 200
        assert "conversation_history" not in model[-1]
        assert response.json()["meta"]["guest"]["questions_used"] == 1

    async def test_a_signed_in_ask_ignores_history_ids(self, fastapi_client, history, model, monkeypatch):
        monkeypatch.setattr(asgi, "extract_user_id_from_bearer_value", lambda _a: "user_abc")
        import backend.rate_limit as rate_limit_mod

        monkeypatch.setattr(rate_limit_mod, "extract_user_id_from_bearer_value", lambda _a: "user_abc")
        history.extend(_row(n) for n in range(1, 12))

        response = await fastapi_client.post(
            "/ask", json={"question": "Q?", "history_ids": _ids(history)},
            headers={"X-Forwarded-For": "203.0.113.94", "Authorization": "Bearer a.b.c"})

        assert response.status_code == 200
        assert "guest" not in response.json()["meta"]
        assert "conversation_history" not in model[-1]

    async def test_the_same_words_after_different_turns_are_not_served_from_cache(
            self, fastapi_client, history, model):
        history.append(_row(1, answer="Context one."))
        first = await _post(fastapi_client, {"question": "And then?", "history_ids": _ids(history)},
                            forwarded="203.0.113.95")
        history.append(_row(2, question="A different topic?", answer="Context two."))
        second = await _post(fastapi_client, {"question": "And then?", "history_ids": _ids(history[:2])},
                             forwarded="203.0.113.95")

        assert first.status_code == second.status_code == 200
        assert len(model) == 2
        assert model[0]["conversation_history"] != model[1]["conversation_history"]
