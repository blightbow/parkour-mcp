"""Tests for parkour_mcp.scorecard module and its GitHub tool integration."""

import sys

import httpx
import pytest
import respx

import parkour_mcp.github  # noqa: F401  — register submodule in sys.modules
from parkour_mcp import scorecard
from parkour_mcp.github import github

# Use sys.modules to reach the submodule.  parkour_mcp/__init__.py exposes
# the ``github`` callable at the package top level, so a plain
# ``parkour_mcp.github`` attribute access resolves to the function, not
# the module.  The same workaround lives in conftest.py.
_gh_mod = sys.modules["parkour_mcp.github"]


# Sample response matching the shape returned by api.securityscorecards.dev.
# Trimmed to the fields fetch_overall actually reads.
SCORECARD_RESPONSE = {
    "date": "2026-04-13",
    "repo": {"name": "github.com/psf/requests", "commit": "abc123"},
    "scorecard": {"version": "v5.0.0", "commit": "def456"},
    "score": 7.4,
    "checks": [],
}

SCORECARD_RESPONSE_INT = {
    **SCORECARD_RESPONSE,
    "score": 8,  # integer is valid JSON — make sure we cast
}


class TestFetchOverall:
    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_float_on_success(self):
        respx.get(
            "https://api.securityscorecards.dev/projects/github.com/psf/requests"
        ).mock(return_value=httpx.Response(200, json=SCORECARD_RESPONSE))

        score = await scorecard.fetch_overall("psf", "requests")
        assert score == pytest.approx(7.4)

    @pytest.mark.asyncio
    @respx.mock
    async def test_casts_integer_score(self):
        respx.get(
            "https://api.securityscorecards.dev/projects/github.com/psf/requests"
        ).mock(return_value=httpx.Response(200, json=SCORECARD_RESPONSE_INT))

        score = await scorecard.fetch_overall("psf", "requests")
        assert isinstance(score, float)
        assert score == pytest.approx(8.0)

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_on_404(self):
        respx.get(
            "https://api.securityscorecards.dev/projects/github.com/obscure/repo"
        ).mock(return_value=httpx.Response(404))

        assert await scorecard.fetch_overall("obscure", "repo") is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_on_500(self):
        respx.get(
            "https://api.securityscorecards.dev/projects/github.com/foo/bar"
        ).mock(return_value=httpx.Response(500))

        assert await scorecard.fetch_overall("foo", "bar") is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_on_malformed_json(self):
        respx.get(
            "https://api.securityscorecards.dev/projects/github.com/foo/bar"
        ).mock(return_value=httpx.Response(200, text="not json"))

        assert await scorecard.fetch_overall("foo", "bar") is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_when_score_field_missing(self):
        respx.get(
            "https://api.securityscorecards.dev/projects/github.com/foo/bar"
        ).mock(return_value=httpx.Response(200, json={"date": "2026-01-01"}))

        assert await scorecard.fetch_overall("foo", "bar") is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_on_network_error(self):
        respx.get(
            "https://api.securityscorecards.dev/projects/github.com/foo/bar"
        ).mock(side_effect=httpx.ConnectError("dns failed"))

        assert await scorecard.fetch_overall("foo", "bar") is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_caches_successful_lookup(self):
        route = respx.get(
            "https://api.securityscorecards.dev/projects/github.com/psf/requests"
        ).mock(return_value=httpx.Response(200, json=SCORECARD_RESPONSE))

        await scorecard.fetch_overall("psf", "requests")
        await scorecard.fetch_overall("psf", "requests")
        # Second call should hit the cache, not the mock again
        assert route.call_count == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_caches_404_as_none(self):
        route = respx.get(
            "https://api.securityscorecards.dev/projects/github.com/obscure/repo"
        ).mock(return_value=httpx.Response(404))

        assert await scorecard.fetch_overall("obscure", "repo") is None
        assert await scorecard.fetch_overall("obscure", "repo") is None
        assert route.call_count == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_does_not_cache_network_error(self):
        route = respx.get(
            "https://api.securityscorecards.dev/projects/github.com/flaky/repo"
        ).mock(side_effect=httpx.ConnectError("transient"))

        await scorecard.fetch_overall("flaky", "repo")
        await scorecard.fetch_overall("flaky", "repo")
        # Transient errors are not cached — both calls hit the network
        assert route.call_count == 2


class TestRepoActionEnrichment:
    """Confirm that _action_repo surfaces openssf_scorecard in frontmatter
    when the scorecard fetch succeeds, and omits it cleanly otherwise.
    """

    @pytest.mark.asyncio
    @respx.mock
    async def test_score_appears_in_frontmatter(self, monkeypatch):
        async def _score(owner, repo):
            assert (owner, repo) == ("org", "tool")
            return 7.4

        monkeypatch.setattr(_gh_mod, "_fetch_scorecard_overall", _score)

        respx.get("https://api.github.com/repos/org/tool").mock(
            return_value=httpx.Response(200, json={
                "full_name": "org/tool",
                "description": "A tool",
                "stargazers_count": 100,
                "forks_count": 5,
                "open_issues_count": 0,
                "language": "Python",
                "license": {"spdx_id": "MIT"},
                "topics": [],
                "default_branch": "main",
            })
        )
        respx.get("https://api.github.com/repos/org/tool/readme").mock(
            return_value=httpx.Response(
                200, text="# README",
                headers={"content-type": "text/plain"},
            )
        )
        respx.get(
            "https://raw.githubusercontent.com/org/tool/main/CITATION.cff"
        ).mock(return_value=httpx.Response(404))
        respx.get(
            "https://api.github.com/repos/org/tool/contents/.github/ISSUE_TEMPLATE"
        ).mock(return_value=httpx.Response(404))

        result = await github("repo", "org/tool")
        assert "openssf_scorecard: 7.4/10" in result
        assert "Packages(action=project, query=github.com/org/tool)" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_integer_score_formatted_without_trailing_zero(
        self, monkeypatch,
    ):
        async def _score(_owner, _repo):
            return 10.0

        monkeypatch.setattr(_gh_mod, "_fetch_scorecard_overall", _score)

        respx.get("https://api.github.com/repos/org/tool").mock(
            return_value=httpx.Response(200, json={
                "full_name": "org/tool",
                "description": "",
                "stargazers_count": 0,
                "forks_count": 0,
                "open_issues_count": 0,
                "language": None,
                "license": None,
                "topics": [],
                "default_branch": "main",
            })
        )
        respx.get("https://api.github.com/repos/org/tool/readme").mock(
            return_value=httpx.Response(404)
        )
        respx.get(
            "https://raw.githubusercontent.com/org/tool/main/CITATION.cff"
        ).mock(return_value=httpx.Response(404))
        respx.get(
            "https://api.github.com/repos/org/tool/contents/.github/ISSUE_TEMPLATE"
        ).mock(return_value=httpx.Response(404))

        result = await github("repo", "org/tool")
        # :g format drops trailing zeros on whole-number floats
        assert "openssf_scorecard: 10/10" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_key_when_no_score(self):
        # Conftest autouse stub already returns None
        respx.get("https://api.github.com/repos/org/tool").mock(
            return_value=httpx.Response(200, json={
                "full_name": "org/tool",
                "description": "",
                "stargazers_count": 0,
                "forks_count": 0,
                "open_issues_count": 0,
                "language": None,
                "license": None,
                "topics": [],
                "default_branch": "main",
            })
        )
        respx.get("https://api.github.com/repos/org/tool/readme").mock(
            return_value=httpx.Response(404)
        )
        respx.get(
            "https://raw.githubusercontent.com/org/tool/main/CITATION.cff"
        ).mock(return_value=httpx.Response(404))
        respx.get(
            "https://api.github.com/repos/org/tool/contents/.github/ISSUE_TEMPLATE"
        ).mock(return_value=httpx.Response(404))

        result = await github("repo", "org/tool")
        assert "openssf_scorecard" not in result
        assert "see_also:" not in result


class TestFileActionEnrichment:
    """Scorecard also surfaces on the blob/file path so the agent can
    weigh trust on the source repo when consuming code.
    """

    @pytest.mark.asyncio
    @respx.mock
    async def test_score_appears_on_file_fetch(self, monkeypatch):
        async def _score(owner, repo):
            assert (owner, repo) == ("o", "r")
            return 6.2

        monkeypatch.setattr(_gh_mod, "_fetch_scorecard_overall", _score)

        respx.get("https://raw.githubusercontent.com/o/r/main/test.py").mock(
            return_value=httpx.Response(200, text="def hello():\n    pass\n")
        )

        result = await github("file", "o/r/test.py", ref="main")
        assert "openssf_scorecard: 6.2/10" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_score_no_key(self):
        # Conftest stub returns None
        respx.get("https://raw.githubusercontent.com/o/r/HEAD/test.py").mock(
            return_value=httpx.Response(200, text="print('hi')\n")
        )

        result = await github("file", "o/r/test.py")
        assert "openssf_scorecard" not in result
