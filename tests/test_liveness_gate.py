"""Tests for the live suite's liveness gate and its per-host waiver.

The gate lives in ``tests/conftest.py``.  These tests drive its hooks directly
with stub items and pre-seeded probe results, so they run in the mocked suite
and never open a socket.
"""

from types import SimpleNamespace

import pytest

from tests import conftest

_KEY = "ultimacodex"
_OTHER = "httpbin"


@pytest.fixture(autouse=True)
def _isolated_gate_state(monkeypatch):
    """Give each test empty probe and skip ledgers; the real ones are session-wide."""
    monkeypatch.setattr(conftest, "_probe_results", {})
    monkeypatch.setattr(conftest, "_waived_skips", {})
    monkeypatch.delenv(conftest.LIVE_ALLOW_DOWN_ENV, raising=False)


def _config(allowed: frozenset[str]):
    config = SimpleNamespace(stash=pytest.Stash())
    config.stash[conftest._ALLOW_DOWN_KEY] = allowed
    return config


def _item(*keys: str, allowed: frozenset[str] = frozenset()):
    marker = SimpleNamespace(args=keys)

    def iter_markers(name):
        return [marker] if name == "requires_live" else []

    return SimpleNamespace(config=_config(allowed), iter_markers=iter_markers)


class _Reporter:
    def __init__(self, allowed: frozenset[str]):
        self.config = _config(allowed)
        self.sections: list[str] = []
        self.lines: list[str] = []

    def section(self, title):
        self.sections.append(title)

    def write_line(self, line):
        self.lines.append(line)


class TestAllowedDownParsing:
    def test_unset_means_no_waiver(self):
        assert conftest._allowed_down() == frozenset()

    def test_comma_list_with_whitespace_and_empties(self, monkeypatch):
        monkeypatch.setenv(conftest.LIVE_ALLOW_DOWN_ENV, f" {_KEY} ,, {_OTHER} ,")
        assert conftest._allowed_down() == {_KEY, _OTHER}

    def test_unknown_key_is_a_usage_error_naming_it(self, monkeypatch):
        monkeypatch.setenv(conftest.LIVE_ALLOW_DOWN_ENV, f"{_KEY},ultimacodx")
        with pytest.raises(pytest.UsageError, match="ultimacodx"):
            conftest._allowed_down()


class TestSetupVerdict:
    def test_down_host_without_waiver_fails_and_names_the_variable(self):
        conftest._probe_results[_KEY] = "HTTP 409"
        with pytest.raises(pytest.fail.Exception) as exc:
            conftest.pytest_runtest_setup(_item(_KEY))
        assert "HTTP 409" in str(exc.value)
        assert f"{conftest.LIVE_ALLOW_DOWN_ENV}={_KEY}" in str(exc.value)

    def test_down_host_with_waiver_skips_and_is_counted(self):
        conftest._probe_results[_KEY] = "HTTP 409"
        with pytest.raises(pytest.skip.Exception, match="waived"):
            conftest.pytest_runtest_setup(_item(_KEY, allowed=frozenset({_KEY})))
        assert conftest._waived_skips == {_KEY: 1}

    def test_waiver_for_one_host_does_not_cover_another(self):
        conftest._probe_results[_KEY] = None
        conftest._probe_results[_OTHER] = "ConnectError"
        with pytest.raises(pytest.fail.Exception, match="httpbin.org"):
            conftest.pytest_runtest_setup(
                _item(_KEY, _OTHER, allowed=frozenset({_KEY}))
            )
        assert conftest._waived_skips == {}

    def test_serving_host_runs_even_when_waived(self):
        conftest._probe_results[_KEY] = None
        conftest.pytest_runtest_setup(_item(_KEY, allowed=frozenset({_KEY})))
        assert conftest._waived_skips == {}


class TestTerminalSummary:
    def test_silent_without_waiver(self):
        reporter = _Reporter(frozenset())
        conftest.pytest_terminal_summary(reporter)
        assert reporter.sections == []
        assert reporter.lines == []

    def test_reports_shed_coverage_and_unused_waivers(self):
        conftest._probe_results.update({_KEY: "HTTP 409", _OTHER: None})
        conftest._waived_skips[_KEY] = 6
        reporter = _Reporter(frozenset({_KEY, _OTHER, "github-api"}))
        conftest.pytest_terminal_summary(reporter)
        assert reporter.sections == ["liveness waiver"]
        assert reporter.lines == [
            "github-api: not probed in this run, waiver unused",
            "httpbin: serving, waiver unused",
            (
                f"ultimacodex: DOWN, 6 test(s) skipped under "
                f"{conftest.LIVE_ALLOW_DOWN_ENV}; this run did not verify them"
            ),
        ]
