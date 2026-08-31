"""
Pins the shared `mock_outbound_httpx` fixture's Supabase response shapes
(plan.md §25) -- a real PostgREST response is never a {"data":...,
"error":...} envelope, it's a bare JSON array for both table ops and RPC
calls. This round-trips through the real supabase-py/postgrest-py client
construction (`app._get_supabase_client()`), not a hand-rolled fake, so a
future fixture edit that reintroduces a wrapper-envelope shape fails here
loudly instead of silently miscategorizing whatever code calls it next.
"""

from __future__ import annotations

import app


def test_table_insert_response_is_a_bare_list(mock_outbound_httpx):
    client = app._get_supabase_client()
    result = client.table("ask_history").insert({"question": "x"}).execute()
    assert isinstance(result.data, list)


def test_rpc_response_is_a_bare_list_of_row_dicts(mock_outbound_httpx):
    client = app._get_supabase_client()
    result = client.rpc(
        "check_and_reserve_user_budget",
        {
            "p_key_column": "user_id",
            "p_key_value": "test-user",
            "p_threshold_usd": 2.0,
            "p_reservation_usd": 0.01,
        },
    ).execute()
    assert isinstance(result.data, list)
    assert result.data and isinstance(result.data[0], dict)
    assert "allowed" in result.data[0]
