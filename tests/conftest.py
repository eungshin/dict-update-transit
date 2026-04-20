"""Pytest fixtures shared across all test modules."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear lookup_word and ai_context caches before every test for isolation."""
    from dictionary import _cached_lookup
    import ai_context

    _cached_lookup.cache_clear()
    ai_context._pick_cache.clear()
    ai_context._explain_cache.clear()
    ai_context._client_cache.clear()
    yield
