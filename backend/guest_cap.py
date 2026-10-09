"""
Limits for signed-out ("guest") conversations.

A signed-out visitor can follow up on an answer, but only up to a point; past it
the next question asks them to sign in. The numbers (all overridable by env):

* ``GUEST_THREAD_MAX_QUESTIONS`` (8) questions per guest conversation, and
  ``GUEST_THREAD_MAX_TOKENS`` (24,000) answer tokens, whichever runs out first.
* One document-style request per conversation (a study guide, a table: the
  asks claude.question_profile calls a study question) does not count toward the
  questions and brings ``GUEST_DOC_EXTRA_TOKENS`` (6,000) of its own.
* ``GUEST_DAILY_MAX_QUESTIONS`` (24, about three full conversations) per device
  in any rolling 24 hours. A conversation is not a row anywhere, so this is also
  what bounds someone who starts a new one for every question.

Server-authoritative, with no schema change. The client names the earlier
answers of its conversation (``history_ids``); only the rows that device owns
(``ask_history.user_id == device:<hash>``, backend/device_identity.py) are used,
so a forged id yields nothing, and the same rows are what the model sees as the
conversation so far. Usage is derived from them: questions are rows, tokens are
estimated from the stored answer's length, the day's count is the owner's rows
since 24 hours ago (index ``(user_id, created_at DESC)``). The estimate is
deliberately a little pessimistic (``CHARS_PER_TOKEN``), and these are limits to
nudge a sign-in, not billing.

What this does not stop: clearing cookies mints a new device, so the counter
starts over. The backstops there are the per-IP rate limit, the global budget
and the WAF. Every lookup here fails open: a database that is down must not turn
into "guests can't ask", and the same outage means nothing is being stored.
"""

import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from backend.logging_setup import _capture_backend_error

CAP_CODE = "guest_cap_reached"


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


THREAD_MAX_QUESTIONS = _env_int("GUEST_THREAD_MAX_QUESTIONS", 8)
THREAD_MAX_TOKENS = _env_int("GUEST_THREAD_MAX_TOKENS", 24000)
DOC_EXTRA_TOKENS = _env_int("GUEST_DOC_EXTRA_TOKENS", 6000)
DAILY_MAX_QUESTIONS = _env_int("GUEST_DAILY_MAX_QUESTIONS", 24)

# A conversation older than this many answers can't be a guest one that is still
# allowed to continue; ids past it are ignored (and bound the lookup).
MAX_HISTORY_IDS = THREAD_MAX_QUESTIONS + 4

# Answers are English and Hebrew; Hebrew tokenises worse than English, so this
# sits between the two rather than at the usual 4.
CHARS_PER_TOKEN = 3

RETRIEVAL_CONTEXT_QUESTIONS = 3

_ID_RE =re.compile(r"^[A-Za-z0-9-]{8,64}$")


@dataclass
class GuestThread:
    """What a guest ``/ask`` knows about its conversation before answering."""

    owner: str
    rows: list = field(default_factory=list)
    daily_used: int = 0
    document: bool = False  # this question is the document-style one
    document_free: bool = False  # ... and it is the conversation's free one
    prior_documents: int = 0
    prior_counted: int = 0
    prior_tokens: int = 0

    @property
    def history(self) -> list:
        """The conversation so far, shaped for claude.build_history_turns()."""
        turns = []
        for row in self.rows:
            turns.append({"role": "user", "content": str(row.get("question") or "")})
            turns.append({"role": "assistant", "content": str(row.get("answer") or "")})
        return turns

    @property
    def retrieval_context(self) -> tuple:
        """Earlier questions, newest first, for source retrieval: a follow-up
        such as "and for a woman?" names no topic of its own (the same hand-off
        a signed-in conversation makes, routes_conversations._earlier_questions)."""
        questions = [str(row.get("question") or "").strip() for row in reversed(self.rows)]
        return tuple(q for q in questions if q)[:RETRIEVAL_CONTEXT_QUESTIONS]

    @property
    def cache_scope(self) -> str:
        """Part of the answer-cache key: an answer that depends on earlier
        turns must not be served for the same words asked elsewhere."""
        return "|".join(str(row.get("id") or "") for row in self.rows)

    @property
    def tokens_limit(self) -> int:
        extra = DOC_EXTRA_TOKENS if (self.prior_documents or self.document) else 0
        return THREAD_MAX_TOKENS + extra


def estimate_tokens(text) -> int:
    return math.ceil(len(str(text or "")) / CHARS_PER_TOKEN)


def is_document_question(question) -> bool:
    from backend import claude  # lazy: claude is heavy and imports half the backend

    try:
        return bool(claude.question_profile(str(question or ""))[1])
    except Exception:
        return False


def _rows_since(supabase, table, owner, since_iso) -> int:
    result = (
        supabase.table(table)
        .select("id", count="exact")
        .eq("user_id", owner)
        .gte("created_at", since_iso)
        .limit(1)
        .execute()
    )
    count = getattr(result, "count", None)
    return count if isinstance(count, int) else 0


def _load_rows(supabase, table, owner, history_ids) -> list:
    ids = []
    for raw in history_ids or []:
        value = str(raw or "").strip()
        if _ID_RE.match(value) and value not in ids:
            ids.append(value)
        if len(ids) >= MAX_HISTORY_IDS:
            break
    if not ids:
        return []
    result = (
        supabase.table(table)
        .select("id,question,answer,created_at")
        .eq("user_id", owner)
        .in_("id", ids)
        .order("created_at", desc=False)
        .execute()
    )
    rows = result.data if isinstance(getattr(result, "data", None), list) else []
    return [row for row in rows if isinstance(row, dict)]


def load_thread(owner, history_ids, question, *, now=None) -> GuestThread | None:
    """The guest's conversation so far plus today's count, or None when there is
    nothing to measure (no device, no database, or a failed lookup)."""
    if not owner:
        return None
    try:
        import app as _app

        supabase = _app._get_supabase_client()
        if not supabase:
            return None
        table = _app.SUPABASE_ASK_HISTORY_TABLE
        now = now or datetime.now(timezone.utc)
        since = (now - timedelta(hours=24)).isoformat()
        rows = _load_rows(supabase, table, owner, history_ids)
        daily_used = _rows_since(supabase, table, owner, since)
    except Exception as exc:
        _capture_backend_error("guest_cap_lookup_failed", exc, {"owner_kind": "device"})
        return None

    prior_documents = sum(1 for row in rows if is_document_question(row.get("question")))
    document = is_document_question(question)
    return GuestThread(
        owner=owner,
        rows=rows,
        daily_used=daily_used,
        document=document,
        document_free=document and prior_documents == 0,
        prior_documents=prior_documents,
        prior_counted=len(rows) - min(prior_documents, 1),
        prior_tokens=sum(estimate_tokens(row.get("answer")) for row in rows),
    )


def refusal_reason(thread: GuestThread) -> str | None:
    """Why this question can't be asked, or None when it can. The day's count is
    checked first: it is the one a new conversation can't escape."""
    if thread.daily_used >= DAILY_MAX_QUESTIONS:
        return "daily"
    if not thread.document_free and thread.prior_counted >= THREAD_MAX_QUESTIONS:
        return "thread_questions"
    if thread.prior_tokens >= thread.tokens_limit:
        return "thread_tokens"
    return None


def usage_payload(thread: GuestThread, answer_text="", *, stored=True) -> dict:
    """The counters the client shows, as they stand once this question has been
    answered (``stored`` False: the answer was not saved, so it is not counted,
    exactly as the next request will find it)."""
    counted = stored and not thread.document_free
    questions_used = thread.prior_counted + (1 if counted else 0)
    tokens_used = thread.prior_tokens + (estimate_tokens(answer_text) if stored else 0)
    daily_used = thread.daily_used + (1 if stored else 0)
    tokens_limit = thread.tokens_limit

    thread_left = max(0, THREAD_MAX_QUESTIONS - questions_used)
    if tokens_used >= tokens_limit:
        thread_left = 0
    daily_left = max(0, DAILY_MAX_QUESTIONS - daily_used)
    return {
        "questions_used": questions_used,
        "questions_limit": THREAD_MAX_QUESTIONS,
        "tokens_used": tokens_used,
        "tokens_limit": tokens_limit,
        "daily_used": daily_used,
        "daily_limit": DAILY_MAX_QUESTIONS,
        "remaining": min(thread_left, daily_left),
        # What the next refusal will be about, so the banner and the modal agree.
        "binding": "daily" if daily_left <= thread_left else "thread",
    }


def refusal_payload(thread: GuestThread, reason: str) -> dict:
    """HTTPException ``detail`` for a refused question (the shape
    turnstile_required uses: ``{error, code}``, plus what the modal needs)."""
    messages = {
        "daily": "You've used today's guest questions. Sign in to keep asking.",
        "thread_questions": "You've reached the guest limit for this conversation. Sign in to keep going.",
        "thread_tokens": "This conversation has used its guest allowance. Sign in to keep going.",
    }
    return {
        "error": messages.get(reason, messages["thread_questions"]),
        "code": CAP_CODE,
        "reason": reason,
        "usage": usage_payload(thread, stored=False),
    }
