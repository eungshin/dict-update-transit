"""
dictionary.py — Core library for CLI Dictionary.

Public boundary contract (consumed by S02 and S03):
    lookup_word(word: str) -> dict | None
    format_definition(result: dict) -> str
    suggest(word: str, limit: int = 5) -> list[str]

lookup_word return shape:
    {
        "word": str,
        "phonetic": str | None,
        "source": str,                 # "api" | "cache" | "wordnet"
        "meanings": [
            {
                "part_of_speech": str,
                "definitions": [
                    {"definition": str, "example": str | None}
                ],
                "synonyms": [str, ...],
                "antonyms": [str, ...],
            }
        ],
    }

Backwards compatibility: every pre-existing key name and type is preserved.
The added fields (`source`, `synonyms`, `antonyms`) are optional extensions
that older consumers can ignore.
"""

from __future__ import annotations

import difflib
import logging
from functools import lru_cache

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.dictionaryapi.dev/api/v2/entries/en"
REQUEST_TIMEOUT = 10  # seconds
CACHE_SIZE = 256


@lru_cache(maxsize=CACHE_SIZE)
def _cached_lookup(word: str) -> dict | None:
    return _lookup_word_uncached(word)


def lookup_word(word: str) -> dict | None:
    """Look up *word* with a 3-tier fallback chain.

    1. SQLite history cache (skipped if history is disabled or uninitialized)
    2. Free Dictionary API (in-memory LRU layered on top)
    3. WordNet offline dictionary (if the optional nltk package is installed)

    Returns None for blank input or when the word is genuinely not found in
    any tier. Re-raises requests.RequestException only when all offline
    fallbacks also fail.
    """
    if not isinstance(word, str) or not word.strip():
        return None
    norm = word.strip().lower()

    # Tier 1 — SQLite history cache
    cached = _history_get_cached(norm)
    if cached is not None:
        return {**cached, "source": "cache"}

    # Tier 2 — Free Dictionary API (wrapped in in-memory LRU)
    try:
        result = _cached_lookup(norm)
    except requests.RequestException:
        # Tier 3 — WordNet offline
        wn_result = _wordnet_lookup(norm)
        if wn_result is not None:
            return wn_result
        raise

    if result is not None:
        result = dict(result, source="api")
    return result


def _lookup_word_uncached(word: str) -> dict | None:
    """Call the Free Dictionary API and return normalized data for *word*.

    Returns None when:
    - *word* is empty or whitespace-only
    - the API reports the word is not found (HTTP 404)

    Raises requests.RequestException for connectivity failures or unexpected
    HTTP errors (anything other than 200 and 404).
    """
    response = requests.get(f"{API_BASE}/{word}", timeout=REQUEST_TIMEOUT)

    if response.status_code == 404:
        return None

    # Raise for any unexpected HTTP error (5xx, 4xx other than 404, etc.)
    response.raise_for_status()

    data = response.json()
    entry = data[0]

    phonetic: str | None = entry.get("phonetic") or None

    # If top-level phonetic is absent, fall back to first phonetics entry with text
    if phonetic is None:
        for ph in entry.get("phonetics", []):
            if ph.get("text"):
                phonetic = ph["text"]
                break

    meanings: list[dict] = []
    for raw_meaning in entry.get("meanings", []):
        part_of_speech = raw_meaning.get("partOfSpeech", "")
        definitions: list[dict] = []
        for raw_def in raw_meaning.get("definitions", []):
            definitions.append(
                {
                    "definition": raw_def.get("definition", ""),
                    "example": raw_def.get("example") or None,
                }
            )
        meanings.append(
            {
                "part_of_speech": part_of_speech,
                "definitions": definitions,
                "synonyms": list(raw_meaning.get("synonyms", []) or []),
                "antonyms": list(raw_meaning.get("antonyms", []) or []),
            }
        )

    return {
        "word": entry.get("word", word),
        "phonetic": phonetic,
        "meanings": meanings,
    }


def format_definition(result: dict) -> str:
    """Format a lookup_word result dict into a plain multi-line string."""
    lines: list[str] = []

    word = result.get("word", "")
    phonetic = result.get("phonetic")
    if phonetic:
        lines.append(f"{word}  [{phonetic}]")
    else:
        lines.append(word)

    for meaning in result.get("meanings", []):
        part_of_speech = meaning.get("part_of_speech", "")
        lines.append(f"\n{part_of_speech}")
        lines.append("-" * len(part_of_speech))

        for i, defn in enumerate(meaning.get("definitions", []), start=1):
            definition_text = defn.get("definition", "")
            lines.append(f"  {i}. {definition_text}")

            example = defn.get("example")
            if example:
                lines.append(f'     Example: "{example}"')

        synonyms = meaning.get("synonyms") or []
        antonyms = meaning.get("antonyms") or []
        if synonyms:
            lines.append(f"     Synonyms: {', '.join(synonyms)}")
        if antonyms:
            lines.append(f"     Antonyms: {', '.join(antonyms)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Did-you-mean
# ---------------------------------------------------------------------------


def suggest(word: str, limit: int = 5) -> list[str]:
    """Return close-match suggestions for *word*.

    Pulls candidate words from user history and (if installed) the WordNet
    lexicon, then uses difflib to rank by similarity. Returns an empty list
    when no candidates score high enough or when no source is available.
    """
    if not isinstance(word, str) or not word.strip():
        return []
    target = word.strip().lower()
    candidates: set[str] = set()

    # From user's own history — cheap and zero-config
    try:
        import history  # local module
        candidates.update(history.known_words())
    except Exception:
        pass

    # From WordNet if available
    try:
        import offline_dict
        if offline_dict.is_available():
            candidates.update(offline_dict.known_words_sample(20000))
    except Exception:
        pass

    # Drop exact match itself so suggestions are actually new
    candidates.discard(target)

    if not candidates:
        return []

    return difflib.get_close_matches(target, candidates, n=max(1, int(limit)), cutoff=0.7)


# ---------------------------------------------------------------------------
# Internal hooks — kept as private helpers so the module stays importable
# even when history.py or offline_dict.py are not set up yet.
# ---------------------------------------------------------------------------


def _history_get_cached(word: str) -> dict | None:
    try:
        import history
        if not history.is_enabled():
            return None
        return history.get_cached(word)
    except Exception:
        return None


def _wordnet_lookup(word: str) -> dict | None:
    try:
        import offline_dict
        if not offline_dict.is_available():
            return None
        return offline_dict.lookup(word)
    except Exception:
        return None
