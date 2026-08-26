"""
Price-table integrity tests for backend/cost_meter.py (plan.md §20a / Prompt
33a — "the cost ledger currently reads $0.00 for most production calls").

Background (plan.md §20.1-C1): `_PRICE_PER_M` did not contain an entry for
`gemini-3.5-flash-lite`, the production Gemini model
(`backend.claude._DEFAULT_GEMINI_MODEL`). `estimate_cost_usd()` silently
fell back to `_UNKNOWN_PRICE` (all zeros) for any unrecognized model, so
every Gemini-primary `/ask` call -- the majority of production traffic --
was logged to `ai_usage_log` at `cost_usd = 0.0`, with no warning anywhere.
Both the per-user ceiling and the global daily alert read that column, so
neither could ever fire on Gemini spend.

Pre-fix verification (§20a.1's golden master, run before this file's
inverted assertions were written): `_PRICE_PER_M` had no
"gemini-3.5-flash-lite" key, so
`estimate_cost_usd("gemini-3.5-flash-lite", 1000, 1000) == 0.0` on the code
as it existed before this pass -- confirmed by direct inspection of
backend/cost_meter.py's `_PRICE_PER_M` dict prior to the fix landing in the
same change-set. `test_production_gemini_model_is_priced` below is that
same assertion, inverted now that the price entry exists.
"""

from __future__ import annotations

import logging

import backend.claude as claude_module
import backend.cost_meter as cost_meter


def test_unknown_model_still_returns_zero_cost():
    """An unrecognized model name has no price and must not guess one --
    $0.00 for a genuinely-unknown model is correct, unchanged behavior.
    This was never the bug; the bug was the PRODUCTION model being
    unknown (see test_production_gemini_model_is_priced below)."""
    assert cost_meter.estimate_cost_usd("not-a-real-model-xyz", 1000, 1000) == 0.0


def test_production_gemini_model_is_priced():
    """Was $0.0 (plan.md §20.1-C1, verified against pre-fix _PRICE_PER_M).
    _PRICE_PER_M now carries a real, non-zero entry for the production
    Gemini model instead of silently falling back to _UNKNOWN_PRICE."""
    cost = cost_meter.estimate_cost_usd(
        claude_module._DEFAULT_GEMINI_MODEL, 1_000_000, 1_000_000
    )
    assert cost > 0.0
    # Pin the literal value (Google ai.google.dev pricing, verified
    # 2026-08-19: $0.30/1M input, $2.50/1M output) so a future accidental
    # edit to the price table is caught here, not on an invoice.
    assert cost == 0.30 + 2.50


class TestEveryDispatchableModelIsPriced:
    """The CI guard that matters: fails on the PR that bumps a model name,
    not three weeks later when the ledger total looks suspiciously low."""

    def test_all_dispatchable_models_have_positive_prices(self):
        missing = []
        for model in sorted(claude_module.get_dispatchable_models()):
            prices = cost_meter._PRICE_PER_M.get(model.lower())
            if not prices or prices["input"] <= 0 or prices["output"] <= 0:
                missing.append(model)
        assert not missing, (
            "backend/cost_meter.py's _PRICE_PER_M has no positive price "
            f"for dispatchable model(s): {missing}. Every model "
            "backend.claude.get_dispatchable_models() can actually call "
            "must have a real entry."
        )

    def test_guard_has_teeth(self, monkeypatch):
        """Proves the guard above can actually fail: temporarily rename the
        dispatched Gemini model to something unpriced and confirm the same
        check the test above runs goes red, then implicitly reverts via
        monkeypatch teardown."""
        monkeypatch.setattr(
            claude_module, "_DEFAULT_GEMINI_MODEL", "gemini-not-a-real-model-xyz"
        )
        missing = [
            model
            for model in claude_module.get_dispatchable_models()
            if not cost_meter._PRICE_PER_M.get(model.lower())
        ]
        assert "gemini-not-a-real-model-xyz" in missing


class TestUnpricedModelWarnsOnceLoudly:
    """§20a.2's "loud runtime fallback": an unpriced model must never again
    fail silently. Still returns $0.0 -- the fix does not guess a price."""

    def test_first_lookup_of_an_unpriced_model_logs_a_warning(self, caplog):
        cost_meter._WARNED_UNPRICED_MODELS.discard("unpriced-model-alpha")
        with caplog.at_level(logging.WARNING, logger="backend.cost_meter"):
            result = cost_meter.estimate_cost_usd("unpriced-model-alpha", 100, 100)

        assert result == 0.0
        assert any(
            "unpriced-model-alpha" in record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        )

    def test_second_lookup_of_the_same_model_does_not_warn_again(self, caplog):
        cost_meter._WARNED_UNPRICED_MODELS.discard("unpriced-model-beta")
        cost_meter.estimate_cost_usd("unpriced-model-beta", 100, 100)

        with caplog.at_level(logging.WARNING, logger="backend.cost_meter"):
            caplog.clear()
            cost_meter.estimate_cost_usd("unpriced-model-beta", 100, 100)

        assert not any(
            "unpriced-model-beta" in record.getMessage() for record in caplog.records
        )

    def test_a_priced_model_never_warns(self, caplog):
        cost_meter._WARNED_UNPRICED_MODELS.discard(
            claude_module._DEFAULT_GEMINI_MODEL.lower()
        )
        with caplog.at_level(logging.WARNING, logger="backend.cost_meter"):
            cost_meter.estimate_cost_usd(claude_module._DEFAULT_GEMINI_MODEL, 100, 100)

        assert not any(
            "no price entry" in record.getMessage() for record in caplog.records
        )
