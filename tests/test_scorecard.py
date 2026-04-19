"""Tests for parkour_mcp.scorecard module and its GitHub tool integration."""

import sys

import httpx
import pytest
import respx

import parkour_mcp.github  # noqa: F401 — register submodule in sys.modules
from parkour_mcp import scorecard
from parkour_mcp.github import github

# Use sys.modules to reach the submodule.  parkour_mcp/__init__.py exposes
# the ``github`` callable at the package top level, so a plain
# ``parkour_mcp.github`` attribute access resolves to the function, not
# the module.  The same workaround lives in conftest.py.
_gh_mod = sys.modules["parkour_mcp.github"]


# ``fetch_overall`` hits ``/v3/projects/github.com%2F{owner}%2F{repo}``
# on deps.dev.  respx's URL matcher decodes ``%2F`` to ``/`` so the mock
# declaration uses the literal path form.
_DEPSDEV_PROJECT_URL = (
    "https://api.deps.dev/v3/projects/github.com%2F{owner}%2F{repo}"
)


def _project(
    owner: str, repo: str,
    *, score=7.4, date="2026-04-13T00:00:00Z", include_scorecard=True,
) -> dict:
    """Minimal deps.dev ``Project`` response shaped for scorecard extraction."""
    body: dict = {
        "projectKey": {"id": f"github.com/{owner}/{repo}"},
        "description": "",
    }
    if include_scorecard:
        body["scorecard"] = {
            "date": date,
            "repository": {"name": f"github.com/{owner}/{repo}"},
            "scorecard": {"version": "v5.4.1"},
            "checks": [],
            "overallScore": score,
        }
    return body


class TestFetchOverall:
    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_score_and_date_on_success(self):
        respx.get(_DEPSDEV_PROJECT_URL.format(owner="psf", repo="requests")).mock(
            return_value=httpx.Response(200, json=_project("psf", "requests")),
        )

        result = await scorecard.fetch_overall("psf", "requests")
        assert result is not None
        score, date = result
        assert score == pytest.approx(7.4)
        assert date == "2026-04-13"

    @pytest.mark.asyncio
    @respx.mock
    async def test_casts_integer_score(self):
        respx.get(_DEPSDEV_PROJECT_URL.format(owner="psf", repo="requests")).mock(
            return_value=httpx.Response(
                200, json=_project("psf", "requests", score=8),
            ),
        )

        result = await scorecard.fetch_overall("psf", "requests")
        assert result is not None
        score, _date = result
        assert isinstance(score, float)
        assert score == pytest.approx(8.0)

    @pytest.mark.asyncio
    @respx.mock
    async def test_truncates_date_timestamp_to_iso_date(self):
        respx.get(_DEPSDEV_PROJECT_URL.format(owner="psf", repo="requests")).mock(
            return_value=httpx.Response(
                200, json=_project(
                    "psf", "requests", date="2026-04-13T12:34:56Z",
                ),
            ),
        )

        result = await scorecard.fetch_overall("psf", "requests")
        assert result is not None
        _score, date = result
        assert date == "2026-04-13"

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_on_404(self):
        respx.get(_DEPSDEV_PROJECT_URL.format(owner="obscure", repo="repo")).mock(
            return_value=httpx.Response(404),
        )

        assert await scorecard.fetch_overall("obscure", "repo") is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_on_500(self):
        respx.get(_DEPSDEV_PROJECT_URL.format(owner="foo", repo="bar")).mock(
            return_value=httpx.Response(500),
        )

        assert await scorecard.fetch_overall("foo", "bar") is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_on_malformed_json(self):
        respx.get(_DEPSDEV_PROJECT_URL.format(owner="foo", repo="bar")).mock(
            return_value=httpx.Response(200, text="not json"),
        )

        assert await scorecard.fetch_overall("foo", "bar") is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_when_scorecard_field_missing(self):
        # A deps.dev project without a scorecard subfield still returns
        # 200; we treat that as "no score available".
        respx.get(_DEPSDEV_PROJECT_URL.format(owner="foo", repo="bar")).mock(
            return_value=httpx.Response(
                200, json=_project("foo", "bar", include_scorecard=False),
            ),
        )

        assert await scorecard.fetch_overall("foo", "bar") is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_when_overall_score_missing(self):
        respx.get(_DEPSDEV_PROJECT_URL.format(owner="foo", repo="bar")).mock(
            return_value=httpx.Response(200, json={
                "projectKey": {"id": "github.com/foo/bar"},
                "scorecard": {"date": "2026-01-01T00:00:00Z", "checks": []},
            }),
        )

        assert await scorecard.fetch_overall("foo", "bar") is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_empty_date_when_upstream_omits_it(self):
        respx.get(_DEPSDEV_PROJECT_URL.format(owner="foo", repo="bar")).mock(
            return_value=httpx.Response(200, json={
                "projectKey": {"id": "github.com/foo/bar"},
                "scorecard": {"overallScore": 3.1, "checks": []},
            }),
        )

        result = await scorecard.fetch_overall("foo", "bar")
        assert result == (3.1, "")

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_on_network_error(self):
        respx.get(_DEPSDEV_PROJECT_URL.format(owner="foo", repo="bar")).mock(
            side_effect=httpx.ConnectError("dns failed"),
        )

        assert await scorecard.fetch_overall("foo", "bar") is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_caches_successful_lookup(self):
        route = respx.get(
            _DEPSDEV_PROJECT_URL.format(owner="psf", repo="requests"),
        ).mock(
            return_value=httpx.Response(200, json=_project("psf", "requests")),
        )

        await scorecard.fetch_overall("psf", "requests")
        await scorecard.fetch_overall("psf", "requests")
        assert route.call_count == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_caches_404_as_none(self):
        route = respx.get(
            _DEPSDEV_PROJECT_URL.format(owner="obscure", repo="repo"),
        ).mock(return_value=httpx.Response(404))

        assert await scorecard.fetch_overall("obscure", "repo") is None
        assert await scorecard.fetch_overall("obscure", "repo") is None
        assert route.call_count == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_does_not_cache_network_error(self):
        route = respx.get(
            _DEPSDEV_PROJECT_URL.format(owner="flaky", repo="repo"),
        ).mock(side_effect=httpx.ConnectError("transient"))

        await scorecard.fetch_overall("flaky", "repo")
        await scorecard.fetch_overall("flaky", "repo")
        # Transient errors are not cached; both calls hit the network.
        assert route.call_count == 2


class TestFormatScore:
    def test_formats_with_date(self):
        assert scorecard.format_score(7.4, "2026-04-13") == "7.4/10 (@ 2026-04-13)"

    def test_drops_trailing_zero_on_whole_number(self):
        assert scorecard.format_score(10.0, "2026-04-13") == "10/10 (@ 2026-04-13)"

    def test_omits_date_clause_when_empty(self):
        assert scorecard.format_score(6.2, "") == "6.2/10"


class TestRepoActionEnrichment:
    """Confirm that _action_repo surfaces openssf_scorecard in frontmatter
    when the scorecard fetch succeeds, and omits it cleanly otherwise.
    """

    @pytest.mark.asyncio
    @respx.mock
    async def test_score_appears_in_frontmatter(self, monkeypatch):
        async def _score(owner, repo):
            assert (owner, repo) == ("org", "tool")
            return (7.4, "2026-04-13")

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
            }),
        )
        respx.get("https://api.github.com/repos/org/tool/readme").mock(
            return_value=httpx.Response(
                200, text="# README",
                headers={"content-type": "text/plain"},
            ),
        )
        respx.get(
            "https://raw.githubusercontent.com/org/tool/main/CITATION.cff",
        ).mock(return_value=httpx.Response(404))
        respx.get(
            "https://api.github.com/repos/org/tool/contents/.github/ISSUE_TEMPLATE",
        ).mock(return_value=httpx.Response(404))

        result = await github("repo", "org/tool")
        assert "openssf_scorecard: 7.4/10 (@ 2026-04-13)" in result
        assert "Packages(action=project, query=github.com/org/tool)" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_integer_score_formatted_without_trailing_zero(
        self, monkeypatch,
    ):
        async def _score(_owner, _repo):
            return (10.0, "2026-04-13")

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
            }),
        )
        respx.get("https://api.github.com/repos/org/tool/readme").mock(
            return_value=httpx.Response(404),
        )
        respx.get(
            "https://raw.githubusercontent.com/org/tool/main/CITATION.cff",
        ).mock(return_value=httpx.Response(404))
        respx.get(
            "https://api.github.com/repos/org/tool/contents/.github/ISSUE_TEMPLATE",
        ).mock(return_value=httpx.Response(404))

        result = await github("repo", "org/tool")
        # :g format drops trailing zeros on whole-number floats
        assert "openssf_scorecard: 10/10 (@ 2026-04-13)" in result

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
            }),
        )
        respx.get("https://api.github.com/repos/org/tool/readme").mock(
            return_value=httpx.Response(404),
        )
        respx.get(
            "https://raw.githubusercontent.com/org/tool/main/CITATION.cff",
        ).mock(return_value=httpx.Response(404))
        respx.get(
            "https://api.github.com/repos/org/tool/contents/.github/ISSUE_TEMPLATE",
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
            return (6.2, "2026-04-13")

        monkeypatch.setattr(_gh_mod, "_fetch_scorecard_overall", _score)

        respx.get("https://raw.githubusercontent.com/o/r/main/test.py").mock(
            return_value=httpx.Response(200, text="def hello():\n    pass\n"),
        )

        result = await github("file", "o/r/test.py", ref="main")
        assert "openssf_scorecard: 6.2/10 (@ 2026-04-13)" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_score_no_key(self):
        # Conftest stub returns None
        respx.get("https://raw.githubusercontent.com/o/r/HEAD/test.py").mock(
            return_value=httpx.Response(200, text="print('hi')\n"),
        )

        result = await github("file", "o/r/test.py")
        assert "openssf_scorecard" not in result
