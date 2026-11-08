"""A model's "false" must not read as true (review of 2026-11-08, contracts.py)."""

from __future__ import annotations

import pytest

from supervisor_harness.contracts import _bool, parse_checkpoint, parse_drift


@pytest.mark.parametrize("word", ["false", "False", " no ", "0", False, 0])
def test_false_spellings_read_as_false(word: object) -> None:
    assert _bool(word, True) is False


@pytest.mark.parametrize("word", ["true", "TRUE", "yes", "1", True, 1])
def test_true_spellings_read_as_true(word: object) -> None:
    assert _bool(word, False) is True


@pytest.mark.parametrize("word", [None, "", "maybe", [], {}])
def test_unrecognised_values_take_the_default(word: object) -> None:
    assert _bool(word, True) is True
    assert _bool(word, False) is False


def test_a_checkpoint_that_says_false_has_not_passed() -> None:
    assert parse_checkpoint({"passed": "false"}, "r", 1).passed is False


def test_a_drift_judge_that_says_false_means_off_task() -> None:
    assert parse_drift({"on_task": "false"}, "model").on_task is False
