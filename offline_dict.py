"""
offline_dict.py — WordNet-backed offline dictionary.

Optional module. Requires `nltk` (installed via `pip install -e .[offline]`).
When the wordnet + omw-1.4 corpora are downloaded, lookup() returns results
shaped identically to dictionary.lookup_word() so the popup can render them
without any branching.

Public API
----------
    is_available() -> bool
    download(progress_cb=None) -> bool
    lookup(word) -> dict | None
    known_words_sample(n: int) -> list[str]
    known_words_count() -> int
    nltk_data_dir() -> Path
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# POS tag (WordNet) → part_of_speech label used elsewhere in the app.
_POS_MAP = {
    "n": "noun",
    "v": "verb",
    "a": "adjective",
    "s": "adjective",  # adjective satellite
    "r": "adverb",
}


def nltk_data_dir() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / "dict-tool" / "nltk_data"


def _configure_nltk_path() -> None:
    """Register our private data dir with nltk.data.path (idempotent)."""
    try:
        import nltk  # type: ignore[import-not-found]
    except ImportError:
        return
    p = str(nltk_data_dir())
    if p not in nltk.data.path:
        nltk.data.path.insert(0, p)


def is_available() -> bool:
    """True iff nltk + the wordnet corpus are importable and findable."""
    try:
        import nltk  # noqa: F401  # type: ignore[import-not-found]
    except ImportError:
        return False
    _configure_nltk_path()
    try:
        from nltk.corpus import wordnet as wn  # type: ignore[import-not-found]
        # Trigger lazy load — raises LookupError if corpus missing
        wn.ensure_loaded()
        return True
    except LookupError:
        return False
    except Exception as exc:
        logger.debug("offline_dict.is_available probe failed: %s", exc)
        return False


def download(progress_cb: Callable[[str], None] | None = None) -> bool:
    """Download wordnet + omw-1.4 corpora into our private data dir.

    `progress_cb(msg)` is called with short status strings so callers can
    display progress. Returns True on success. Does not raise — exceptions
    are swallowed and logged so the UI can show a friendly error.
    """
    try:
        import nltk  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("nltk not installed — pip install -e .[offline]")
        return False

    target = nltk_data_dir()
    target.mkdir(parents=True, exist_ok=True)
    _configure_nltk_path()

    for pkg in ("wordnet", "omw-1.4"):
        if progress_cb:
            progress_cb(f"Downloading {pkg}…")
        try:
            ok = nltk.download(pkg, download_dir=str(target), quiet=True)
            if not ok:
                logger.warning("nltk.download(%s) returned False", pkg)
                return False
        except Exception as exc:
            logger.warning("nltk.download(%s) failed: %s", pkg, exc)
            return False

    if progress_cb:
        progress_cb("Done")
    return is_available()


def lookup(word: str) -> dict | None:
    """Return a lookup_word()-shaped dict for *word*, or None if unknown."""
    if not isinstance(word, str) or not word.strip():
        return None
    if not is_available():
        return None
    try:
        from nltk.corpus import wordnet as wn  # type: ignore[import-not-found]
    except ImportError:
        return None

    norm = word.strip().lower().replace(" ", "_")
    synsets = wn.synsets(norm)
    if not synsets:
        return None

    # Group synsets by POS so each part_of_speech gets one meaning entry.
    # Preserve encounter order.
    grouped: "dict[str, list]" = {}
    for syn in synsets:
        pos = _POS_MAP.get(syn.pos(), syn.pos())
        grouped.setdefault(pos, []).append(syn)

    meanings: list[dict] = []
    for pos, syns in grouped.items():
        definitions: list[dict] = []
        synonyms: list[str] = []
        antonyms: list[str] = []
        seen_syn: set[str] = set()
        seen_ant: set[str] = set()

        for syn in syns:
            defn = (syn.definition() or "").strip()
            examples = syn.examples() or []
            example = examples[0] if examples else None
            definitions.append({"definition": defn, "example": example})

            for lemma in syn.lemmas():
                name = lemma.name().replace("_", " ")
                if name.lower() != word.strip().lower() and name not in seen_syn:
                    seen_syn.add(name)
                    synonyms.append(name)
                for ant in lemma.antonyms():
                    aname = ant.name().replace("_", " ")
                    if aname not in seen_ant:
                        seen_ant.add(aname)
                        antonyms.append(aname)

        meanings.append({
            "part_of_speech": pos,
            "definitions": definitions,
            "synonyms": synonyms[:20],    # cap to keep popup readable
            "antonyms": antonyms[:20],
        })

    return {
        "word": word.strip(),
        "phonetic": None,    # WordNet has no IPA data
        "source": "wordnet",
        "meanings": meanings,
    }


def known_words_sample(n: int) -> list[str]:
    """Return up to *n* distinct lemma names from WordNet, alphabetical."""
    if not is_available():
        return []
    try:
        from nltk.corpus import wordnet as wn  # type: ignore[import-not-found]
    except ImportError:
        return []
    if n <= 0:
        return []
    out: list[str] = []
    seen: set[str] = set()
    # all_lemma_names() is a generator; break early when we hit n.
    for name in wn.all_lemma_names():
        norm = name.replace("_", " ").lower()
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
        if len(out) >= n:
            break
    out.sort()
    return out


def known_words_count() -> int:
    """Approximate distinct lemma count (expensive — only for stats UI)."""
    if not is_available():
        return 0
    try:
        from nltk.corpus import wordnet as wn  # type: ignore[import-not-found]
    except ImportError:
        return 0
    return sum(1 for _ in wn.all_lemma_names())
