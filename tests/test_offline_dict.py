"""
tests/test_offline_dict.py — Unit tests for offline_dict.py.

nltk is an optional dep and may not be installed in CI, so tests fall into
two groups:

1. Module-level graceful-degradation tests (no nltk needed)
2. Mocked-wordnet tests that stub the nltk.corpus.wordnet surface we use.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

import offline_dict


# ---------------------------------------------------------------------------
# Graceful degradation when nltk absent
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_is_available_false_when_nltk_missing(self, monkeypatch):
        # Force the nltk import to fail by injecting a broken stub
        monkeypatch.setitem(sys.modules, "nltk", None)
        assert offline_dict.is_available() is False

    def test_lookup_returns_none_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(offline_dict, "is_available", lambda: False)
        assert offline_dict.lookup("hello") is None

    def test_lookup_rejects_blank(self):
        assert offline_dict.lookup("") is None
        assert offline_dict.lookup("   ") is None

    def test_known_words_sample_empty_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(offline_dict, "is_available", lambda: False)
        assert offline_dict.known_words_sample(100) == []

    def test_known_words_count_zero_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(offline_dict, "is_available", lambda: False)
        assert offline_dict.known_words_count() == 0

    def test_download_returns_false_when_nltk_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "nltk", None)
        assert offline_dict.download() is False


# ---------------------------------------------------------------------------
# Mocked WordNet: simulate the subset of nltk.corpus.wordnet we use
# ---------------------------------------------------------------------------


class FakeLemma:
    def __init__(self, name: str, antonyms: list["FakeLemma"] | None = None):
        self._name = name
        self._antonyms = antonyms or []

    def name(self) -> str:
        return self._name

    def antonyms(self) -> list["FakeLemma"]:
        return self._antonyms


class FakeSynset:
    def __init__(self, pos: str, definition: str,
                 examples: list[str],
                 lemmas: list[FakeLemma]):
        self._pos = pos
        self._def = definition
        self._examples = examples
        self._lemmas = lemmas

    def pos(self) -> str:
        return self._pos

    def definition(self) -> str:
        return self._def

    def examples(self) -> list[str]:
        return self._examples

    def lemmas(self) -> list[FakeLemma]:
        return self._lemmas


@pytest.fixture
def fake_wordnet(monkeypatch):
    """Install a fake `nltk.corpus.wordnet` for lookup tests."""
    ant_walk = FakeLemma("walk")
    run_verb = FakeSynset(
        pos="v",
        definition="move at a speed faster than a walk",
        examples=["she ran home"],
        lemmas=[FakeLemma("run", [ant_walk]), FakeLemma("sprint"), FakeLemma("dash")],
    )
    run_noun = FakeSynset(
        pos="n",
        definition="an act or spell of running",
        examples=[],
        lemmas=[FakeLemma("run"), FakeLemma("jog")],
    )

    wn_stub = MagicMock()
    wn_stub.synsets = MagicMock(side_effect=lambda w: {
        "run": [run_verb, run_noun],
        "xyzzy": [],
    }.get(w, []))
    wn_stub.all_lemma_names = MagicMock(return_value=iter(
        ["apple", "apple", "banana", "carrot", "dog", "elephant", "fig"]
    ))
    wn_stub.ensure_loaded = MagicMock()

    # Force is_available to return True regardless of real nltk presence
    monkeypatch.setattr(offline_dict, "is_available", lambda: True)

    # Install fake module structure so `from nltk.corpus import wordnet` works
    fake_nltk = types.ModuleType("nltk")
    fake_corpus = types.ModuleType("nltk.corpus")
    fake_corpus.wordnet = wn_stub
    fake_nltk.corpus = fake_corpus
    fake_nltk.data = types.SimpleNamespace(path=[])
    monkeypatch.setitem(sys.modules, "nltk", fake_nltk)
    monkeypatch.setitem(sys.modules, "nltk.corpus", fake_corpus)
    return wn_stub


class TestLookupWithFakeWordnet:
    def test_unknown_word_returns_none(self, fake_wordnet):
        assert offline_dict.lookup("xyzzy") is None

    def test_known_word_shape(self, fake_wordnet):
        result = offline_dict.lookup("run")
        assert result is not None
        # Matches the extended lookup_word contract
        assert {"word", "phonetic", "source", "meanings"}.issubset(result.keys())
        assert result["source"] == "wordnet"
        assert result["phonetic"] is None
        assert result["word"] == "run"

    def test_meanings_grouped_by_pos(self, fake_wordnet):
        result = offline_dict.lookup("run")
        pos_tags = [m["part_of_speech"] for m in result["meanings"]]
        assert "verb" in pos_tags
        assert "noun" in pos_tags

    def test_synonyms_exclude_lookup_word(self, fake_wordnet):
        result = offline_dict.lookup("run")
        for m in result["meanings"]:
            assert "run" not in [s.lower() for s in m["synonyms"]]

    def test_synonyms_included(self, fake_wordnet):
        result = offline_dict.lookup("run")
        verb_meaning = next(m for m in result["meanings"]
                            if m["part_of_speech"] == "verb")
        assert "sprint" in verb_meaning["synonyms"]
        assert "dash" in verb_meaning["synonyms"]

    def test_antonyms_extracted(self, fake_wordnet):
        result = offline_dict.lookup("run")
        verb_meaning = next(m for m in result["meanings"]
                            if m["part_of_speech"] == "verb")
        assert "walk" in verb_meaning["antonyms"]

    def test_definitions_and_examples(self, fake_wordnet):
        result = offline_dict.lookup("run")
        verb_meaning = next(m for m in result["meanings"]
                            if m["part_of_speech"] == "verb")
        defn = verb_meaning["definitions"][0]
        assert defn["definition"].startswith("move at a speed")
        assert defn["example"] == "she ran home"

    def test_empty_examples_yield_none(self, fake_wordnet):
        result = offline_dict.lookup("run")
        noun_meaning = next(m for m in result["meanings"]
                            if m["part_of_speech"] == "noun")
        assert noun_meaning["definitions"][0]["example"] is None

    def test_known_words_sample_distinct_and_capped(self, fake_wordnet):
        sample = offline_dict.known_words_sample(3)
        assert len(sample) == 3
        assert len(set(sample)) == 3
        assert sample == sorted(sample)

    def test_known_words_sample_zero(self, fake_wordnet):
        assert offline_dict.known_words_sample(0) == []
