"""
tests/test_history.py — Unit tests for history.py (SQLite store).

Each test gets a fresh temp DB via the isolated_db fixture. No network.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import pytest

import history


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch):
    """Fresh DB per test. Resets module globals."""
    db_path = tmp_path / "history.db"
    # Reset module state so init_db runs
    monkeypatch.setattr(history, "_INITIALIZED", False)
    monkeypatch.setattr(history, "_DB_PATH", None)
    monkeypatch.setattr(history, "_FTS_AVAILABLE", False)
    monkeypatch.setattr(history, "_ENABLED", True)
    history.init_db(db_path)
    yield db_path
    # Cleanup: close any lingering connections (WAL files)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()


def _make_result(word: str, definition: str = "a test definition",
                 synonyms: list[str] | None = None) -> dict:
    return {
        "word": word,
        "phonetic": f"/{word}/",
        "source": "api",
        "meanings": [
            {
                "part_of_speech": "noun",
                "definitions": [{"definition": definition, "example": None}],
                "synonyms": synonyms or [],
                "antonyms": [],
            }
        ],
    }


# ---------------------------------------------------------------------------
# init_db / basic state
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_creates_file(self, isolated_db):
        assert isolated_db.exists()

    def test_init_is_idempotent(self, isolated_db):
        history.init_db(isolated_db)
        history.init_db(isolated_db)  # must not raise

    def test_fts_available_when_supported(self, isolated_db):
        # SQLite shipped with CPython has FTS5 on Linux
        assert history._FTS_AVAILABLE is True

    def test_is_enabled_default_true(self, isolated_db):
        assert history.is_enabled() is True

    def test_set_enabled_toggles(self, isolated_db):
        history.set_enabled(False)
        assert history.is_enabled() is False
        history.set_enabled(True)
        assert history.is_enabled() is True


# ---------------------------------------------------------------------------
# record_lookup + get_cached
# ---------------------------------------------------------------------------


class TestRecordAndCache:
    def test_record_returns_rowid(self, isolated_db):
        rowid = history.record_lookup("hello", _make_result("hello"))
        assert isinstance(rowid, int)
        assert rowid > 0

    def test_record_disabled_returns_none(self, isolated_db):
        history.set_enabled(False)
        assert history.record_lookup("hello", _make_result("hello")) is None

    def test_record_rejects_empty_word(self, isolated_db):
        assert history.record_lookup("", _make_result("")) is None
        assert history.record_lookup("   ", _make_result("")) is None

    def test_record_rejects_non_dict_result(self, isolated_db):
        assert history.record_lookup("hello", "not a dict") is None  # type: ignore[arg-type]

    def test_get_cached_hit(self, isolated_db):
        history.record_lookup("hello", _make_result("hello", "a greeting"))
        cached = history.get_cached("hello")
        assert cached is not None
        assert cached["word"] == "hello"
        assert cached["meanings"][0]["definitions"][0]["definition"] == "a greeting"

    def test_get_cached_miss(self, isolated_db):
        assert history.get_cached("nonexistent") is None

    def test_get_cached_case_insensitive(self, isolated_db):
        history.record_lookup("Hello", _make_result("Hello"))
        assert history.get_cached("hello") is not None
        assert history.get_cached("HELLO") is not None

    def test_get_cached_returns_latest(self, isolated_db):
        history.record_lookup("run", _make_result("run", "v1"))
        time.sleep(1.01)  # created_at is seconds — force monotonic change
        history.record_lookup("run", _make_result("run", "v2"))
        cached = history.get_cached("run")
        assert cached["meanings"][0]["definitions"][0]["definition"] == "v2"


# ---------------------------------------------------------------------------
# recent / favorites / set_favorite
# ---------------------------------------------------------------------------


class TestRecent:
    def test_recent_empty(self, isolated_db):
        assert history.recent() == []

    def test_recent_ordered_newest_first(self, isolated_db):
        history.record_lookup("alpha", _make_result("alpha"))
        time.sleep(1.01)
        history.record_lookup("beta", _make_result("beta"))
        r = history.recent()
        assert [x["word"] for x in r] == ["beta", "alpha"]

    def test_recent_dedupes_by_word(self, isolated_db):
        history.record_lookup("run", _make_result("run", "v1"))
        time.sleep(1.01)
        history.record_lookup("run", _make_result("run", "v2"))
        r = history.recent()
        assert len(r) == 1
        assert r[0]["result"]["meanings"][0]["definitions"][0]["definition"] == "v2"

    def test_recent_respects_limit(self, isolated_db):
        for i in range(5):
            history.record_lookup(f"w{i}", _make_result(f"w{i}"))
            time.sleep(0.01)
        assert len(history.recent(limit=3)) == 3


class TestFavorites:
    def test_set_favorite_on_then_off(self, isolated_db):
        history.record_lookup("star", _make_result("star"))
        history.set_favorite("star", True)
        favs = history.favorites()
        assert len(favs) == 1
        assert favs[0]["word"] == "star"
        assert favs[0]["is_favorite"] is True

        history.set_favorite("star", False)
        assert history.favorites() == []

    def test_favorite_persists_across_new_lookup(self, isolated_db):
        """COALESCE carries favorite flag forward on re-lookup."""
        history.record_lookup("star", _make_result("star"))
        history.set_favorite("star", True)
        history.record_lookup("star", _make_result("star", "v2"))
        favs = history.favorites()
        assert len(favs) == 1
        assert favs[0]["is_favorite"] is True


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_empty_query_returns_recent(self, isolated_db):
        history.record_lookup("hello", _make_result("hello"))
        assert len(history.search("")) == 1
        assert len(history.search("   ")) == 1

    def test_search_prefix_match(self, isolated_db):
        history.record_lookup("hello", _make_result("hello"))
        history.record_lookup("help", _make_result("help"))
        history.record_lookup("world", _make_result("world"))
        results = history.search("hel")
        words = {r["word"] for r in results}
        assert "hello" in words
        assert "help" in words
        assert "world" not in words

    def test_search_matches_definition_body(self, isolated_db):
        history.record_lookup("x", _make_result("x", "something about APPLES"))
        results = history.search("apples")
        assert any(r["word"] == "x" for r in results)

    def test_search_no_match_returns_empty(self, isolated_db):
        history.record_lookup("hello", _make_result("hello"))
        assert history.search("qqzzqqzz") == []

    def test_search_dedupes_by_word(self, isolated_db):
        history.record_lookup("run", _make_result("run", "v1"))
        time.sleep(1.01)
        history.record_lookup("run", _make_result("run", "v2"))
        results = history.search("run")
        assert len(results) == 1


# ---------------------------------------------------------------------------
# known_words + stats + export + clear
# ---------------------------------------------------------------------------


class TestKnownWords:
    def test_known_words_empty(self, isolated_db):
        assert history.known_words() == []

    def test_known_words_distinct_normalized(self, isolated_db):
        history.record_lookup("Hello", _make_result("Hello"))
        history.record_lookup("hello", _make_result("hello"))
        history.record_lookup("world", _make_result("world"))
        kw = set(history.known_words())
        assert kw == {"hello", "world"}


class TestStats:
    def test_stats_empty(self, isolated_db):
        s = history.stats()
        assert s["total"] == 0
        assert s["favorites"] == 0
        assert s["distinct_words"] == 0

    def test_stats_populated(self, isolated_db):
        history.record_lookup("a", _make_result("a"))
        history.record_lookup("b", _make_result("b"))
        history.record_lookup("a", _make_result("a", "v2"))
        history.set_favorite("a", True)
        s = history.stats()
        assert s["total"] == 3
        assert s["distinct_words"] == 2
        assert s["favorites"] == 1
        assert s["db_bytes"] > 0


class TestExportCsv:
    def test_export_empty(self, isolated_db, tmp_path):
        out = tmp_path / "export.csv"
        assert history.export_csv(out) == 0
        with out.open() as fh:
            rows = list(csv.reader(fh))
        assert rows[0][0] == "word"
        assert len(rows) == 1  # just header

    def test_export_roundtrip(self, isolated_db, tmp_path):
        history.record_lookup("hello", _make_result("hello", "a greeting"),
                              context="say hello", source="api")
        out = tmp_path / "export.csv"
        n = history.export_csv(out)
        assert n == 1
        with out.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert rows[0]["word"] == "hello"
        assert rows[0]["context"] == "say hello"
        assert rows[0]["source"] == "api"
        # result_json column must round-trip to the original dict
        parsed = json.loads(rows[0]["result_json"])
        assert parsed["word"] == "hello"


class TestClear:
    def test_clear_empties_db(self, isolated_db):
        history.record_lookup("hello", _make_result("hello"))
        history.record_lookup("world", _make_result("world"))
        history.clear()
        assert history.recent() == []
        assert history.stats()["total"] == 0
        # Search over empty DB must not error
        assert history.search("hello") == []
