"""Live integration tests for the GitHub API core.

Skipped by default. Run with:
    GITHUB_TOKEN=$(gh auth token) uv run pytest tests/test_github_live.py -v
    GITHUB_TOKEN=$(gh auth token) uv run pytest -m live -v

Requires GITHUB_TOKEN to be set (or ~/.config/kagi/github_token to exist).
Tests are individually skipped if no token is available.
"""

import httpx
import pytest

from kagi_research_mcp.github import (
    _detect_github_url,
    _get_github_token,
    _github_request,
    _next_page_url,
    _rate_limits,
)

pytestmark = pytest.mark.live

skip_no_token = pytest.mark.skipif(
    not _get_github_token(),
    reason="GITHUB_TOKEN not set",
)


# ---------------------------------------------------------------------------
# URL detection (no network, but good to have alongside live tests)
# ---------------------------------------------------------------------------

class TestUrlDetection:
    def test_blob(self):
        m = _detect_github_url("https://github.com/pallets/flask/blob/main/src/flask/app.py")
        assert m is not None
        assert m.kind == "blob"
        assert m.owner == "pallets"
        assert m.repo == "flask"
        assert m.ref == "main"
        assert m.path == "src/flask/app.py"

    def test_tree(self):
        m = _detect_github_url("https://github.com/pallets/flask/tree/main/src/flask")
        assert m is not None
        assert m.kind == "tree"
        assert m.path == "src/flask"

    def test_issue(self):
        m = _detect_github_url("https://github.com/pallets/flask/issues/5618")
        assert m is not None
        assert m.kind == "issue"
        assert m.number == 5618

    def test_pull(self):
        m = _detect_github_url("https://github.com/pallets/flask/pull/5617")
        assert m is not None
        assert m.kind == "pull"
        assert m.number == 5617

    def test_repo(self):
        m = _detect_github_url("https://github.com/pallets/flask")
        assert m is not None
        assert m.kind == "repo"

    def test_repo_with_git_suffix(self):
        m = _detect_github_url("https://github.com/pallets/flask.git")
        assert m is not None
        assert m.kind == "repo"
        assert m.repo == "flask"

    def test_gist(self):
        m = _detect_github_url("https://gist.github.com/user/abc123def456")
        assert m is not None
        assert m.kind == "gist"
        assert m.gist_id == "abc123def456"

    def test_non_repo_paths_rejected(self):
        assert _detect_github_url("https://github.com/settings/tokens") is None
        assert _detect_github_url("https://github.com/orgs/pallets") is None
        assert _detect_github_url("https://example.com/foo/bar") is None

    def test_discussion_requires_token(self):
        m = _detect_github_url("https://github.com/pallets/flask/discussions/5500")
        if _get_github_token():
            assert m is not None
            assert m.kind == "discussion"
        else:
            assert m is None

    def test_pull_with_subpath(self):
        """PR URL with /files or /commits suffix still matches."""
        m = _detect_github_url("https://github.com/pallets/flask/pull/5617/files")
        assert m is not None
        assert m.kind == "pull"
        assert m.number == 5617


# ---------------------------------------------------------------------------
# Authenticated API requests
# ---------------------------------------------------------------------------

class TestGitHubRequest:
    @skip_no_token
    @pytest.mark.asyncio
    async def test_repo_metadata(self):
        result = await _github_request("GET", "/repos/pallets/flask")
        assert isinstance(result, dict)
        assert result["full_name"] == "pallets/flask"
        assert "stargazers_count" in result
        assert "language" in result

    @skip_no_token
    @pytest.mark.asyncio
    async def test_issue_fetch(self):
        result = await _github_request(
            "GET", "/repos/pallets/flask/issues/5618",
        )
        assert isinstance(result, dict)
        assert result["number"] == 5618
        assert "title" in result
        assert "body" in result
        assert "user" in result

    @skip_no_token
    @pytest.mark.asyncio
    async def test_issue_comments_pagination(self):
        """Fetch comments on a high-comment issue, verify Link header parsing."""
        # pallets/flask#1361 has 57 comments
        result = await _github_request(
            "GET", "/repos/pallets/flask/issues/1361/comments",
            params={"per_page": "30", "page": "1"},
        )
        assert isinstance(result, list)
        assert len(result) == 30

    @skip_no_token
    @pytest.mark.asyncio
    async def test_search_issues(self):
        result = await _github_request(
            "GET", "/search/issues",
            params={"q": "repo:pallets/flask is:issue blueprint", "per_page": "5"},
        )
        assert isinstance(result, dict)
        assert "items" in result
        assert "total_count" in result
        assert len(result["items"]) <= 5

    @skip_no_token
    @pytest.mark.asyncio
    async def test_search_code(self):
        result = await _github_request(
            "GET", "/search/code",
            params={"q": "repo:pallets/flask send_file"},
            accept="application/vnd.github.text-match+json",
        )
        assert isinstance(result, dict)
        assert "items" in result
        # text-match accept header should give us text_matches
        if result["items"]:
            assert "text_matches" in result["items"][0]

    @skip_no_token
    @pytest.mark.asyncio
    async def test_404_returns_error_string(self):
        result = await _github_request(
            "GET", "/repos/pallets/definitely-not-a-real-repo-12345",
        )
        assert isinstance(result, str)
        assert "Error" in result
        assert "Not found" in result

    @skip_no_token
    @pytest.mark.asyncio
    async def test_rate_limit_tracking(self):
        """After a request, rate limit state should be tracked."""
        await _github_request("GET", "/repos/pallets/flask")
        assert "core" in _rate_limits
        rl = _rate_limits["core"]
        assert rl.limit > 0
        assert rl.remaining >= 0
        assert rl.reset_epoch > 0

    @skip_no_token
    @pytest.mark.asyncio
    async def test_search_rate_limit_tracked_separately(self):
        """Search requests should track the 'search' resource limit."""
        await _github_request(
            "GET", "/search/issues",
            params={"q": "repo:pallets/flask test", "per_page": "1"},
        )
        assert "search" in _rate_limits
        assert _rate_limits["search"].resource == "search"

    @skip_no_token
    @pytest.mark.asyncio
    async def test_pr_with_review_comments(self):
        """Fetch PR review comments to verify response structure."""
        # pallets/flask#2936 has 12 review comments
        result = await _github_request(
            "GET", "/repos/pallets/flask/pulls/2936/comments",
        )
        assert isinstance(result, list)
        assert len(result) > 0
        comment = result[0]
        assert "path" in comment
        assert "body" in comment
        assert "diff_hunk" in comment
        assert "user" in comment

    @skip_no_token
    @pytest.mark.asyncio
    async def test_raw_file_fetch(self):
        """Verify raw.githubusercontent.com serves file content."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            headers = {"User-Agent": "kagi-research-mcp-test"}
            token = _get_github_token()
            if token:
                headers["Authorization"] = f"token {token}"
            resp = await client.get(
                "https://raw.githubusercontent.com/pallets/flask/main/src/flask/__init__.py",
                headers=headers,
            )
            assert resp.status_code == 200
            assert "Flask" in resp.text


# ---------------------------------------------------------------------------
# Link header parsing
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CITATION.cff + shelf integration
# ---------------------------------------------------------------------------

class TestCitationCff:
    @skip_no_token
    @pytest.mark.asyncio
    async def test_pytorch_has_doi(self):
        """PyTorch CITATION.cff has a preferred-citation with DOI."""
        from kagi_research_mcp.github import _fetch_citation_cff, _parse_citation_cff
        cff = await _fetch_citation_cff("pytorch", "pytorch", "main")
        assert cff is not None
        doi, title, authors, year = _parse_citation_cff(cff)
        assert doi is not None
        assert doi.startswith("10.")
        assert "PyTorch" in title
        assert len(authors) > 0
        assert year is not None

    @skip_no_token
    @pytest.mark.asyncio
    async def test_scikit_learn_no_doi_but_has_metadata(self):
        """scikit-learn CITATION.cff has preferred-citation but no DOI."""
        from kagi_research_mcp.github import _fetch_citation_cff, _parse_citation_cff
        cff = await _fetch_citation_cff("scikit-learn", "scikit-learn", "main")
        assert cff is not None
        doi, title, authors, year = _parse_citation_cff(cff)
        assert doi is None  # no DOI in their CFF
        assert "Scikit-learn" in title
        assert len(authors) > 0

    @skip_no_token
    @pytest.mark.asyncio
    async def test_flask_no_citation_cff(self):
        """Flask has no CITATION.cff."""
        from kagi_research_mcp.github import _fetch_citation_cff
        cff = await _fetch_citation_cff("pallets", "flask", "main")
        assert cff is None

    @skip_no_token
    @pytest.mark.asyncio
    async def test_repo_action_tracks_on_shelf(self):
        """The repo action should populate the research shelf."""
        from kagi_research_mcp.github import github
        from kagi_research_mcp.shelf import _get_shelf, _reset_shelf
        _reset_shelf()
        result = await github("repo", "pytorch/pytorch")
        assert "shelf:" in result
        shelf = _get_shelf()
        records = await shelf.list_all()
        assert len(records) == 1
        assert records[0].doi.startswith("10.")  # real DOI from CFF
        assert records[0].source_tool == "github"
        _reset_shelf()


class TestLinkHeaderParsing:
    def test_next_link(self):
        header = '<https://api.github.com/repos/x/y/issues?page=2>; rel="next", <https://api.github.com/repos/x/y/issues?page=5>; rel="last"'
        assert _next_page_url(header) == "https://api.github.com/repos/x/y/issues?page=2"

    def test_no_next_link(self):
        header = '<https://api.github.com/repos/x/y/issues?page=1>; rel="prev", <https://api.github.com/repos/x/y/issues?page=5>; rel="last"'
        assert _next_page_url(header) is None

    def test_none_header(self):
        assert _next_page_url(None) is None

    def test_empty_header(self):
        assert _next_page_url("") is None
