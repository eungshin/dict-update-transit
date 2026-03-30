"""
tests/test_dictionary.py — Tests for dictionary.py boundary contract,
error handling, and data normalization.

Test categories:
    1. lookup_word — live API integration tests (marked with @pytest.mark.integration)
    2. lookup_word — unit tests with mocked HTTP responses
    3. format_definition — unit tests (no network required)
    4. Boundary contract tests — exact key/structure assertions
    5. Negative tests — malformed inputs, error paths, boundary conditions
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from dictionary import format_definition, lookup_word


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

MINIMAL_RESULT = {
    "word": "test",
    "phonetic": "/tɛst/",
    "meanings": [
        {
            "part_of_speech": "noun",
            "definitions": [
                {
                    "definition": "a procedure to discover quality or performance",
                    "example": "he passed the test",
                }
            ],
        }
    ],
}

NO_PHONETIC_RESULT = {
    "word": "test",
    "phonetic": None,
    "meanings": [
        {
            "part_of_speech": "noun",
            "definitions": [
                {
                    "definition": "a procedure to discover quality or performance",
                    "example": None,
                }
            ],
        }
    ],
}

MULTI_MEANING_RESULT = {
    "word": "run",
    "phonetic": "/rʌn/",
    "meanings": [
        {
            "part_of_speech": "verb",
            "definitions": [
                {"definition": "move at a speed faster than a walk", "example": "she ran to catch the bus"},
                {"definition": "manage or be in charge of", "example": None},
            ],
        },
        {
            "part_of_speech": "noun",
            "definitions": [
                {"definition": "an act or spell of running", "example": "a run in the park"},
            ],
        },
    ],
}

API_HELLO_RESPONSE = [
    {
        "word": "hello",
        "phonetic": "həˈləʊ",
        "phonetics": [{"text": "həˈləʊ", "audio": ""}],
        "meanings": [
            {
                "partOfSpeech": "exclamation",
                "definitions": [
                    {
                        "definition": "used as a greeting",
                        "example": "hello there, Katie!",
                        "synonyms": [],
                        "antonyms": [],
                    }
                ],
            }
        ],
    }
]

API_NOT_FOUND_RESPONSE = {
    "title": "No Definitions Found",
    "message": "Sorry, we could not find definitions for the word you were looking for.",
    "resolution": "Please check your spelling. Or try searching for a similar word.",
}


# ---------------------------------------------------------------------------
# Helpers to build mock responses
# ---------------------------------------------------------------------------

def _mock_response(status_code: int, json_data) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data
    if status_code >= 400:
        mock.raise_for_status.side_effect = requests.HTTPError(response=mock)
    else:
        mock.raise_for_status.return_value = None
    return mock


# ---------------------------------------------------------------------------
# lookup_word — unit tests with mocked HTTP
# ---------------------------------------------------------------------------

class TestLookupWordUnit:
    """Unit tests for lookup_word using mocked requests.get."""

    def test_known_word_returns_dict(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        assert result is not None
        assert isinstance(result, dict)

    def test_known_word_has_correct_top_level_keys(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        assert set(result.keys()) == {"word", "phonetic", "meanings"}

    def test_known_word_word_field(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        assert result["word"] == "hello"

    def test_known_word_phonetic_field(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        assert result["phonetic"] == "həˈləʊ"

    def test_known_word_meanings_is_list(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        assert isinstance(result["meanings"], list)
        assert len(result["meanings"]) >= 1

    def test_meaning_has_correct_keys(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        meaning = result["meanings"][0]
        assert set(meaning.keys()) == {"part_of_speech", "definitions"}

    def test_definition_has_correct_keys(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        defn = result["meanings"][0]["definitions"][0]
        assert set(defn.keys()) == {"definition", "example"}

    def test_definition_example_is_str_or_none(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        example = result["meanings"][0]["definitions"][0]["example"]
        assert example is None or isinstance(example, str)

    def test_404_returns_none(self):
        mock_resp = _mock_response(404, API_NOT_FOUND_RESPONSE)
        mock_resp.raise_for_status.side_effect = None  # 404 should NOT raise
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("xyzzynotaword")
        assert result is None

    def test_missing_phonetic_returns_none(self):
        """API response without top-level phonetic normalizes to None."""
        api_data = [
            {
                "word": "nophonetic",
                "phonetics": [],  # no text entries
                "meanings": [
                    {
                        "partOfSpeech": "noun",
                        "definitions": [{"definition": "something", "synonyms": [], "antonyms": []}],
                    }
                ],
            }
        ]
        mock_resp = _mock_response(200, api_data)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("nophonetic")
        assert result["phonetic"] is None

    def test_missing_example_returns_none(self):
        """API definition without 'example' key normalizes to None."""
        api_data = [
            {
                "word": "noexample",
                "phonetic": "/nəʊ/",
                "meanings": [
                    {
                        "partOfSpeech": "noun",
                        "definitions": [{"definition": "something without example", "synonyms": [], "antonyms": []}],
                    }
                ],
            }
        ]
        mock_resp = _mock_response(200, api_data)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("noexample")
        assert result["meanings"][0]["definitions"][0]["example"] is None

    def test_phonetics_fallback_when_no_top_level_phonetic(self):
        """Falls back to phonetics[].text when top-level phonetic absent."""
        api_data = [
            {
                "word": "fallback",
                "phonetics": [{"text": "/fɔːlbæk/", "audio": ""}],
                "meanings": [
                    {
                        "partOfSpeech": "noun",
                        "definitions": [{"definition": "a contingency", "synonyms": [], "antonyms": []}],
                    }
                ],
            }
        ]
        mock_resp = _mock_response(200, api_data)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("fallback")
        assert result["phonetic"] == "/fɔːlbæk/"

    def test_network_error_raises_request_exception(self):
        with patch("dictionary.requests.get", side_effect=requests.ConnectionError("no network")):
            with pytest.raises(requests.RequestException):
                lookup_word("hello")

    def test_server_error_raises_request_exception(self):
        mock_resp = _mock_response(500, {})
        mock_resp.raise_for_status.side_effect = requests.HTTPError("server error")
        with patch("dictionary.requests.get", return_value=mock_resp):
            with pytest.raises(requests.RequestException):
                lookup_word("hello")

    def test_uses_10_second_timeout(self):
        """requests.get is called with timeout=10."""
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp) as mock_get:
            lookup_word("hello")
        _args, kwargs = mock_get.call_args
        assert kwargs.get("timeout") == 10


# ---------------------------------------------------------------------------
# lookup_word — negative / malformed input tests
# ---------------------------------------------------------------------------

class TestLookupWordNegative:
    """Negative tests: malformed inputs, empty strings, whitespace."""

    def test_empty_string_returns_none(self):
        result = lookup_word("")
        assert result is None

    def test_whitespace_only_returns_none(self):
        result = lookup_word("   ")
        assert result is None

    def test_tab_only_returns_none(self):
        result = lookup_word("\t")
        assert result is None

    def test_newline_only_returns_none(self):
        result = lookup_word("\n")
        assert result is None


# ---------------------------------------------------------------------------
# format_definition — unit tests
# ---------------------------------------------------------------------------

class TestFormatDefinition:
    """Unit tests for format_definition with manually constructed dicts."""

    def test_returns_str(self):
        output = format_definition(MINIMAL_RESULT)
        assert isinstance(output, str)

    def test_output_contains_word(self):
        output = format_definition(MINIMAL_RESULT)
        assert "test" in output

    def test_output_contains_part_of_speech(self):
        output = format_definition(MINIMAL_RESULT)
        assert "noun" in output

    def test_output_contains_definition_text(self):
        output = format_definition(MINIMAL_RESULT)
        assert "procedure" in output

    def test_output_contains_phonetic(self):
        output = format_definition(MINIMAL_RESULT)
        assert "/tɛst/" in output

    def test_output_contains_example(self):
        output = format_definition(MINIMAL_RESULT)
        assert "passed the test" in output

    def test_none_phonetic_does_not_crash(self):
        """format_definition handles phonetic=None without crashing."""
        output = format_definition(NO_PHONETIC_RESULT)
        assert isinstance(output, str)
        assert "test" in output

    def test_none_phonetic_not_in_output(self):
        """No 'None' literal appears in output when phonetic is None."""
        output = format_definition(NO_PHONETIC_RESULT)
        assert "None" not in output

    def test_none_example_does_not_crash(self):
        """format_definition handles example=None without crashing."""
        output = format_definition(NO_PHONETIC_RESULT)
        assert isinstance(output, str)

    def test_none_example_not_in_output(self):
        """No 'None' literal or 'Example: None' in output when example is None."""
        output = format_definition(NO_PHONETIC_RESULT)
        assert "Example: None" not in output
        assert "None" not in output

    def test_multiple_meanings_all_appear(self):
        output = format_definition(MULTI_MEANING_RESULT)
        assert "verb" in output
        assert "noun" in output

    def test_multiple_definitions_numbered(self):
        output = format_definition(MULTI_MEANING_RESULT)
        assert "1." in output
        assert "2." in output

    def test_format_is_multiline(self):
        output = format_definition(MINIMAL_RESULT)
        assert "\n" in output

    def test_not_a_rich_renderable(self):
        """format_definition must return plain str, not a rich object."""
        from dictionary import format_definition as fd
        output = fd(MINIMAL_RESULT)
        # Rich renderables have __rich_console__ or similar; plain str does not
        assert type(output) is str
        assert not hasattr(output, "__rich_console__")


# ---------------------------------------------------------------------------
# Boundary contract tests
# ---------------------------------------------------------------------------

class TestBoundaryContract:
    """Exact key/structure assertions for the S02/S03 boundary contract."""

    def test_exact_top_level_keys(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        assert set(result.keys()) == {"word", "phonetic", "meanings"}, (
            "lookup_word must return exactly keys: word, phonetic, meanings"
        )

    def test_meanings_items_have_exact_keys(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        for meaning in result["meanings"]:
            assert set(meaning.keys()) == {"part_of_speech", "definitions"}, (
                "Each meaning must have exactly keys: part_of_speech, definitions"
            )

    def test_definitions_items_have_exact_keys(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        for meaning in result["meanings"]:
            for defn in meaning["definitions"]:
                assert set(defn.keys()) == {"definition", "example"}, (
                    "Each definition must have exactly keys: definition, example"
                )

    def test_word_is_str(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        assert isinstance(result["word"], str)

    def test_phonetic_is_str_or_none(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        assert result["phonetic"] is None or isinstance(result["phonetic"], str)

    def test_meanings_is_list(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        assert isinstance(result["meanings"], list)

    def test_definitions_is_list(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        for meaning in result["meanings"]:
            assert isinstance(meaning["definitions"], list)

    def test_definition_text_is_str(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        for meaning in result["meanings"]:
            for defn in meaning["definitions"]:
                assert isinstance(defn["definition"], str)

    def test_example_is_str_or_none(self):
        mock_resp = _mock_response(200, API_HELLO_RESPONSE)
        with patch("dictionary.requests.get", return_value=mock_resp):
            result = lookup_word("hello")
        for meaning in result["meanings"]:
            for defn in meaning["definitions"]:
                assert defn["example"] is None or isinstance(defn["example"], str)


# ---------------------------------------------------------------------------
# Integration tests (live network — skipped in CI unless explicitly enabled)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestLookupWordIntegration:
    """Live API tests — require network access. Run with: pytest -m integration"""

    def test_hello_returns_dict_with_correct_keys(self):
        result = lookup_word("hello")
        assert result is not None
        assert set(result.keys()) == {"word", "phonetic", "meanings"}

    def test_hello_word_field(self):
        result = lookup_word("hello")
        assert result["word"] == "hello"

    def test_hello_has_meanings(self):
        result = lookup_word("hello")
        assert len(result["meanings"]) >= 1

    def test_unknown_word_returns_none(self):
        result = lookup_word("xyzzynotaword")
        assert result is None
