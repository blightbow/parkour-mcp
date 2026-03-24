"""Tests for kagi_research_mcp.semantic_scholar module."""

import httpx
import pytest
import respx

import sys

import kagi_research_mcp.semantic_scholar
# Alias the module before importing the same-named function
_s2_module = sys.modules["kagi_research_mcp.semantic_scholar"]

from kagi_research_mcp.semantic_scholar import (
    S2_BASE_URL,
    S2_CONFIG_PATH,
    _detect_s2_url,
    _fetch_s2_paper,
    _format_author,
    _format_paper_detail,
    _format_paper_list,
    _format_snippets,
    _get_s2_api_key,
    _s2_request,
    semantic_scholar,
)
from kagi_research_mcp._pipeline import _s2_fast_path

from .conftest import (
    S2_PAPER_SEARCH_RESPONSE,
    S2_PAPER_DETAIL_RESPONSE,
    S2_REFERENCE_RESPONSE,
    S2_AUTHOR_SEARCH_RESPONSE,
    S2_AUTHOR_DETAIL_RESPONSE,
    S2_AUTHOR_PAPERS_RESPONSE,
    S2_TEXT_AVAILABILITY_FULLTEXT,
    S2_TEXT_AVAILABILITY_NONE,
    S2_SNIPPET_RESPONSE,
    S2_SNIPPET_CORPUS_RESPONSE,
)


# ---------------------------------------------------------------------------
# _detect_s2_url
# ---------------------------------------------------------------------------

class TestDetectS2Url:
    def test_standard_url_with_slug(self):
        url = "https://www.semanticscholar.org/paper/Attention-Is-All-You-Need-Vaswani-Shazeer/204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        assert _detect_s2_url(url) == "204e3073870fae3d05bcbc2f6a8e263d9b72e776"

    def test_url_without_slug(self):
        url = "https://www.semanticscholar.org/paper/204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        assert _detect_s2_url(url) == "204e3073870fae3d05bcbc2f6a8e263d9b72e776"

    def test_url_without_www(self):
        url = "https://semanticscholar.org/paper/Attention-Is-All-You-Need-Vaswani-Shazeer/204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        assert _detect_s2_url(url) == "204e3073870fae3d05bcbc2f6a8e263d9b72e776"

    def test_url_with_query_params(self):
        url = "https://www.semanticscholar.org/paper/Attention-Is-All-You-Need-Vaswani-Shazeer/204e3073870fae3d05bcbc2f6a8e263d9b72e776?sort=relevance"
        assert _detect_s2_url(url) == "204e3073870fae3d05bcbc2f6a8e263d9b72e776"

    def test_non_s2_url(self):
        assert _detect_s2_url("https://arxiv.org/abs/1706.03762") is None

    def test_s2_non_paper_url(self):
        assert _detect_s2_url("https://www.semanticscholar.org/author/1234") is None

    def test_http_scheme(self):
        url = "http://www.semanticscholar.org/paper/204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        assert _detect_s2_url(url) == "204e3073870fae3d05bcbc2f6a8e263d9b72e776"


# ---------------------------------------------------------------------------
# _get_s2_api_key
# ---------------------------------------------------------------------------

class TestGetS2ApiKey:
    def test_env_var_precedence(self, monkeypatch, tmp_path):
        monkeypatch.setenv("S2_API_KEY", "env-key-123")
        assert _get_s2_api_key() == "env-key-123"

    def test_config_file_fallback(self, monkeypatch, tmp_path):
        monkeypatch.delenv("S2_API_KEY", raising=False)
        key_file = tmp_path / "s2_api_key"
        key_file.write_text("file-key-456\n")
        monkeypatch.setattr(_s2_module, "S2_CONFIG_PATH", key_file)
        assert _get_s2_api_key() == "file-key-456"

    def test_missing_key(self, monkeypatch, tmp_path):
        monkeypatch.delenv("S2_API_KEY", raising=False)
        monkeypatch.setattr(_s2_module, "S2_CONFIG_PATH", tmp_path / "nonexistent")
        assert _get_s2_api_key() == ""

    def test_env_var_overrides_config_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("S2_API_KEY", "env-key")
        key_file = tmp_path / "s2_api_key"
        key_file.write_text("file-key")
        monkeypatch.setattr(_s2_module, "S2_CONFIG_PATH", key_file)
        assert _get_s2_api_key() == "env-key"


# ---------------------------------------------------------------------------
# _s2_request
# ---------------------------------------------------------------------------

class TestS2Request:
    @pytest.mark.asyncio
    @respx.mock
    async def test_success(self):
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        result = await _s2_request("/paper/search", {"query": "test"})
        assert isinstance(result, dict)
        assert result == {"data": []}

    @pytest.mark.asyncio
    @respx.mock
    async def test_404(self):
        respx.get(f"{S2_BASE_URL}/paper/invalid").mock(
            return_value=httpx.Response(404, json={"error": "Paper not found"})
        )
        result = await _s2_request("/paper/invalid")
        assert "Not found" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_without_key(self, monkeypatch):
        monkeypatch.delenv("S2_API_KEY", raising=False)
        monkeypatch.setattr(_s2_module, "S2_CONFIG_PATH", Path("/nonexistent/path"))
        monkeypatch.setattr(_s2_module, "_S2_MAX_RETRIES", 0)
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(429)
        )
        result = await _s2_request("/paper/search", {"query": "test"})
        assert "Rate limited" in result
        assert "S2_API_KEY" in result
        assert "api-key-form" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_with_key(self, monkeypatch):
        monkeypatch.setenv("S2_API_KEY", "my-key")
        monkeypatch.setattr(_s2_module, "_S2_MAX_RETRIES", 0)
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(429)
        )
        result = await _s2_request("/paper/search", {"query": "test"})
        assert "Rate limited" in result
        assert "Try again" in result
        assert "S2_API_KEY" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_retry_then_success(self, monkeypatch):
        monkeypatch.setattr(_s2_module, "_S2_RETRY_BACKOFF", 0.0)
        route = respx.get(f"{S2_BASE_URL}/paper/search")
        route.side_effect = [
            httpx.Response(429),
            httpx.Response(200, json={"data": []}),
        ]
        result = await _s2_request("/paper/search", {"query": "test"})
        assert isinstance(result, dict)
        assert result == {"data": []}
        assert route.call_count == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_exhausts_retries(self, monkeypatch):
        monkeypatch.setattr(_s2_module, "_S2_MAX_RETRIES", 2)
        monkeypatch.setattr(_s2_module, "_S2_RETRY_BACKOFF", 0.0)
        monkeypatch.setenv("S2_API_KEY", "my-key")
        route = respx.get(f"{S2_BASE_URL}/paper/search")
        route.mock(return_value=httpx.Response(429))
        result = await _s2_request("/paper/search", {"query": "test"})
        assert "Rate limited" in result
        assert route.call_count == 3  # initial + 2 retries

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout(self):
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        result = await _s2_request("/paper/search", {"query": "test"})
        assert "timed out" in result


# ---------------------------------------------------------------------------
# semantic_scholar — search
# ---------------------------------------------------------------------------

class TestSemanticScholarSearch:
    @pytest.mark.asyncio
    @respx.mock
    async def test_keyword_search(self):
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(200, json=S2_PAPER_SEARCH_RESPONSE)
        )
        result = await semantic_scholar("search", "attention mechanism transformers")
        assert result.startswith("---\n")
        assert "api: Semantic Scholar" in result
        assert "action: search" in result
        assert "hint:" in result
        assert "Attention is All you Need" in result
        assert "Vaswani" in result
        assert "1,542" in result  # total in pagination hint

    @pytest.mark.asyncio
    @respx.mock
    async def test_empty_results(self):
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(200, json={"total": 0, "data": []})
        )
        result = await semantic_scholar("search", "xyznonexistent")
        assert "No papers found" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_pagination(self):
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(200, json=S2_PAPER_SEARCH_RESPONSE)
        )
        result = await semantic_scholar("search", "attention", offset=10, limit=5)
        assert "offset" in result.lower() or "paginate" in result.lower()


# ---------------------------------------------------------------------------
# semantic_scholar — paper
# ---------------------------------------------------------------------------

class TestSemanticScholarPaper:
    @pytest.mark.asyncio
    @respx.mock
    async def test_by_id(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
        )
        result = await semantic_scholar("paper", paper_id)
        assert "Attention is All you Need" in result
        assert "Vaswani" in result
        assert "10.48550/arXiv.1706.03762" in result
        assert "Abstract" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_by_doi_prefix(self):
        doi = "DOI:10.48550/arXiv.1706.03762"
        respx.get(f"{S2_BASE_URL}/paper/{doi}").mock(
            return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
        )
        result = await semantic_scholar("paper", doi)
        assert "Attention is All you Need" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_by_s2_url(self):
        s2_url = "https://www.semanticscholar.org/paper/Attention-Is-All-You-Need-Vaswani-Shazeer/204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
        )
        result = await semantic_scholar("paper", s2_url)
        assert "Attention is All you Need" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_not_found(self):
        respx.get(f"{S2_BASE_URL}/paper/nonexistent").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        result = await semantic_scholar("paper", "nonexistent")
        assert "Not found" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_s2_paper_frontmatter(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
        )
        result = await _fetch_s2_paper(paper_id)
        assert "---" in result
        assert "api: Semantic Scholar" in result
        assert "source:" in result
        assert "see_also:" in result
        assert "ARXIV:1706.03762" in result


# ---------------------------------------------------------------------------
# semantic_scholar — paper includes citation counts
# ---------------------------------------------------------------------------

class TestSemanticScholarPaperCitationCounts:
    @pytest.mark.asyncio
    @respx.mock
    async def test_paper_includes_influential_count(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        detail = dict(S2_PAPER_DETAIL_RESPONSE)
        detail["influentialCitationCount"] = 4542
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=detail)
        )
        result = await semantic_scholar("paper", paper_id)
        assert "120,000" in result
        assert "4,542 influential" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_paper_action_includes_frontmatter(self):
        """The paper action should include YAML frontmatter like the URL interception path."""
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
        )
        result = await semantic_scholar("paper", paper_id)
        assert result.startswith("---\n")
        assert "api: Semantic Scholar" in result
        assert f"source: https://www.semanticscholar.org/paper/{paper_id}" in result
        assert "see_also:" in result
        assert "ARXIV:1706.03762" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_paper_without_arxiv_id(self):
        """Paper without arXiv ID should not include see_also."""
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        detail = dict(S2_PAPER_DETAIL_RESPONSE)
        detail["externalIds"] = {"DOI": "10.1234/test"}
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=detail)
        )
        result = await semantic_scholar("paper", paper_id)
        assert result.startswith("---\n")
        assert "see_also" not in result


# ---------------------------------------------------------------------------
# semantic_scholar — references
# ---------------------------------------------------------------------------

class TestSemanticScholarReferences:
    @pytest.mark.asyncio
    @respx.mock
    async def test_basic_references(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}/references").mock(
            return_value=httpx.Response(200, json=S2_REFERENCE_RESPONSE)
        )
        result = await semantic_scholar("references", paper_id)
        assert result.startswith("---\n")
        assert "api: Semantic Scholar" in result
        assert "action: references" in result
        assert "Bahdanau" in result
        assert "Neural Machine Translation" in result


# ---------------------------------------------------------------------------
# semantic_scholar — author
# ---------------------------------------------------------------------------

class TestSemanticScholarAuthor:
    @pytest.mark.asyncio
    @respx.mock
    async def test_author_search(self):
        respx.get(f"{S2_BASE_URL}/author/search").mock(
            return_value=httpx.Response(200, json=S2_AUTHOR_SEARCH_RESPONSE)
        )
        result = await semantic_scholar("author_search", "Ashish Vaswani")
        assert result.startswith("---\n")
        assert "api: Semantic Scholar" in result
        assert "action: author_search" in result
        assert "Ashish Vaswani" in result
        assert "Google Brain" in result
        assert "1234" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_author_detail_with_papers(self):
        respx.get(f"{S2_BASE_URL}/author/1234").mock(
            return_value=httpx.Response(200, json=S2_AUTHOR_DETAIL_RESPONSE)
        )
        respx.get(f"{S2_BASE_URL}/author/1234/papers").mock(
            return_value=httpx.Response(200, json=S2_AUTHOR_PAPERS_RESPONSE)
        )
        result = await semantic_scholar("author", "1234")
        assert result.startswith("---\n")
        assert "api: Semantic Scholar" in result
        assert "action: author" in result
        assert "source: https://www.semanticscholar.org/author/1234" in result
        assert "Ashish Vaswani" in result
        assert "h-index:** 25" in result
        assert "Attention is All you Need" in result
        assert "Top Papers" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_author_not_found(self):
        respx.get(f"{S2_BASE_URL}/author/0000").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        result = await semantic_scholar("author", "0000")
        assert "Not found" in result


# ---------------------------------------------------------------------------
# Invalid action
# ---------------------------------------------------------------------------

class TestSemanticScholarInvalidAction:
    @pytest.mark.asyncio
    async def test_unknown_action(self):
        result = await semantic_scholar("invalid_action", "test")
        assert "Unknown action" in result
        assert "invalid_action" in result
        assert "snippets" in result


# ---------------------------------------------------------------------------
# semantic_scholar — snippets
# ---------------------------------------------------------------------------

class TestSemanticScholarSnippets:
    @pytest.mark.asyncio
    @respx.mock
    async def test_snippets_with_paper_id(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_TEXT_AVAILABILITY_FULLTEXT)
        )
        respx.get(f"{S2_BASE_URL}/snippet/search").mock(
            return_value=httpx.Response(200, json=S2_SNIPPET_RESPONSE)
        )
        result = await semantic_scholar(
            "snippets", "multi-head attention", paper_id=paper_id
        )
        assert result.startswith("---\n")
        assert "api: Semantic Scholar" in result
        assert "action: snippets" in result
        assert "hint:" in result
        assert f"paper: {paper_id}" in result
        assert "### Multi-Head Attention" in result
        assert "jointly attend" in result
        assert "### Scaled Dot-Product Attention" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_snippets_corpus_wide(self):
        respx.get(f"{S2_BASE_URL}/snippet/search").mock(
            return_value=httpx.Response(200, json=S2_SNIPPET_CORPUS_RESPONSE)
        )
        result = await semantic_scholar("snippets", "multi-head attention")
        assert result.startswith("---\n")
        assert "api: Semantic Scholar" in result
        assert "action: snippets" in result
        assert "## Attention is All you Need" in result
        assert "## BERT" in result
        assert "### Multi-Head Attention" in result
        assert "### Model Architecture" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_snippets_no_full_text(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_TEXT_AVAILABILITY_NONE)
        )
        result = await semantic_scholar(
            "snippets", "attention", paper_id=paper_id
        )
        assert "Full text is not available" in result
        assert "paper action" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_snippets_empty_results(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_TEXT_AVAILABILITY_FULLTEXT)
        )
        respx.get(f"{S2_BASE_URL}/snippet/search").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        result = await semantic_scholar(
            "snippets", "nonexistent topic", paper_id=paper_id
        )
        assert "No snippet matches found" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_snippets_abstract_kind_tagged(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_TEXT_AVAILABILITY_FULLTEXT)
        )
        respx.get(f"{S2_BASE_URL}/snippet/search").mock(
            return_value=httpx.Response(200, json=S2_SNIPPET_RESPONSE)
        )
        result = await semantic_scholar(
            "snippets", "attention", paper_id=paper_id
        )
        # The abstract snippet should be tagged with [abstract]
        assert "[abstract]" in result
        # Body snippets should NOT be tagged
        assert "[body]" not in result


# ---------------------------------------------------------------------------
# S2 fast path (URL interception)
# ---------------------------------------------------------------------------

class TestS2FastPath:
    @pytest.mark.asyncio
    @respx.mock
    async def test_s2_url_intercepted(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
        )
        url = f"https://www.semanticscholar.org/paper/Attention-Is-All-You-Need/{paper_id}"
        result = await _s2_fast_path(url)
        assert result is not None
        assert "Attention is All you Need" in result
        assert "api: Semantic Scholar" in result

    @pytest.mark.asyncio
    async def test_non_s2_url_returns_none(self):
        result = await _s2_fast_path("https://example.com/some-page")
        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_api_error_still_returns_result(self):
        """API errors should still return a result (not None) to avoid CAPTCHA fallback."""
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(500)
        )
        url = f"https://www.semanticscholar.org/paper/{paper_id}"
        result = await _s2_fast_path(url)
        assert result is not None
        assert "Error" in result


# Needed for Path in test_429_without_key
from pathlib import Path
