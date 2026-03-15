"""Tests for claude_web_tools.kagi module — balance checking and lockout logic."""

import pytest
from unittest.mock import patch, MagicMock

import claude_web_tools.kagi as kagi_mod
from claude_web_tools.kagi import (
    _extract_balance,
    _check_balance,
    search,
    summarize,
)


# --- _extract_balance ---

class TestExtractBalance:
    def test_extracts_float_balance(self):
        assert _extract_balance({"meta": {"api_balance": 12.34}}) == 12.34

    def test_extracts_int_balance(self):
        assert _extract_balance({"meta": {"api_balance": 5}}) == 5.0

    def test_extracts_string_balance(self):
        assert _extract_balance({"meta": {"api_balance": "3.50"}}) == 3.50

    def test_returns_none_when_missing(self):
        assert _extract_balance({"meta": {}}) is None

    def test_returns_none_when_no_meta(self):
        assert _extract_balance({}) is None

    def test_returns_none_for_invalid_value(self):
        assert _extract_balance({"meta": {"api_balance": "not_a_number"}}) is None


# --- _check_balance and lockout ---

class TestCheckBalance:
    def setup_method(self):
        """Reset lockout state before each test."""
        kagi_mod._summarize_locked = False

    def test_no_warning_when_balance_healthy(self):
        warning = _check_balance({"meta": {"api_balance": 5.00}})
        assert warning is None

    def test_warning_when_balance_low(self):
        warning = _check_balance({"meta": {"api_balance": 0.50}})
        assert warning is not None
        assert "<!-- warning:" in warning
        assert "$0.50" in warning

    def test_low_balance_sets_lockout(self):
        _check_balance({"meta": {"api_balance": 0.25}})
        assert kagi_mod._summarize_locked is True

    def test_healthy_balance_clears_lockout_for_non_summarize(self):
        kagi_mod._summarize_locked = True
        _check_balance({"meta": {"api_balance": 5.00}}, is_summarize=False)
        assert kagi_mod._summarize_locked is False

    def test_healthy_balance_does_not_clear_lockout_for_summarize(self):
        kagi_mod._summarize_locked = True
        _check_balance({"meta": {"api_balance": 5.00}}, is_summarize=True)
        assert kagi_mod._summarize_locked is True

    def test_no_meta_does_not_change_lockout(self):
        kagi_mod._summarize_locked = True
        _check_balance({})
        assert kagi_mod._summarize_locked is True

    def test_threshold_boundary_low(self):
        warning = _check_balance({"meta": {"api_balance": 0.99}})
        assert warning is not None
        assert kagi_mod._summarize_locked is True

    def test_threshold_boundary_at(self):
        warning = _check_balance({"meta": {"api_balance": 1.00}})
        assert warning is None
        assert kagi_mod._summarize_locked is False


# --- Lockout integration ---

class TestSummarizeLockout:
    def setup_method(self):
        kagi_mod._summarize_locked = False

    @pytest.mark.asyncio
    async def test_summarize_blocked_when_locked(self):
        kagi_mod._summarize_locked = True
        result = await summarize(url="https://example.com")
        assert "temporarily disabled" in result
        assert "low API balance" in result

    @pytest.mark.asyncio
    async def test_search_clears_lockout_on_healthy_balance(self):
        kagi_mod._summarize_locked = True

        mock_client = MagicMock()
        mock_client.search.return_value = {
            "meta": {"api_balance": 10.00},
            "data": [],
        }

        with patch.object(kagi_mod, "get_client", return_value=mock_client):
            result = await search("test query")

        assert kagi_mod._summarize_locked is False
        assert "balance low" not in result.lower()

    @pytest.mark.asyncio
    async def test_search_warns_and_locks_on_low_balance(self):
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "meta": {"api_balance": 0.42},
            "data": [{"t": 0, "title": "Result", "url": "https://example.com", "snippet": "A result"}],
        }

        with patch.object(kagi_mod, "get_client", return_value=mock_client):
            result = await search("test query")

        assert kagi_mod._summarize_locked is True
        assert result.startswith("<!-- warning:")
        assert "$0.42" in result
        assert "Result" in result  # actual results still returned

    @pytest.mark.asyncio
    async def test_summarize_warns_on_low_balance(self):
        mock_client = MagicMock()
        mock_client.summarize.return_value = {
            "meta": {"api_balance": 0.10},
            "data": {"output": "Summary text here."},
        }

        with patch.object(kagi_mod, "get_client", return_value=mock_client):
            result = await summarize(url="https://example.com")

        assert result.startswith("<!-- warning:")
        assert "Summary text here." in result
        assert "$0.10" in result
        assert kagi_mod._summarize_locked is True
