"""
dictionary.py — Core library for CLI Dictionary.

Public boundary contract (consumed by S02 and S03):
    lookup_word(word: str) -> dict | None
    format_definition(result: dict) -> str

lookup_word return shape (hard contract — do not change field names):
    {
        "word": str,
        "phonetic": str | None,
        "meanings": [
            {
                "part_of_speech": str,
                "definitions": [
                    {
                        "definition": str,
                        "example": str | None,
                    }
                ],
            }
        ],
    }
"""

from __future__ import annotations

from functools import lru_cache

import requests

API_BASE = "https://api.dictionaryapi.dev/api/v2/entries/en"
REQUEST_TIMEOUT = 10  # seconds
CACHE_SIZE = 256


@lru_cache(maxsize=CACHE_SIZE)
def _cached_lookup(word: str) -> dict | None:
    return _lookup_word_uncached(word)


def lookup_word(word: str) -> dict | None:
    if not isinstance(word, str) or not word.strip():
        return None
    return _cached_lookup(word.strip().lower())


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
            }
        )

    return {
        "word": entry.get("word", word),
        "phonetic": phonetic,
        "meanings": meanings,
    }


def format_definition(result: dict) -> str:
    """Format a lookup_word result dict into a plain multi-line string.

    Returns a plain str (not a rich renderable) so S02 can embed it in a
    popup without requiring a terminal.
    """
    lines: list[str] = []

    # Header: word + optional phonetic
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

    return "\n".join(lines)
