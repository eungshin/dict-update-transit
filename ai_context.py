"""
ai_context.py — AI-powered contextual meaning disambiguation.

Uses NVIDIA API (OpenAI-compatible endpoint) to pick the most relevant
definition from a list of meanings based on surrounding sentence context.
Also provides direct AI explanation for phrases/idioms not found in dictionary.

Public API:
    pick_definition(word, sentence, meanings) -> int | None
    explain_phrase(phrase, sentence) -> str | None
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict

from openai import OpenAI

logger = logging.getLogger(__name__)

# NVIDIA API endpoint and default models
_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
_DEFAULT_MODEL = "nvidia/llama-3.3-nemotron-super-49b-v1"
# Smaller model for disambiguation — only needs a single integer back
_DEFAULT_PICK_MODEL = "meta/llama-3.1-8b-instruct"

# Per-call timeouts (seconds) — prevent worker thread hang on slow API
_PICK_TIMEOUT = 4.0
_EXPLAIN_TIMEOUT = 8.0

# Response cache size (per function)
_CACHE_MAX = 128

# Client singleton cache: (api_key, id(OpenAI symbol)) -> OpenAI instance.
# id(OpenAI) included so unittest.mock.patch("ai_context.OpenAI", ...) invalidates.
_client_cache: dict = {}

# Response caches — bounded LRU. Only successful results cached.
_pick_cache: "OrderedDict[tuple, int]" = OrderedDict()
_explain_cache: "OrderedDict[tuple, str]" = OrderedDict()


def _lru_get(cache: OrderedDict, key):
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    return None


def _lru_put(cache: OrderedDict, key, value) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _CACHE_MAX:
        cache.popitem(last=False)

_PROMPT_TEMPLATE = """\
Word: {word}
Context: "{sentence}"
Meanings:
{meanings_list}
Reply with only the number of the most relevant meaning (0-indexed). No explanation."""

# System prompt — forces plain text output, no markdown
_SYSTEM_PROMPT = """\
You are a dictionary assistant. Output plain text only — no exceptions.
ABSOLUTE RULES:
- No markdown whatsoever: no #, ##, **, *, -, bullet points, backticks, or horizontal rules
- No introductory phrases: do not start with "Sure", "Of course", "Here is", "아래는", "다음은" or similar
- No closing remarks, no meta-commentary, no translation notes in parentheses showing other languages
- No mixing multiple languages in a single sentence unless the style explicitly requires it
- Use only numbered lists (1. 2. 3.) when listing multiple meanings — no other list format
- Output only the core explanation. Nothing before it, nothing after it."""


def _strip_markdown(text: str) -> str:
    """Remove residual markdown and noise from AI output."""
    import re
    # Remove ### ## # headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove ** bold **, * italic *, __ underline __
    text = re.sub(r'[*_]{1,3}(.+?)[*_]{1,3}', r'\1', text)
    # Remove leading bullet symbols (*, -, •, ·, ▪)
    text = re.sub(r'^\s*[\*\-•·▪]\s+', '', text, flags=re.MULTILINE)
    # Remove intro lines containing common filler phrases
    text = re.sub(
        r'^[^\n]*(아래는|다음은|here is|here are|sure[,!]?|of course[,!]?|certainly[,!]?)[^\n]*\n',
        '', text, flags=re.IGNORECASE | re.MULTILINE
    )
    # Remove English translation in parentheses after Korean (e.g. "성공을 빌다 (to wish good luck)")
    # Keep only first part
    text = re.sub(r'\s*\(to [^)]+\)', '', text)
    text = re.sub(r'\s*\([a-zA-Z][^)]{2,60}\)', '', text)
    # Remove Chinese/Japanese characters that leaked in
    text = re.sub(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]+', '', text)
    # Collapse 3+ blank lines → 1
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Remove trailing whitespace per line
    text = '\n'.join(line.rstrip() for line in text.splitlines())
    return text.strip()


# ---------------------------------------------------------------------------
# Explain prompt templates — keyed by (language, style)
# ---------------------------------------------------------------------------



_EXPLAIN_TEMPLATES: dict[str, str] = {

    # ── English / Concise
    "en_concise": """\
Word/phrase: "{phrase}"{context_part}
Give part of speech, core meaning, and one usage example. Plain text, 2-3 sentences.""",

    # ── English / Detailed
    "en_detailed": """\
Word/phrase: "{phrase}"{context_part}
1. List all possible meanings with part of speech (numbered).
2. State which meaning fits this context and why.
3. One natural usage example for that meaning.
Plain text only. No extra commentary.""",

    # ── 한국어 / 간결
    "ko_concise": """\
단어/표현: "{phrase}"{context_part}
품사, 핵심 의미, 예문을 2-3문장으로 한국어로 설명해. 군더더기 없이 핵심만.""",

    # ── 한국어 / 상세
    "ko_detailed": """\
단어/표현: "{phrase}"{context_part}
1. 가능한 모든 의미를 품사와 함께 번호로 나열해.
2. 이 문맥에서 어떤 의미로 쓰였는지와 이유를 설명해.
3. 해당 의미의 자연스러운 예문 하나.
한국어로 작성. 군더더기 설명 없이.""",

    # ── 한영 혼용 / 간결
    "mixed_concise": """\
Word/phrase: "{phrase}"{context_part}
Explain in Korean. Keep the word/phrase and examples in English. Part of speech + meaning + one example. 2-3 sentences.""",

    # ── 한영 혼용 / 상세
    "mixed_detailed": """\
단어/표현: "{phrase}"{context_part}
1. 가능한 모든 의미를 품사와 함께 번호로 나열 (단어·예문은 영어 유지, 설명은 한국어).
2. 이 문맥에서의 의미와 이유.
3. 자연스러운 영어 예문 하나.
군더더기 없이.""",
}


def _build_explain_prompt(
    phrase: str,
    sentence: str | None,
    language: str,
    style: str,
    custom_suffix: str,
) -> str:
    """Build the explain prompt from config settings."""
    key = f"{language}_{style}"
    template = _EXPLAIN_TEMPLATES.get(key, _EXPLAIN_TEMPLATES["en_detailed"])

    context_part = ""
    if sentence and sentence.strip() and sentence.strip() != phrase.strip():
        if language == "en":
            context_part = f' in the context: "{sentence}"'
        else:
            context_part = f' (문맥: "{sentence}")'

    prompt = template.format(phrase=phrase, context_part=context_part)

    if custom_suffix and custom_suffix.strip():
        prompt += f"\n\n추가 지시: {custom_suffix.strip()}"

    return prompt


def _make_client(api_key: str) -> OpenAI:
    cache_key = (api_key, id(OpenAI))
    client = _client_cache.get(cache_key)
    if client is None:
        client = OpenAI(api_key=api_key, base_url=_NVIDIA_BASE_URL)
        _client_cache[cache_key] = client
    return client


def _get_client() -> tuple[OpenAI, str] | None:
    """Return (client, model) for explain_phrase or None if API key absent."""
    api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not api_key:
        return None
    model = os.environ.get("NVIDIA_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    return _make_client(api_key), model


def _get_pick_client() -> tuple[OpenAI, str] | None:
    """Return (client, model) for pick_definition or None if API key absent.

    Uses NVIDIA_MODEL_PICK env override (smaller/faster model recommended)
    falling back to NVIDIA_MODEL, then to a small default.
    """
    api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not api_key:
        return None
    model = (
        os.environ.get("NVIDIA_MODEL_PICK", "").strip()
        or os.environ.get("NVIDIA_MODEL", "").strip()
        or _DEFAULT_PICK_MODEL
    )
    return _make_client(api_key), model


def explain_phrase(
    phrase: str,
    sentence: str | None = None,
    language: str = "en",
    style: str = "detailed",
    custom_suffix: str = "",
) -> str | None:
    """Ask AI to explain a word or phrase directly.

    Used as fallback when the dictionary API returns no result.

    Parameters
    ----------
    phrase:
        The word, idiom, or phrase to explain.
    sentence:
        Optional surrounding sentence for context.
    language:
        Response language: 'en' | 'ko' | 'mixed'
    style:
        Explanation style: 'concise' | 'detailed'
    custom_suffix:
        Additional user-defined instruction appended to the prompt.

    Returns
    -------
    str | None
        AI explanation text, or None if API key absent or call fails.
    """
    result = _get_client()
    if result is None:
        logger.debug("NVIDIA_API_KEY not set — cannot explain phrase")
        return None

    client, model = result

    sentence_key = (sentence or "")[:64]
    cache_key = (phrase, sentence_key, language, style, custom_suffix, model)
    cached = _lru_get(_explain_cache, cache_key)
    if cached is not None:
        logger.debug("explain_phrase cache hit for %r", phrase)
        return cached

    prompt = _build_explain_prompt(phrase, sentence, language, style, custom_suffix)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
            temperature=0.3,
            timeout=_EXPLAIN_TIMEOUT,
        )
        text = (response.choices[0].message.content or "").strip()
        if text:
            text = _strip_markdown(text)
            logger.info("AI explained phrase %r (%s/%s, %d chars)", phrase, language, style, len(text))
            _lru_put(_explain_cache, cache_key, text)
            return text
        return None
    except Exception as exc:
        logger.warning("AI explain_phrase failed (%s)", exc)
        return None


def pick_definition(
    word: str,
    sentence: str,
    meanings: list[dict],
) -> int | None:
    """Return the 0-based index of the most contextually relevant meaning.

    Parameters
    ----------
    word:
        The word being looked up.
    sentence:
        Surrounding sentence context (may equal *word* if no context available).
    meanings:
        List of meaning dicts as returned by lookup_word() —
        each has 'part_of_speech' and 'definitions' keys.

    Returns
    -------
    int | None
        0-based index into *meanings*, or None if:
        - NVIDIA_API_KEY is absent/empty
        - Only one meaning exists (return 0 directly, no API call)
        - API call fails or returns unparseable response
    """
    if not meanings:
        return None

    if len(meanings) == 1:
        return 0

    api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not api_key:
        logger.debug("NVIDIA_API_KEY not set — skipping AI disambiguation")
        return None

    try:
        client_info = _get_pick_client()
        if client_info is None:
            return None
        client, model = client_info

        # Cache key includes meaning count so different lookups don't collide
        sentence_key = (sentence or "")[:64]
        cache_key = (word, sentence_key, len(meanings), model)
        cached = _lru_get(_pick_cache, cache_key)
        if cached is not None:
            logger.debug("pick_definition cache hit for %r", word)
            return cached

        # Build numbered meanings list
        lines: list[str] = []
        for i, m in enumerate(meanings):
            pos = m.get("part_of_speech", "?")
            defs = m.get("definitions", [])
            first_def = defs[0].get("definition", "") if defs else ""
            lines.append(f"{i}. [{pos}] {first_def}")
        meanings_list = "\n".join(lines)

        prompt = _PROMPT_TEMPLATE.format(
            word=word,
            sentence=sentence,
            meanings_list=meanings_list,
        )

        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10,
            temperature=0.0,
            timeout=_PICK_TIMEOUT,
        )
        raw = response.choices[0].message.content or ""
        raw = raw.strip()

        # Parse: accept bare integer or integer at start of string
        for token in raw.split():
            try:
                idx = int(token)
                if 0 <= idx < len(meanings):
                    logger.info(
                        "AI chose meaning %d for '%s' (context: %.40s)", idx, word, sentence
                    )
                    _lru_put(_pick_cache, cache_key, idx)
                    return idx
                else:
                    logger.warning(
                        "AI returned out-of-range index %d (max %d) — falling back",
                        idx,
                        len(meanings) - 1,
                    )
                    return None
            except ValueError:
                continue

        logger.warning("AI response not parseable as integer: %r — falling back", raw[:80])
        return None

    except Exception as exc:
        logger.warning("AI disambiguation failed (%s) — showing all meanings", exc)
        return None
