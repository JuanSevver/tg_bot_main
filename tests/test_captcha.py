"""Tests for captcha generation and validation logic."""
from __future__ import annotations

import pytest

from bot.handlers.user.start import _generate_captcha


class TestCaptchaGeneration:
    def test_returns_expression_and_answer(self):
        expr, answer = _generate_captcha()
        assert isinstance(expr, str)
        assert isinstance(answer, int)

    def test_expression_format(self):
        expr, _ = _generate_captcha()
        # Format: "A + B"
        assert "+" in expr
        parts = expr.split("+")
        assert len(parts) == 2
        assert parts[0].strip().isdigit()
        assert parts[1].strip().isdigit()

    def test_answer_matches_expression(self):
        for _ in range(20):
            expr, answer = _generate_captcha()
            a, b = [int(x.strip()) for x in expr.split("+")]
            assert a + b == answer

    def test_operands_within_range(self):
        for _ in range(50):
            expr, _ = _generate_captcha()
            a, b = [int(x.strip()) for x in expr.split("+")]
            assert 1 <= a <= 10
            assert 1 <= b <= 10

    def test_each_call_produces_different_result(self):
        results = set()
        for _ in range(30):
            expr, answer = _generate_captcha()
            results.add((expr, answer))
        assert len(results) > 5  # very unlikely to get same result 30 times


class TestCaptchaValidation:
    """Simulate the validation logic from process_captcha handler."""

    def _validate(self, user_input: str, correct: int) -> bool:
        try:
            return int(user_input.strip()) == correct
        except (ValueError, AttributeError):
            return False

    def test_correct_answer_passes(self):
        _, answer = _generate_captcha()
        assert self._validate(str(answer), answer) is True

    def test_wrong_answer_fails(self):
        _, answer = _generate_captcha()
        assert self._validate(str(answer + 1), answer) is False

    def test_non_numeric_input_fails(self):
        assert self._validate("abc", 5) is False

    def test_empty_input_fails(self):
        assert self._validate("", 5) is False

    def test_float_input_fails(self):
        assert self._validate("3.0", 3) is False

    def test_whitespace_stripped(self):
        assert self._validate("  7  ", 7) is True
