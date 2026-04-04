"""Tests for kagi_research_mcp.github module (mocked, no network)."""

import httpx
import pytest
import respx

from kagi_research_mcp.github import (
    _detect_github_url,
    _github_request,
    _next_page_url,
    _parse_citation_cff,
    _parse_owner_repo,
    _parse_owner_repo_number,
    _parse_owner_repo_path,
    _sectionize_code,
    extract_code_definitions,
    format_code_sections,
    github,
)
from kagi_research_mcp._pipeline import _page_cache
from kagi_research_mcp.shelf import _get_shelf, _reset_shelf


@pytest.fixture(autouse=True)
def clear_page_cache():
    yield
    _page_cache.clear()


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

class TestDetectGithubUrl:
    def test_blob(self):
        m = _detect_github_url("https://github.com/owner/repo/blob/main/src/app.py")
        assert m is not None and m.kind == "blob"
        assert m.owner == "owner" and m.repo == "repo"
        assert m.ref == "main" and m.path == "src/app.py"

    def test_tree(self):
        m = _detect_github_url("https://github.com/owner/repo/tree/v2.0/src")
        assert m is not None and m.kind == "tree"
        assert m.ref == "v2.0" and m.path == "src"

    def test_issue(self):
        m = _detect_github_url("https://github.com/owner/repo/issues/42")
        assert m is not None and m.kind == "issue"
        assert m.number == 42

    def test_pull(self):
        m = _detect_github_url("https://github.com/owner/repo/pull/99")
        assert m is not None and m.kind == "pull"
        assert m.number == 99

    def test_pull_with_subpath(self):
        m = _detect_github_url("https://github.com/owner/repo/pull/99/files")
        assert m is not None and m.kind == "pull" and m.number == 99

    def test_repo_root(self):
        m = _detect_github_url("https://github.com/owner/repo")
        assert m is not None and m.kind == "repo"
        assert m.owner == "owner" and m.repo == "repo"

    def test_repo_git_suffix(self):
        m = _detect_github_url("https://github.com/owner/repo.git")
        assert m is not None and m.kind == "repo" and m.repo == "repo"

    def test_gist(self):
        m = _detect_github_url("https://gist.github.com/user/abc123")
        assert m is not None and m.kind == "gist" and m.gist_id == "abc123"

    def test_gist_no_user(self):
        m = _detect_github_url("https://gist.github.com/abc123")
        assert m is not None and m.kind == "gist"

    def test_settings_rejected(self):
        assert _detect_github_url("https://github.com/settings/tokens") is None

    def test_orgs_rejected(self):
        assert _detect_github_url("https://github.com/orgs/myorg") is None

    def test_non_github(self):
        assert _detect_github_url("https://example.com/owner/repo") is None

    def test_http_scheme(self):
        m = _detect_github_url("http://github.com/owner/repo/issues/1")
        assert m is not None and m.kind == "issue"


# ---------------------------------------------------------------------------
# Query parsing
# ---------------------------------------------------------------------------

class TestQueryParsing:
    def test_owner_repo_number(self):
        result = _parse_owner_repo_number("facebook/react#1234")
        assert result == ("facebook", "react", 1234)

    def test_owner_repo_number_invalid(self):
        result = _parse_owner_repo_number("not-valid")
        assert isinstance(result, str) and "Error" in result

    def test_owner_repo(self):
        result = _parse_owner_repo("facebook/react")
        assert result == ("facebook", "react")

    def test_owner_repo_invalid(self):
        result = _parse_owner_repo("just-one-part")
        assert isinstance(result, str) and "Error" in result

    def test_owner_repo_path(self):
        result = _parse_owner_repo_path("facebook/react/src/React.js")
        assert result == ("facebook", "react", "src/React.js")

    def test_owner_repo_path_invalid(self):
        result = _parse_owner_repo_path("facebook/react")
        assert isinstance(result, str) and "Error" in result


# ---------------------------------------------------------------------------
# Link header parsing
# ---------------------------------------------------------------------------

class TestNextPageUrl:
    def test_extracts_next(self):
        link = '<https://api.github.com/repos/x/y?page=2>; rel="next", <https://api.github.com/repos/x/y?page=5>; rel="last"'
        assert _next_page_url(link) == "https://api.github.com/repos/x/y?page=2"

    def test_no_next(self):
        link = '<https://api.github.com/repos/x/y?page=1>; rel="prev"'
        assert _next_page_url(link) is None

    def test_none(self):
        assert _next_page_url(None) is None


# ---------------------------------------------------------------------------
# _github_request (mocked)
# ---------------------------------------------------------------------------

class TestGithubRequest:
    @pytest.mark.asyncio
    @respx.mock
    async def test_success_json(self):
        respx.get("https://api.github.com/repos/o/r").mock(
            return_value=httpx.Response(200, json={"full_name": "o/r"})
        )
        result = await _github_request("GET", "/repos/o/r")
        assert isinstance(result, dict)
        assert result["full_name"] == "o/r"

    @pytest.mark.asyncio
    @respx.mock
    async def test_404_returns_error(self):
        respx.get("https://api.github.com/repos/o/nope").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        result = await _github_request("GET", "/repos/o/nope")
        assert isinstance(result, str) and "Not found" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_422_extracts_error(self):
        respx.get("https://api.github.com/search/issues").mock(
            return_value=httpx.Response(422, json={
                "message": "Validation Failed",
                "errors": [{"message": "Bad query syntax"}],
            })
        )
        result = await _github_request("GET", "/search/issues", params={"q": "bad"})
        assert isinstance(result, str) and "Bad query syntax" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_rate_limit_tracked(self):
        from kagi_research_mcp.github import _rate_limits
        respx.get("https://api.github.com/repos/o/r").mock(
            return_value=httpx.Response(200, json={}, headers={
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "4999",
                "x-ratelimit-reset": "1700000000",
                "x-ratelimit-resource": "core",
            })
        )
        await _github_request("GET", "/repos/o/r")
        assert "core" in _rate_limits
        assert _rate_limits["core"].remaining == 4999


# ---------------------------------------------------------------------------
# Action: search_issues (mocked)
# ---------------------------------------------------------------------------

class TestSearchIssues:
    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_results(self):
        respx.get("https://api.github.com/search/issues").mock(
            return_value=httpx.Response(200, json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [{
                    "number": 42,
                    "title": "Test Issue",
                    "state": "open",
                    "labels": [{"name": "bug"}],
                    "updated_at": "2025-01-01T00:00:00Z",
                    "repository_url": "https://api.github.com/repos/owner/repo",
                    "user": {"login": "author"},
                }],
            })
        )
        result = await github("search_issues", "repo:owner/repo test", limit=5)
        assert "Test Issue" in result
        assert "owner/repo#42" in result
        assert "bug" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_results(self):
        respx.get("https://api.github.com/search/issues").mock(
            return_value=httpx.Response(200, json={
                "total_count": 0, "incomplete_results": False, "items": [],
            })
        )
        result = await github("search_issues", "nothing", limit=5)
        assert "No results" in result


# ---------------------------------------------------------------------------
# Action: issue (mocked)
# ---------------------------------------------------------------------------

class TestIssueAction:
    @pytest.mark.asyncio
    @respx.mock
    async def test_issue_with_comments(self):
        respx.get("https://api.github.com/repos/o/r/issues/1").mock(
            return_value=httpx.Response(200, json={
                "number": 1,
                "title": "Bug Report",
                "state": "open",
                "user": {"login": "reporter"},
                "body": "Something is broken",
                "created_at": "2025-01-01T00:00:00Z",
                "comments": 1,
                "labels": [],
                "reactions": {"+1": 3, "-1": 0, "laugh": 0, "hooray": 0,
                              "confused": 0, "heart": 0, "rocket": 0, "eyes": 0},
                "author_association": "NONE",
            })
        )
        respx.get("https://api.github.com/repos/o/r/issues/1/comments").mock(
            return_value=httpx.Response(200, json=[{
                "id": 100,
                "user": {"login": "maintainer"},
                "body": "I'll look into this",
                "created_at": "2025-01-02T00:00:00Z",
                "reactions": {},
                "author_association": "MEMBER",
            }])
        )
        result = await github("issue", "o/r#1")
        assert "Bug Report" in result
        assert "Something is broken" in result
        assert "ic_100" in result
        assert "@maintainer" in result
        assert "MEMBER" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_issue_url_autodetect(self):
        """Passing a full GitHub URL should work."""
        respx.get("https://api.github.com/repos/o/r/issues/5").mock(
            return_value=httpx.Response(200, json={
                "number": 5, "title": "Test", "state": "open",
                "user": {"login": "u"}, "body": "", "created_at": "2025-01-01T00:00:00Z",
                "comments": 0, "labels": [], "reactions": {}, "author_association": "NONE",
            })
        )
        result = await github("issue", "https://github.com/o/r/issues/5")
        assert "o/r#5" in result


# ---------------------------------------------------------------------------
# Action: pull_request (mocked)
# ---------------------------------------------------------------------------

class TestPullRequestAction:
    @pytest.mark.asyncio
    @respx.mock
    async def test_pr_with_review_comments(self):
        respx.get("https://api.github.com/repos/o/r/pulls/10").mock(
            return_value=httpx.Response(200, json={
                "number": 10,
                "title": "Add feature",
                "state": "closed",
                "merged": True,
                "user": {"login": "dev"},
                "body": "This adds a feature",
                "created_at": "2025-01-01T00:00:00Z",
                "additions": 50,
                "deletions": 10,
                "changed_files": 3,
                "base": {"ref": "main"},
                "head": {"ref": "feature-branch"},
                "comments": 0,
                "review_comments": 1,
                "labels": [],
                "author_association": "CONTRIBUTOR",
            })
        )
        respx.get("https://api.github.com/repos/o/r/pulls/10/comments").mock(
            return_value=httpx.Response(200, json=[{
                "id": 200,
                "user": {"login": "reviewer"},
                "body": "Looks good",
                "path": "src/main.py",
                "line": 42,
                "created_at": "2025-01-02T00:00:00Z",
                "diff_hunk": "@@ -40,3 +40,5 @@\n+new code\n+more code",
                "author_association": "MEMBER",
                "in_reply_to_id": None,
            }])
        )
        respx.get("https://api.github.com/repos/o/r/issues/10/comments").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = await github("pull_request", "o/r#10")
        assert "Add feature" in result
        assert "merged" in result
        assert "3 files changed, +50, -10" in result
        assert "rc_200" in result
        assert "src/main.py" in result
        assert "Looks good" in result


# ---------------------------------------------------------------------------
# Action: file (mocked)
# ---------------------------------------------------------------------------

class TestFileAction:
    @pytest.mark.asyncio
    @respx.mock
    async def test_file_fetch(self):
        respx.get("https://raw.githubusercontent.com/o/r/main/test.py").mock(
            return_value=httpx.Response(200, text="def hello():\n    print('hi')\n")
        )
        result = await github("file", "o/r/test.py", ref="main")
        assert "python" in result  # language detection
        assert "def hello():" in result
        assert "1 |" in result  # line numbers

    @pytest.mark.asyncio
    @respx.mock
    async def test_binary_detection(self):
        respx.get("https://raw.githubusercontent.com/o/r/HEAD/image.png").mock(
            return_value=httpx.Response(200, text="PNG\x00\x00\x00binary")
        )
        result = await github("file", "o/r/image.png")
        assert "Binary file" in result


# ---------------------------------------------------------------------------
# Action: repo (mocked)
# ---------------------------------------------------------------------------

class TestRepoAction:
    @pytest.mark.asyncio
    @respx.mock
    async def test_repo_metadata(self):
        respx.get("https://api.github.com/repos/o/r").mock(
            return_value=httpx.Response(200, json={
                "full_name": "o/r",
                "description": "A test repo",
                "stargazers_count": 1000,
                "forks_count": 50,
                "open_issues_count": 10,
                "language": "Python",
                "license": {"spdx_id": "MIT"},
                "topics": ["testing"],
            })
        )
        respx.get("https://api.github.com/repos/o/r/readme").mock(
            return_value=httpx.Response(
                200, text="# README\n\nHello world",
                headers={"content-type": "text/plain"},
            )
        )
        result = await github("repo", "o/r")
        assert "o/r" in result
        assert "1,000" in result  # formatted stars
        assert "Python" in result
        assert "MIT" in result
        assert "testing" in result


# ---------------------------------------------------------------------------
# Action: tree (mocked)
# ---------------------------------------------------------------------------

class TestTreeAction:
    @pytest.mark.asyncio
    @respx.mock
    async def test_directory_listing(self):
        respx.get("https://api.github.com/repos/o/r/contents/src").mock(
            return_value=httpx.Response(200, json=[
                {"name": "utils", "type": "dir", "size": 0},
                {"name": "app.py", "type": "file", "size": 2048},
                {"name": "config.json", "type": "file", "size": 512},
            ])
        )
        result = await github("tree", "o/r/src", ref="main")
        assert "utils/" in result
        assert "app.py" in result
        assert "2.0KB" in result


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------

class TestDispatch:
    @pytest.mark.asyncio
    async def test_invalid_action(self):
        result = await github("invalid", "query")
        assert "Unknown action" in result


# ---------------------------------------------------------------------------
# Source code sectionization
# ---------------------------------------------------------------------------

class TestCodeSectionization:
    def test_python_definitions(self):
        code = '''def hello():
    """Say hello."""
    pass

class Foo:
    """A class."""
    def bar(self):
        return 42
'''
        defs = extract_code_definitions(code, ".py")
        assert len(defs) >= 2
        names = [d.name for d in defs]
        assert "hello" in names
        assert "Foo" in names
        # Check docstrings
        hello_def = next(d for d in defs if d.name == "hello")
        assert hello_def.docstring == "Say hello."
        foo_def = next(d for d in defs if d.name == "Foo")
        assert foo_def.docstring == "A class."

    def test_nested_methods(self):
        code = '''class Calc:
    def add(self):
        pass
    def sub(self):
        pass
'''
        defs = extract_code_definitions(code, ".py")
        calc = next(d for d in defs if d.name == "Calc")
        assert calc.depth == 0
        methods = [d for d in defs if d.depth == 1]
        assert len(methods) == 2

    def test_unknown_extension(self):
        assert extract_code_definitions("code", ".xyz") == []

    def test_format_code_sections(self):
        code = '''def hello():
    """Greet."""
    pass
'''
        defs = extract_code_definitions(code, ".py")
        output = format_code_sections(defs)
        assert "hello" in output
        assert "Greet." in output
        assert "L1-" in output

    def test_sectionize_code_returns_presplit(self):
        code = "def a():\n    pass\n\ndef b():\n    pass\n"
        result = _sectionize_code(code, ".py")
        assert result is not None
        assert isinstance(result, list)
        assert all(isinstance(item, tuple) and len(item) == 2 for item in result)

    def test_sectionize_unknown_ext(self):
        assert _sectionize_code("code", ".xyz") is None


# ---------------------------------------------------------------------------
# CITATION.cff parsing
# ---------------------------------------------------------------------------

class TestParseCitationCff:
    def test_preferred_citation_with_doi(self):
        """preferred-citation DOI takes precedence over top-level."""
        cff = {
            "cff-version": "1.2.0",
            "title": "My Software",
            "doi": "10.5281/zenodo.9999",
            "authors": [{"family-names": "Doe", "given-names": "Jane"}],
            "preferred-citation": {
                "type": "article",
                "title": "My Paper",
                "doi": "10.1234/journal.5678",
                "authors": [
                    {"family-names": "Doe", "given-names": "Jane"},
                    {"family-names": "Smith", "given-names": "John"},
                ],
                "date-released": "2024-03-15",
            },
        }
        doi, title, authors, year = _parse_citation_cff(cff)
        assert doi == "10.1234/journal.5678"
        assert title == "My Paper"
        assert authors == ["Doe, Jane", "Smith, John"]
        assert year == 2024

    def test_top_level_doi_only(self):
        """When no preferred-citation, use top-level fields."""
        cff = {
            "title": "My Tool",
            "doi": "10.5281/zenodo.1111",
            "authors": [{"family-names": "Lee", "given-names": "Alex"}],
            "date-released": "2023-06-01",
        }
        doi, title, authors, year = _parse_citation_cff(cff)
        assert doi == "10.5281/zenodo.1111"
        assert title == "My Tool"
        assert authors == ["Lee, Alex"]
        assert year == 2023

    def test_no_doi(self):
        """CFF without any DOI returns None for doi."""
        cff = {
            "title": "Untitled Software",
            "authors": [{"name": "ACME Corp"}],
        }
        doi, title, authors, year = _parse_citation_cff(cff)
        assert doi is None
        assert title == "Untitled Software"
        assert authors == ["ACME Corp"]
        assert year is None

    def test_preferred_citation_inherits_top_level_doi(self):
        """preferred-citation without DOI falls back to top-level DOI."""
        cff = {
            "doi": "10.5281/zenodo.5555",
            "title": "Software",
            "preferred-citation": {
                "title": "The Paper",
                "authors": [{"family-names": "Foo", "given-names": "Bar"}],
            },
        }
        doi, title, authors, year = _parse_citation_cff(cff)
        assert doi == "10.5281/zenodo.5555"
        assert title == "The Paper"

    def test_year_from_year_field(self):
        """Year can come from a 'year' field instead of date-released."""
        cff = {
            "title": "Tool",
            "preferred-citation": {
                "title": "Paper",
                "year": 2022,
                "authors": [],
            },
        }
        _, _, _, year = _parse_citation_cff(cff)
        assert year == 2022

    def test_author_formats(self):
        """Various author field combinations are handled."""
        cff = {
            "title": "Tool",
            "authors": [
                {"family-names": "Doe", "given-names": "Jane"},
                {"family-names": "Solo"},
                {"name": "The Community"},
                {},  # empty author
            ],
        }
        _, _, authors, _ = _parse_citation_cff(cff)
        assert authors == ["Doe, Jane", "Solo", "The Community"]


# ---------------------------------------------------------------------------
# Shelf integration (mocked repo action)
# ---------------------------------------------------------------------------

class TestRepoShelfIntegration:
    @pytest.fixture(autouse=True)
    def reset_shelf(self):
        _reset_shelf()
        yield
        _reset_shelf()

    @pytest.mark.asyncio
    @respx.mock
    async def test_repo_with_citation_cff_doi(self):
        """Repo with CITATION.cff containing DOI tracks with real DOI."""
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
                "created_at": "2023-01-01T00:00:00Z",
            })
        )
        respx.get("https://api.github.com/repos/org/tool/readme").mock(
            return_value=httpx.Response(200, text="# README",
                                        headers={"content-type": "text/plain"})
        )
        cff_yaml = (
            "cff-version: 1.2.0\n"
            "title: My Tool\n"
            "preferred-citation:\n"
            "  type: article\n"
            "  title: The Paper\n"
            "  doi: 10.1234/paper.5678\n"
            "  authors:\n"
            "    - family-names: Smith\n"
            "      given-names: John\n"
            "  date-released: 2024-01-15\n"
        )
        respx.get(
            "https://raw.githubusercontent.com/org/tool/main/CITATION.cff"
        ).mock(return_value=httpx.Response(200, text=cff_yaml))

        result = await github("repo", "org/tool")
        assert "shelf:" in result

        shelf = _get_shelf()
        records = await shelf.list_all()
        assert len(records) == 1
        assert records[0].doi == "10.1234/paper.5678"
        assert records[0].title == "The Paper"
        assert records[0].source_tool == "github"

    @pytest.mark.asyncio
    @respx.mock
    async def test_repo_without_citation_cff(self):
        """Repo without CITATION.cff tracks with synthetic github: key."""
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
                "created_at": "2023-01-01T00:00:00Z",
            })
        )
        respx.get("https://api.github.com/repos/org/tool/readme").mock(
            return_value=httpx.Response(200, text="# README",
                                        headers={"content-type": "text/plain"})
        )
        respx.get(
            "https://raw.githubusercontent.com/org/tool/main/CITATION.cff"
        ).mock(return_value=httpx.Response(404))

        result = await github("repo", "org/tool")
        assert "shelf:" in result

        shelf = _get_shelf()
        records = await shelf.list_all()
        assert len(records) == 1
        assert records[0].doi == "github:org/tool"
        assert "A tool" in records[0].title
        assert records[0].source_tool == "github"

    @pytest.mark.asyncio
    @respx.mock
    async def test_repo_citation_cff_no_doi(self):
        """CFF without DOI uses synthetic key but real metadata."""
        respx.get("https://api.github.com/repos/org/lib").mock(
            return_value=httpx.Response(200, json={
                "full_name": "org/lib",
                "description": "A library",
                "stargazers_count": 50,
                "forks_count": 2,
                "open_issues_count": 1,
                "language": "Rust",
                "license": {"spdx_id": "Apache-2.0"},
                "topics": [],
                "default_branch": "main",
                "created_at": "2022-06-01T00:00:00Z",
            })
        )
        respx.get("https://api.github.com/repos/org/lib/readme").mock(
            return_value=httpx.Response(200, text="# Lib",
                                        headers={"content-type": "text/plain"})
        )
        cff_yaml = (
            "cff-version: 1.2.0\n"
            "title: My Library\n"
            "authors:\n"
            "  - family-names: Doe\n"
            "    given-names: Jane\n"
            "date-released: 2022-11-01\n"
        )
        respx.get(
            "https://raw.githubusercontent.com/org/lib/main/CITATION.cff"
        ).mock(return_value=httpx.Response(200, text=cff_yaml))

        await github("repo", "org/lib")

        shelf = _get_shelf()
        records = await shelf.list_all()
        assert len(records) == 1
        assert records[0].doi == "github:org/lib"
        assert records[0].title == "My Library"
        assert records[0].authors == ["Doe, Jane"]
        assert records[0].year == 2022
