"""
tests/test_hotkey_daemon.py — Unit tests for pure functions in hotkey_daemon.py.

All tests are display-free, network-free, and Win32-free.
"""

from __future__ import annotations

import pytest

from hotkey_daemon import (
    CLIPBOARD_WAIT_MS,
    HOTKEY_ID,
    POLL_INTERVAL_MS,
    POPUP_MAX_HEIGHT,
    POPUP_WIDTH,
    clamp_position,
    extract_word,
)


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------


class TestConstants:
    def test_hotkey_id(self):
        assert HOTKEY_ID == 1

    def test_clipboard_wait_ms(self):
        assert CLIPBOARD_WAIT_MS == 100

    def test_poll_interval_ms(self):
        assert POLL_INTERVAL_MS == 20

    def test_popup_width(self):
        assert POPUP_WIDTH == 420

    def test_popup_max_height(self):
        assert POPUP_MAX_HEIGHT == 300


# ---------------------------------------------------------------------------
# extract_word — happy-path tests
# ---------------------------------------------------------------------------


class TestExtractWordHappyPath:
    def test_simple_word(self):
        assert extract_word("hello") == "hello"

    def test_leading_whitespace_stripped(self):
        assert extract_word("  hello") == "hello"

    def test_trailing_whitespace_stripped(self):
        assert extract_word("hello  ") == "hello"

    def test_leading_and_trailing_whitespace(self):
        assert extract_word("   world   ") == "world"

    def test_hyphenated_word(self):
        """'well-known' is a single token — no internal whitespace."""
        assert extract_word("well-known") == "well-known"

    def test_apostrophe_word(self):
        """Contractions like "don't" are single tokens."""
        assert extract_word("don't") == "don't"

    def test_numeric_word(self):
        assert extract_word("42") == "42"

    def test_single_character(self):
        assert extract_word("a") == "a"

    def test_word_with_surrounding_newline_stripped(self):
        """A word surrounded by newlines should be stripped to the word itself."""
        assert extract_word("\nhello\n") == "hello"

    def test_word_with_surrounding_tab_stripped(self):
        assert extract_word("\thello\t") == "hello"


# ---------------------------------------------------------------------------
# extract_word — negative / malformed input tests
# ---------------------------------------------------------------------------


class TestExtractWordNegative:
    def test_empty_string(self):
        assert extract_word("") is None

    def test_whitespace_only_spaces(self):
        assert extract_word("   ") is None

    def test_whitespace_only_tab(self):
        assert extract_word("\t") is None

    def test_whitespace_only_newline(self):
        assert extract_word("\n") is None

    def test_multi_word_two_words(self):
        """Multi-word phrases are now accepted (idiom support)."""
        assert extract_word("hello world") == "hello world"

    def test_multi_word_three_words(self):
        """Idioms like 'kick the bucket' are accepted."""
        assert extract_word("kick the bucket") == "kick the bucket"

    def test_internal_newline(self):
        """Newline between words is normalized to a space."""
        assert extract_word("hello\nworld") == "hello world"

    def test_internal_tab(self):
        """Tab between words is normalized to a space."""
        assert extract_word("hello\tworld") == "hello world"

    def test_leading_space_then_space_in_middle(self):
        """Leading space stripped; internal space kept (multi-word phrase)."""
        assert extract_word("  hello world") == "hello world"

    def test_carriage_return(self):
        """CR normalized to space."""
        assert extract_word("hello\rworld") == "hello world"

    def test_too_long_returns_none(self):
        """Text longer than 60 chars is rejected as a full sentence."""
        assert extract_word("a" * 61) is None

    def test_exactly_60_chars_accepted(self):
        assert extract_word("a" * 60) is not None


# ---------------------------------------------------------------------------
# clamp_position — happy-path tests
# ---------------------------------------------------------------------------


SCREEN_W = 1920
SCREEN_H = 1080
W = 420
H = 300


class TestClampPositionHappyPath:
    def test_center_of_screen_unchanged(self):
        """A popup placed near the center should not be clamped."""
        x, y = clamp_position(700, 400, W, H, SCREEN_W, SCREEN_H)
        assert (x, y) == (700, 400)

    def test_origin_unchanged(self):
        """(0, 0) is already fully in bounds."""
        x, y = clamp_position(0, 0, W, H, SCREEN_W, SCREEN_H)
        assert (x, y) == (0, 0)

    def test_right_edge_clamped(self):
        """Popup starting at x=1900 would overflow right — clamp left."""
        x, y = clamp_position(1900, 400, W, H, SCREEN_W, SCREEN_H)
        assert x == SCREEN_W - W  # 1500
        assert y == 400

    def test_bottom_edge_clamped(self):
        """Popup starting at y=1000 would overflow bottom — clamp up."""
        x, y = clamp_position(400, 1000, W, H, SCREEN_W, SCREEN_H)
        assert x == 400
        assert y == SCREEN_H - H  # 780

    def test_bottom_right_corner_clamped_both(self):
        """The exact scenario from the slice plan: (1900, 1060) → (1500, 780)."""
        x, y = clamp_position(1900, 1060, W, H, SCREEN_W, SCREEN_H)
        assert (x, y) == (1500, 780)

    def test_exact_right_boundary_not_clamped(self):
        """Popup ending exactly at screen_w should not be clamped."""
        x_in = SCREEN_W - W  # 1500
        x, y = clamp_position(x_in, 100, W, H, SCREEN_W, SCREEN_H)
        assert x == x_in

    def test_exact_bottom_boundary_not_clamped(self):
        y_in = SCREEN_H - H  # 780
        x, y = clamp_position(100, y_in, W, H, SCREEN_W, SCREEN_H)
        assert y == y_in


# ---------------------------------------------------------------------------
# clamp_position — negative / boundary tests
# ---------------------------------------------------------------------------


class TestClampPositionNegative:
    def test_negative_x_clamped_to_zero(self):
        x, y = clamp_position(-50, 200, W, H, SCREEN_W, SCREEN_H)
        assert x == 0
        assert y == 200

    def test_negative_y_clamped_to_zero(self):
        x, y = clamp_position(200, -50, W, H, SCREEN_W, SCREEN_H)
        assert x == 200
        assert y == 0

    def test_negative_both_clamped_to_zero(self):
        x, y = clamp_position(-100, -100, W, H, SCREEN_W, SCREEN_H)
        assert (x, y) == (0, 0)

    def test_popup_wider_than_screen_clamped_to_zero(self):
        """If popup is wider than the screen, x clamps to 0."""
        x, y = clamp_position(100, 100, SCREEN_W + 100, H, SCREEN_W, SCREEN_H)
        assert x == 0

    def test_popup_taller_than_screen_clamped_to_zero(self):
        """If popup is taller than the screen, y clamps to 0."""
        x, y = clamp_position(100, 100, W, SCREEN_H + 100, SCREEN_W, SCREEN_H)
        assert y == 0

    def test_popup_larger_than_screen_both_axes(self):
        """Popup bigger than screen on both axes → (0, 0)."""
        x, y = clamp_position(
            100, 100, SCREEN_W + 50, SCREEN_H + 50, SCREEN_W, SCREEN_H
        )
        assert (x, y) == (0, 0)

    def test_overflow_by_one_pixel(self):
        """Off by one: x+w = screen_w+1 → clamp by 1."""
        x, y = clamp_position(SCREEN_W - W + 1, 100, W, H, SCREEN_W, SCREEN_H)
        assert x == SCREEN_W - W

    def test_large_negative_coordinates(self):
        x, y = clamp_position(-9999, -9999, W, H, SCREEN_W, SCREEN_H)
        assert (x, y) == (0, 0)
