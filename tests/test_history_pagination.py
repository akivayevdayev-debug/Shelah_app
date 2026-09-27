"""
GET /api/user/history paging and search (deep-link Phase 6: the /history
page lists every stored answer, not just the sidebar's latest 20).

Keyset pagination on (created_at, id), newest first; ``q`` filters on the
question text. The fake table below applies the same filters PostgREST
would -- including parsing the ``or=`` keyset expression the route builds --
so the pages it returns are the pages a real database returns.
"""

import re

import pytest

import backend.auth as auth_module
import backend.routes_user as routes_user_module

USER = "user_pager"
OTHER = "user_other"
AUTH = {"Authorization": "Bearer test-token"}


def _uuid(n):
    return f"00000000-0000-4000-8000-{n:012d}"


def _ts(minute, frac=""):
    return f"2026-09-24T10:{minute:02d}:00{frac}+00:00"


def _like_to_regex(pattern):
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$", re.I | re.S)


_KEYSET_RE = re.compile(
    r'^created_at\.lt\."(?P<ts>[^"]+)",and\(created_at\.eq\."(?P=ts)",id\.lt\.(?P<id>[0-9a-f-]{36})\)$'
)


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, table):
        self.table = table
        self.filters = []
        self.orders = []
        self.limit_n = None
        self.or_expr = None
        self.ilike_pattern = None

    def select(self, columns):
        self.columns = columns.split(",")
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def ilike(self, key, pattern):
        assert key == "question"
        self.ilike_pattern = pattern
        return self

    def or_(self, expr):
        self.or_expr = expr
        return self

    def order(self, key, desc=False):
        self.orders.append((key, desc))
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def execute(self):
        self.table.queries.append(self)
        rows = [r for r in self.table.rows if all(r.get(k) == v for k, v in self.filters)]
        if self.ilike_pattern is not None:
            rx = _like_to_regex(self.ilike_pattern)
            rows = [r for r in rows if rx.match(r["question"])]
        if self.or_expr is not None:
            match = _KEYSET_RE.match(self.or_expr)
            assert match, self.or_expr
            ts, last_id = match["ts"], match["id"]
            rows = [r for r in rows if r["created_at"] < ts or (r["created_at"] == ts and r["id"] < last_id)]
        assert self.orders == [("created_at", True), ("id", True)]
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        rows = rows[: self.limit_n]
        return _Result([{c: r.get(c) for c in self.columns} for r in rows])


class _Table:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []


class _Client:
    def __init__(self, rows):
        self.history = _Table(rows)

    def table(self, name):
        return _Query(self.history)


def _row(n, minute, question, user=USER, frac=""):
    return {
        "id": _uuid(n), "user_id": user, "question": question, "answer": f"answer {n}",
        "sources": [], "ai_cited_sources": [], "community": "All", "mode": "balanced",
        "language": "en", "created_at": _ts(minute, frac),
    }


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(auth_module, "_verify_clerk_token", lambda token: {"sub": USER, "sid": "s"})


@pytest.fixture
def db(monkeypatch, authed):
    rows = [_row(n, minute=n, question=f"Question number {n} about Shabbat" if n % 2 else f"Question {n} on kashrut")
            for n in range(1, 13)]
    # Two answers stored in the same instant: the id breaks the tie.
    rows += [_row(20, 30, "Tie A about Shabbat"), _row(21, 30, "Tie B about Shabbat")]
    rows += [_row(40, 50, "Someone else's Shabbat question", user=OTHER)]
    client = _Client(rows)
    monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
    return client


def _page(test_client, **params):
    response = test_client.get("/api/user/history", query_string=params, headers=AUTH)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _walk(test_client, **params):
    """Every page, following next_cursor to the end."""
    pages, cursor = [], None
    while True:
        body = _page(test_client, **params, **({"cursor": cursor} if cursor else {}))
        pages.append(body)
        cursor = body["next_cursor"]
        if not cursor:
            return pages


class TestKeysetPagination:
    def test_pages_cover_every_own_row_once_newest_first(self, test_client, db):
        pages = _walk(test_client, limit=5)
        ids = [item["id"] for page in pages for item in page["items"]]
        expected = sorted((r for r in db.history.rows if r["user_id"] == USER),
                          key=lambda r: (r["created_at"], r["id"]), reverse=True)
        assert ids == [r["id"] for r in expected]
        assert [len(p["items"]) for p in pages] == [5, 5, 4]
        assert pages[-1]["next_cursor"] is None

    def test_rows_sharing_a_timestamp_split_across_pages_without_loss(self, test_client, db):
        first = _page(test_client, limit=1)
        assert first["items"][0]["id"] == _uuid(21)
        second = _page(test_client, limit=1, cursor=first["next_cursor"])
        assert second["items"][0]["id"] == _uuid(20)

    def test_a_row_stored_after_page_one_does_not_shift_page_two(self, test_client, db):
        first = _page(test_client, limit=3)
        db.history.rows.append(_row(99, 59, "Brand new question"))
        second = _page(test_client, limit=3, cursor=first["next_cursor"])
        seen = {i["id"] for i in first["items"]}
        assert not seen & {i["id"] for i in second["items"]}
        assert second["items"][0]["created_at"] < first["items"][-1]["created_at"] or (
            second["items"][0]["created_at"] == first["items"][-1]["created_at"]
        )

    def test_default_page_is_20_and_last_page_has_no_cursor(self, test_client, db):
        body = _page(test_client)
        assert len(body["items"]) == 14
        assert body["next_cursor"] is None
        assert db.history.queries[-1].limit_n == 21

    def test_limit_is_clamped_and_garbage_falls_back_to_the_default(self, test_client, db):
        _page(test_client, limit=9999)
        assert db.history.queries[-1].limit_n == 51
        _page(test_client, limit=0)
        assert db.history.queries[-1].limit_n == 2
        _page(test_client, limit="ten")
        assert db.history.queries[-1].limit_n == 21

    def test_every_query_is_scoped_to_the_caller(self, test_client, db):
        _walk(test_client, limit=4, user_id=OTHER)
        for query in db.history.queries:
            assert ("user_id", USER) in query.filters
            assert ("user_id", OTHER) not in query.filters

    def test_cursor_round_trips_microsecond_timestamps(self, test_client, monkeypatch, authed):
        rows = [_row(n, 5, f"q{n}", frac=".123456") for n in range(1, 4)]
        client = _Client(rows)
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        ids = [i["id"] for p in _walk(test_client, limit=1) for i in p["items"]]
        assert ids == [_uuid(3), _uuid(2), _uuid(1)]


class TestCursorValidation:
    @pytest.mark.parametrize("cursor", [
        "not-base64!",
        "e30",  # {}
        "WyJ4IiwieSJd",  # ["x","y"]
        "WzEsMl0",  # [1,2]
        "WyIyMDI2LTA5LTI0VDEwOjAwOjAwKzAwOjAwIiwibm90LWEtdXVpZCJd",  # [ts,"not-a-uuid"]
        "WyIyMDI2LTA5LTI0VDEwOjAwOjAwKzAwOjAwIiwiMDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDAxIiwieCJd",  # 3 items
        "WyInKSxpZC5ndC4wIiwiMDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDAxIl0",  # injection in ts
        "%%%",
    ])
    def test_a_cursor_this_api_did_not_issue_is_400_without_a_query(self, test_client, db, cursor):
        response = test_client.get("/api/user/history", query_string={"cursor": cursor}, headers=AUTH)
        assert response.status_code == 400
        assert response.get_json() == {"error": "Invalid cursor"}
        assert db.history.queries == []

    def test_blank_cursor_is_the_first_page(self, test_client, db):
        assert len(_page(test_client, cursor="  ")["items"]) == 14


class TestSearch:
    def test_filters_on_question_text_case_insensitively(self, test_client, db):
        body = _page(test_client, q="  SHABBAT  ")
        questions = [i["question"] for i in body["items"]]
        assert questions and all("shabbat" in q.lower() for q in questions)
        assert "Someone else's Shabbat question" not in questions
        assert db.history.queries[-1].ilike_pattern == "%SHABBAT%"

    def test_search_pages_with_the_same_cursor(self, test_client, db):
        pages = _walk(test_client, q="kashrut", limit=2)
        questions = [i["question"] for p in pages for i in p["items"]]
        assert questions == [f"Question {n} on kashrut" for n in (12, 10, 8, 6, 4, 2)]

    def test_wildcards_in_the_query_match_literally(self, test_client, monkeypatch, authed):
        rows = [_row(1, 1, "100% sure?"), _row(2, 2, "1000 sure?"), _row(3, 3, "a_b"), _row(4, 4, "axb"),
                _row(5, 5, "back\\slash")]
        client = _Client(rows)
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        assert [i["question"] for i in _page(test_client, q="100%")["items"]] == ["100% sure?"]
        assert [i["question"] for i in _page(test_client, q="a_b")["items"]] == ["a_b"]
        assert [i["question"] for i in _page(test_client, q="k\\s")["items"]] == ["back\\slash"]
        assert client.history.queries[0].ilike_pattern == "%100\\%%"

    def test_query_is_capped_and_whitespace_collapsed(self, test_client, db):
        _page(test_client, q="a  b\tc" + "x" * 500)
        pattern = db.history.queries[-1].ilike_pattern
        assert pattern.startswith("%a b cx")
        assert len(pattern) == 200 + 2

    def test_no_match_is_an_empty_last_page(self, test_client, db):
        assert _page(test_client, q="nothing like this") == {"items": [], "next_cursor": None}


def test_escape_like_and_cursor_helpers():
    assert routes_user_module._escape_like("50%_\\") == "50\\%\\_\\\\"
    cursor = routes_user_module._encode_history_cursor({"created_at": _ts(1), "id": _uuid(1)})
    assert "=" not in cursor
    assert routes_user_module._decode_history_cursor(cursor) == (_ts(1), _uuid(1))
    z = routes_user_module._encode_history_cursor({"created_at": "2026-09-24T10:00:00Z", "id": _uuid(2)})
    assert routes_user_module._decode_history_cursor(z) == ("2026-09-24T10:00:00Z", _uuid(2))
