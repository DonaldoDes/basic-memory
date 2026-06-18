"""Unit + adversarial tests for real date arithmetic & functions (US-2 / ADR-006 §Gap #4).

Covers BP4-B-01..05 (unit) plus the adversarial degradation surface:

- ``dur("7 days")`` / ``dur(7, "days")`` / ``dur("2 weeks")`` parse into a real
  ``timedelta`` (BP4-B-01).
- ``date(today)`` / ``date(now)`` / ``date("2026-06-10")`` build a real
  ``date`` / ``datetime`` (BP4-B-02).
- ``date(today) - dur(7 days)`` and ``date + dur`` arithmetic + comparisons are
  correct (BP4-B-03).
- ``dateformat(<date>, "yyyy-MM-dd")`` renders a formatted date (BP4-B-04).
- the whitelist (``formula_parser.ALLOWED_FUNCTIONS``) carries ``dur`` and the
  dispatch stays closed — no eval/exec/getattr (BP4-B-05).

Determinism (ADR-006 §Invariants — tests must not assert on the real wall clock):
``today`` / ``now`` are driven by an INJECTED clock (``now=`` parameter of
``evaluate_formula`` / ``safe_evaluate_formula``). No test reads the system date.

Adversarial (zone sensible — ADR-006 §Invariants, checklist adversarial-testing):
- invalid date string → inert block / None, never a crash;
- pathological / huge / negative duration → bounded, inert, no crash;
- unknown dateformat pattern → graceful degradation, no crash;
- date × non-date comparison → None, never a crash.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from basic_memory.bases.errors import BasesUnsupportedError
from basic_memory.bases.formula_parser import ALLOWED_FUNCTIONS, parse_formula
from basic_memory.bases.formula_eval import (
    evaluate_formula,
    safe_evaluate_formula,
)


# A FIXED injected clock — every test is deterministic against this instant.
FIXED_NOW = _dt.datetime(2026, 6, 18, 14, 30, 0, tzinfo=_dt.timezone.utc)
FIXED_TODAY = _dt.date(2026, 6, 18)


def _ev(expr: str, row: dict | None = None, now: _dt.datetime | None = FIXED_NOW):
    """Parse + evaluate an expression with the injected clock (raises typed errors)."""
    return evaluate_formula(parse_formula(expr), row or {}, now=now)


def _safe(expr: str, row: dict | None = None, now: _dt.datetime | None = FIXED_NOW):
    """Parse + safe-evaluate (never raises) — returns (value, error_type)."""
    return safe_evaluate_formula(parse_formula(expr), row or {}, now=now)


# =========================================================================== #
# BP4-B-01 — dur() parses into a real duration
# =========================================================================== #
class TestDur:
    def test_dur_string_days(self):
        assert _ev('dur("7 days")') == _dt.timedelta(days=7)

    def test_dur_string_weeks(self):
        assert _ev('dur("2 weeks")') == _dt.timedelta(weeks=2)

    def test_dur_two_args_form(self):
        assert _ev('dur(7, "days")') == _dt.timedelta(days=7)

    def test_dur_hours_minutes(self):
        assert _ev('dur("3 hours")') == _dt.timedelta(hours=3)
        assert _ev('dur("30 minutes")') == _dt.timedelta(minutes=30)

    def test_dur_singular_unit(self):
        assert _ev('dur("1 day")') == _dt.timedelta(days=1)


# =========================================================================== #
# BP4-B-02 — date() builds a real date / datetime
# =========================================================================== #
class TestDate:
    def test_date_today_is_injected_clock(self):
        assert _ev("date(today)") == FIXED_TODAY

    def test_date_now_is_injected_clock(self):
        assert _ev("date(now)") == FIXED_NOW

    def test_date_from_iso_string(self):
        assert _ev('date("2026-06-10")') == _dt.date(2026, 6, 10)

    def test_date_from_iso_datetime_string(self):
        got = _ev('date("2026-06-10T08:15:00")')
        assert isinstance(got, _dt.datetime)
        assert got.year == 2026 and got.month == 6 and got.day == 10

    def test_today_is_deterministic_under_clock(self):
        other = _dt.datetime(2020, 1, 2, tzinfo=_dt.timezone.utc)
        assert _ev("date(today)", now=other) == _dt.date(2020, 1, 2)


# =========================================================================== #
# BP4-B-03 — date ± dur arithmetic + comparisons
# =========================================================================== #
class TestDateArithmetic:
    def test_today_minus_7_days(self):
        assert _ev('date(today) - dur("7 days")') == _dt.date(2026, 6, 11)

    def test_today_plus_dur(self):
        assert _ev('date(today) + dur("3 days")') == _dt.date(2026, 6, 21)

    def test_date_comparison_greater(self):
        assert _ev('date("2026-06-18") > date("2026-06-11")') is True

    def test_date_comparison_lesser_equal(self):
        assert _ev('date("2026-06-11") <= date(today)') is True

    def test_mtime_string_compared_to_relative_date(self):
        """file.mtime (an ISO string from the provider) > date(today) - dur(7 days).

        The provider exposes file.mtime as an ISO string; the comparison must
        coerce it to a date so a recent note matches and an old one does not.
        """
        recent = {"file.mtime": "2026-06-17T09:00:00"}
        old = {"file.mtime": "2026-01-01T09:00:00"}
        expr = 'file.mtime > date(today) - dur("7 days")'
        assert _ev(expr, recent) is True
        assert _ev(expr, old) is False


# =========================================================================== #
# BP4-B-04 — dateformat() renders a formatted date
# =========================================================================== #
class TestDateFormat:
    def test_dateformat_yyyy_mm_dd(self):
        assert _ev('dateformat(date("2026-06-10"), "yyyy-MM-dd")') == "2026-06-10"

    def test_dateformat_today(self):
        assert _ev('dateformat(date(today), "yyyy-MM-dd")') == "2026-06-18"

    def test_dateformat_with_time_tokens(self):
        got = _ev('dateformat(date("2026-06-10T08:05:00"), "yyyy-MM-dd HH:mm")')
        assert got == "2026-06-10 08:05"

    def test_dateformat_on_iso_string_value(self):
        """dateformat applied to a raw ISO string (file.mtime form) coerces it."""
        row = {"file.mtime": "2026-06-17T09:00:00"}
        assert _ev('dateformat(file.mtime, "yyyy-MM-dd")', row) == "2026-06-17"


# =========================================================================== #
# BP4-B-05 — whitelist extended, dispatch closed
# =========================================================================== #
class TestWhitelistClosed:
    def test_dur_in_allowed_functions(self):
        assert "dur" in ALLOWED_FUNCTIONS

    def test_date_dateformat_in_allowed_functions(self):
        assert "date" in ALLOWED_FUNCTIONS
        assert "dateformat" in ALLOWED_FUNCTIONS

    def test_unknown_date_function_refused(self):
        with pytest.raises(BasesUnsupportedError):
            parse_formula('datetime("2026-06-10")')

    def test_no_eval_exec_getattr_in_date_path(self):
        """Static guard: the date code path introduces no dynamic execution.

        Scans the *parsed AST* of ``formula_eval`` (not raw text — the module's
        docstrings legitimately describe what the sandbox does NOT do, e.g. "no
        ``getattr``/``__import__``/``globals``"). A genuine call to a
        sandbox-escape builtin (``eval``/``exec``/``compile``/``__import__``/
        ``getattr``/``globals``/``vars``) or an ``import os``/``subprocess`` would
        be a real escape and is forbidden. ``FormulaEvaluator.eval`` (the
        AST-walker method) is a method DEFINITION/attribute call, not a builtin
        call, so it never trips this guard.
        """
        import ast
        import inspect

        import basic_memory.bases.formula_eval as mod

        tree = ast.parse(inspect.getsource(mod))
        forbidden_calls = {
            "eval",
            "exec",
            "compile",
            "__import__",
            "getattr",
            "setattr",
            "globals",
            "locals",
            "vars",
        }
        forbidden_imports = {"os", "subprocess", "sys", "importlib"}
        for node in ast.walk(tree):
            # A bare-name builtin call: eval(...), exec(...), getattr(...).
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls, (
                    f"forbidden builtin call {node.func.id}() in formula_eval"
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in forbidden_imports, f"forbidden import {alias.name}"
            if isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root not in forbidden_imports, f"forbidden import from {node.module}"


# =========================================================================== #
# Adversarial — degradation, never a crash (zone sensible)
# =========================================================================== #
class TestDatesAdversarial:
    def test_invalid_date_string_inert(self):
        value, err = _safe('date("not-a-date")')
        assert value is None
        assert err is not None  # inert block, typed error, no crash

    def test_invalid_dur_string_inert(self):
        value, err = _safe('dur("garbage")')
        assert value is None
        assert err is not None

    def test_huge_duration_is_bounded(self):
        """A pathological duration count is refused (limit), not materialised."""
        value, err = _safe('dur(999999999999, "days")')
        assert value is None
        assert err == "limit"

    def test_negative_duration_allowed_but_bounded(self):
        """A small negative duration is a normal value (date going backwards)."""
        assert _ev('date(today) + dur(-2, "days")') == _dt.date(2026, 6, 16)

    def test_huge_negative_duration_is_bounded(self):
        value, err = _safe('dur(-999999999999, "days")')
        assert value is None
        assert err == "limit"

    def test_unknown_dateformat_pattern_degrades(self):
        """An unknown pattern degrades to a safe rendering, never a crash."""
        value, err = _safe('dateformat(date("2026-06-10"), "QQQQ-unknown")')
        # Either inert (typed err) or a best-effort string — but NEVER a crash.
        assert err is not None or isinstance(value, str)

    def test_date_compared_to_non_date_is_none(self):
        """date > a non-date value degrades to None (TypeError swallowed)."""
        value, err = _safe('date("2026-06-10") > "banana"')
        assert value is None
        assert err is not None

    def test_dur_unknown_unit_inert(self):
        value, err = _safe('dur("7 fortnights")')
        assert value is None
        assert err is not None

    def test_dateformat_on_none_does_not_crash(self):
        value, err = _safe('dateformat(missing_field, "yyyy-MM-dd")', {})
        # missing field is None → degrade, never crash.
        assert err is not None or value in (None, "")

    def test_huge_dur_via_weeks_bounded(self):
        value, err = _safe('dur(999999999, "weeks")')
        assert value is None
        assert err == "limit"
