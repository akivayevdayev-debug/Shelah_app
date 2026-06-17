"""
Minimal smoke test for backend/ask_pipeline.py.

ask_pipeline.py is explicitly NOT live code — its own module docstring states
it is not imported or called by app.py or asgi.py, and that `run_ask_pipeline`
depends on a `flask_app_module` shape that has not been verified end-to-end.

Per scope, this file intentionally does NOT exercise `run_ask_pipeline()`'s
internal logic. It only confirms the module imports cleanly and that its
two public symbols still exist with the expected basic shape.
"""

from __future__ import annotations

import backend.ask_pipeline as ask_pipeline_module


def test_module_imports_cleanly():
    """import backend.ask_pipeline must succeed with no side effects/errors."""
    assert ask_pipeline_module is not None


def test_run_ask_pipeline_is_still_defined():
    assert hasattr(ask_pipeline_module, "run_ask_pipeline")
    assert callable(ask_pipeline_module.run_ask_pipeline)


def test_ask_pipeline_result_is_still_defined():
    assert hasattr(ask_pipeline_module, "AskPipelineResult")
    assert isinstance(ask_pipeline_module.AskPipelineResult, type)


def test_ask_pipeline_result_constructs_with_no_args_and_defaults_to_none():
    """
    AskPipelineResult is a __slots__-based class whose __init__ sets every
    slot from kwargs.get(slot), defaulting to None when absent.
    """
    result = ask_pipeline_module.AskPipelineResult(**{})

    for slot in result.__slots__:
        assert getattr(result, slot) is None
