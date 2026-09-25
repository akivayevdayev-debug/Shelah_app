"""
Feedback linked to the stored answer it rates (deep-link Phase 6).

The owner sends ``history_id``; a visitor on a shared link sends
``share_token``. The route stores the resolved ask_history row id -- only
for the caller's own row, or a share link that is still live -- and never
the token. Before scripts/sql/migrate_answer_feedback_history_link.sql runs,
the feedback is still saved, without the link.
"""

import pytest

import backend.routes_feedback as feedback_module

OWNER = "user_owner"
ENTRY = "0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88"
OTHER_ENTRY = "9f9f9f9f-2f5e-4c1d-9a7e-3d2b1c0a9f88"
# A share token's shape (16-64 URL-safe chars), plainly not a secret.
TOKEN = "t" * 22


class _APIError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, name):
        self.db, self.name, self.filters, self.payload = db, name, [], None

    def select(self, *_):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def limit(self, *_):
        return self

    def insert(self, payload):
        self.payload = payload
        return self

    def execute(self):
        if self.name == "ask_history":
            self.db.lookups.append(self.filters)
            if self.db.lookup_error:
                raise self.db.lookup_error
            rows = [r for r in self.db.history if all(r.get(k) == v for k, v in self.filters)]
            return _Result([{"id": r["id"]} for r in rows])
        self.db.insert_attempts.append(dict(self.payload))
        if self.db.link_columns_missing and ("history_id" in self.payload or "shared_view" in self.payload):
            raise _APIError(self.db.link_columns_missing)
        if self.db.insert_error:
            raise self.db.insert_error
        self.db.feedback.append(dict(self.payload))
        return _Result([self.payload])


class _DB:
    def __init__(self):
        self.history = [
            {"id": ENTRY, "user_id": OWNER, "share_token": TOKEN, "is_public": True},
            {"id": OTHER_ENTRY, "user_id": "user_other", "share_token": None, "is_public": False},
        ]
        self.feedback, self.insert_attempts, self.lookups = [], [], []
        self.link_columns_missing = None
        self.lookup_error = None
        self.insert_error = None

    def table(self, name):
        return _Query(self, name)


@pytest.fixture
def db(monkeypatch):
    database = _DB()
    monkeypatch.setattr(feedback_module, "_get_supabase_client", lambda: database)
    monkeypatch.setattr(feedback_module, "_get_request_user_id", lambda: None)
    return database


@pytest.fixture
def as_owner(monkeypatch):
    monkeypatch.setattr(feedback_module, "_get_request_user_id", lambda: OWNER)


def _post(test_client, **extra):
    body = {"question": "May I carry on Shabbat?", "verdict": "helpful", **extra}
    response = test_client.post("/api/feedback", json=body)
    assert response.status_code == 200, response.get_json()
    return response


def test_owner_feedback_links_their_own_answer(test_client, db, as_owner):
    _post(test_client, history_id=ENTRY)
    assert db.feedback[-1]["history_id"] == ENTRY
    assert db.feedback[-1]["shared_view"] is False
    assert db.lookups[-1] == [("id", ENTRY), ("user_id", OWNER)]


def test_someone_elses_history_id_is_not_linked(test_client, db, as_owner):
    _post(test_client, history_id=OTHER_ENTRY)
    assert "history_id" not in db.feedback[-1]


def test_signed_out_history_id_is_ignored_without_a_lookup(test_client, db):
    _post(test_client, history_id=ENTRY)
    assert "history_id" not in db.feedback[-1]
    assert db.lookups == []


@pytest.mark.parametrize("bad", ["entry-1", "", None, 42, "0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f8"])
def test_malformed_history_id_is_ignored_without_a_lookup(test_client, db, as_owner, bad):
    _post(test_client, history_id=bad)
    assert "history_id" not in db.feedback[-1]
    assert db.lookups == []


def test_visitor_feedback_through_a_live_share_link_is_linked_by_row_id(test_client, db):
    _post(test_client, share_token=TOKEN)
    stored = db.feedback[-1]
    assert stored["history_id"] == ENTRY
    assert stored["shared_view"] is True
    assert TOKEN not in stored.values()
    assert db.lookups[-1] == [("share_token", TOKEN), ("is_public", True)]


def test_share_token_wins_over_a_history_id_in_the_same_payload(test_client, db, as_owner):
    _post(test_client, share_token=TOKEN, history_id=OTHER_ENTRY)
    assert db.feedback[-1]["history_id"] == ENTRY
    assert db.feedback[-1]["shared_view"] is True


def test_revoked_share_link_saves_unlinked_feedback(test_client, db):
    db.history[0].update(share_token=None, is_public=False)
    _post(test_client, share_token=TOKEN)
    assert "history_id" not in db.feedback[-1]


@pytest.mark.parametrize("bad", ["short", "has space in it xxxxxxx", "x" * 65, "../../api/user"])
def test_malformed_share_token_is_ignored_without_a_lookup(test_client, db, bad):
    _post(test_client, share_token=bad)
    assert "history_id" not in db.feedback[-1]
    assert db.lookups == []


def test_a_failed_lookup_saves_unlinked_feedback(test_client, db):
    db.lookup_error = _APIError("42703")  # share columns not migrated yet
    _post(test_client, share_token=TOKEN)
    assert db.feedback[-1]["verdict"] == "helpful"
    assert "history_id" not in db.feedback[-1]


@pytest.mark.parametrize("code", ["42703", "PGRST204"])
def test_before_the_link_migration_the_verdict_is_still_saved(test_client, db, as_owner, code):
    db.link_columns_missing = code
    _post(test_client, history_id=ENTRY)
    assert len(db.insert_attempts) == 2
    assert db.insert_attempts[0]["history_id"] == ENTRY
    assert db.feedback == [db.insert_attempts[1]]
    assert "history_id" not in db.feedback[0]


def test_any_other_insert_error_is_still_a_500(test_client, db, as_owner):
    db.insert_error = _APIError("23505")
    response = test_client.post("/api/feedback", json={"question": "q", "verdict": "helpful", "history_id": ENTRY})
    assert response.status_code == 500
    assert len(db.insert_attempts) == 1


def test_unlinked_feedback_inserts_exactly_the_old_record(test_client, db):
    _post(test_client)
    assert set(db.feedback[-1]) == {
        "user_id", "question_hash", "verdict", "comment", "mode", "language", "fallback", "safety_class",
    }
    assert len(db.insert_attempts) == 1
