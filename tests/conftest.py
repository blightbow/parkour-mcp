"""Shared fixtures for parkour-mcp tests."""

import pathlib
import sys
import time

import httpx
import pytest

from parkour_mcp.common import init_tool_names

# Initialize tool display names once for the entire test session.
# Uses "code" profile so tool_name() calls in hint/note strings resolve
# to PascalCase names (WebFetchIncisive, SemanticScholar, etc.).
init_tool_names("code")

import parkour_mcp.semantic_scholar  # noqa: E402

_s2_mod = sys.modules["parkour_mcp.semantic_scholar"]

import parkour_mcp.doi  # noqa: E402

_doi_mod = sys.modules["parkour_mcp.doi"]

import parkour_mcp.reddit  # noqa: E402

_reddit_mod = sys.modules["parkour_mcp.reddit"]

import parkour_mcp.github  # noqa: E402

_github_mod = sys.modules["parkour_mcp.github"]

import parkour_mcp.scorecard  # noqa: E402

_scorecard_mod = sys.modules["parkour_mcp.scorecard"]

import parkour_mcp.huggingface  # noqa: E402

_hf_mod = sys.modules["parkour_mcp.huggingface"]

import parkour_mcp.ietf  # noqa: E402

_ietf_mod = sys.modules["parkour_mcp.ietf"]

import parkour_mcp.packages  # noqa: E402

_packages_mod = sys.modules["parkour_mcp.packages"]

import parkour_mcp.common  # noqa: E402

_common_mod = sys.modules["parkour_mcp.common"]

import parkour_mcp.discourse  # noqa: E402

_discourse_mod = sys.modules["parkour_mcp.discourse"]

import parkour_mcp.mediawiki  # noqa: E402

_mediawiki_mod = sys.modules["parkour_mcp.mediawiki"]

import parkour_mcp.youtube  # noqa: E402

_youtube_mod = sys.modules["parkour_mcp.youtube"]

import parkour_mcp.markdown  # noqa: E402

_markdown_mod = sys.modules["parkour_mcp.markdown"]

import parkour_mcp.fetch_direct  # noqa: E402

_fetch_direct_mod = sys.modules["parkour_mcp.fetch_direct"]

import parkour_mcp._pipeline  # noqa: E402, F401

_pipeline_mod = sys.modules["parkour_mcp._pipeline"]


@pytest.fixture(autouse=True)
def _reset_process_lifetime_state():
    """Clear process-lifetime caches and ledgers before and after each test.

    Several module-level structures persist for the MCP server's lifetime by
    design; under pytest that lifetime spans the whole session, so state from
    one test would leak into every later one. The ``tip`` fire-once ledger and
    ``_JS_SHELL_SEEN`` set are obvious, but the content caches matter too: the
    premature-requires_js oracle treats a URL already in ``_page_cache`` as
    non-cold, so a warm cache from an earlier test silently suppresses the tip
    (and, more broadly, lets one test's fetched content answer another's
    request). Reset per test so cache- and tip-sensitive behavior is observed
    in isolation.
    """
    def _clear():
        _markdown_mod._FIRED_TIPS.clear()
        _fetch_direct_mod._JS_SHELL_SEEN.clear()
        _pipeline_mod._page_cache.clear()
        _pipeline_mod._wiki_cache.clear()
        _youtube_mod._transcript_cache.clear()
        _youtube_mod._yt_info_cache.clear()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _enable_s2_for_tests(monkeypatch):
    """Enable Semantic Scholar integration and disable its rate limiter in tests."""
    monkeypatch.setenv("S2_ACCEPT_TOS", "1")
    monkeypatch.setattr(_s2_mod._s2_limiter, "min_interval", 0.0)


@pytest.fixture(autouse=True)
def _disable_doi_rate_limit(monkeypatch):
    """Disable DOI, DataCite, and CrossRef rate limiters in unit tests."""
    monkeypatch.setattr(_doi_mod._doi_limiter, "min_interval", 0.0)
    monkeypatch.setattr(_doi_mod._datacite_limiter, "min_interval", 0.0)
    monkeypatch.setattr(_doi_mod._crossref_limiter, "min_interval", 0.0)


@pytest.fixture(autouse=True)
def _disable_reddit_rate_limit(monkeypatch):
    """Disable the 2s Reddit rate limiter in unit tests."""
    monkeypatch.setattr(_reddit_mod._reddit_limiter, "min_interval", 0.0)


@pytest.fixture(autouse=True)
def _reddit_oauth_state(monkeypatch):
    """Reset Reddit OAuth module state and seed a valid fake token.

    Module-level token state would otherwise leak across tests.  Seeding a
    far-future token lets content-fetch and fast-path tests skip mocking the
    token mint; auth-flow tests invalidate it (set ``_oauth_expires_at = 0``)
    to exercise minting.  ``monkeypatch.setattr`` auto-reverts after the test.
    """
    monkeypatch.setattr(_reddit_mod, "_oauth_token", "test-token")
    monkeypatch.setattr(_reddit_mod, "_oauth_token_headers", {})
    monkeypatch.setattr(_reddit_mod, "_oauth_expires_at", time.monotonic() + 86400)
    monkeypatch.setattr(_reddit_mod, "_oauth_backend", "mobile")
    yield
    # Cancel any refresh daemon a test may have started.
    task = getattr(_reddit_mod, "_refresh_task", None)
    if task is not None:
        task.cancel()
        monkeypatch.setattr(_reddit_mod, "_refresh_task", None)


class _FakeWreqStatus:
    def __init__(self, code):
        self._code = code

    def as_int(self):
        return self._code


class _FakeWreqHeaders:
    """wreq returns header values as bytes; the fake does too.

    Returning str here would hide a decode bug in the replay-header path,
    which is the one place reddit.py reads a response header and feeds it
    back into a later request.
    """

    def __init__(self, mapping):
        self._pairs = {k.lower(): v.encode() for k, v in (mapping or {}).items()}

    def get(self, key):
        return self._pairs.get(key.lower())

    def __iter__(self):
        return iter(self._pairs.items())


class FakeResponse:
    """Stand-in for a ``wreq`` Response used in the Reddit tests.

    Only implements what reddit.py reads: ``status.as_int()``, ``url``,
    ``headers.get()`` returning bytes, and an awaitable ``json()``.
    """

    def __init__(self, status_code=200, json_data=None, headers=None, url=""):
        self.status = _FakeWreqStatus(status_code)
        self._json = json_data
        self.headers = _FakeWreqHeaders(headers)
        self.url = url

    async def json(self):
        return self._json


class _FakeAsyncSession:
    """URL-keyed mock for wreq's Client — respx-like semantics.

    Each URL maps to a queue of responses (or exceptions).  A single
    registered response repeats for every call; multiple registrations are
    consumed in order, the last one repeating once the queue drains.  This
    lets a test mock a 401-then-200 sequence on the same URL to exercise the
    Reddit OAuth refresh-and-retry path.
    """

    def __init__(self):
        self._get: dict[str, list] = {}
        self._head: dict[str, list] = {}
        self._post: dict[str, list] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, url, **_):
        return self._dispatch("GET", self._get, url)

    async def head(self, url, **_):
        # HEAD defaults to 200 with url-as-final to support redirect-follow tests
        return self._dispatch("HEAD", self._head, url, default=FakeResponse(200, url=url))

    async def post(self, url, **_):
        return self._dispatch("POST", self._post, url)

    @staticmethod
    def _dispatch(method, table, url, default=None):
        queue = table.get(url)
        if not queue:
            if default is not None:
                return default
            raise RuntimeError(f"No mock registered for {method} {url}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def mock_get(self, url, *, status=200, json_data=None, headers=None, final_url=None):
        self._get.setdefault(url, []).append(
            FakeResponse(status, json_data=json_data, headers=headers, url=final_url or url)
        )

    def mock_head(self, url, *, status=200, headers=None, final_url=None):
        self._head.setdefault(url, []).append(
            FakeResponse(status, headers=headers, url=final_url or url)
        )

    def mock_post(self, url, *, status=200, json_data=None, headers=None):
        self._post.setdefault(url, []).append(
            FakeResponse(status, json_data=json_data, headers=headers, url=url)
        )

    def raise_on_get(self, url, exc):
        self._get.setdefault(url, []).append(exc)


@pytest.fixture
def fake_async_session(monkeypatch):
    """Replace the wreq Client in reddit.py with a URL-keyed mock.

    Usage:
        fake_async_session.mock_get(url, json_data=..., status=200)
        fake_async_session.mock_head(url, final_url=..., status=200)
        fake_async_session.raise_on_get(url, cc_exc.Timeout("..."))
    """
    fake = _FakeAsyncSession()
    monkeypatch.setattr("parkour_mcp.reddit.Client", lambda *a, **kw: fake)
    return fake


@pytest.fixture(autouse=True)
def _disable_github_rate_limit(monkeypatch):
    """Disable the 1s GitHub rate limiter in unit tests."""
    monkeypatch.setattr(_github_mod._github_limiter, "min_interval", 0.0)


@pytest.fixture(autouse=True)
def _stub_scorecard_for_github(monkeypatch):
    """Default every GitHub repo/file test to a no-op scorecard lookup.

    Scorecard enrichment is strictly additive, and existing assertions don't
    expect the new frontmatter key. Stubbing the reference github.py
    imports means those tests don't need to mock the OpenSSF endpoint.
    Tests specifically exercising enrichment re-patch or use respx.
    """
    async def _no_score(_owner, _repo):
        return None

    monkeypatch.setattr(_github_mod, "_fetch_scorecard_overall", _no_score)
    _scorecard_mod._reset_cache()


@pytest.fixture(autouse=True)
def _hf_state(monkeypatch):
    """Disable the HF rate limiter and isolate token lookup.

    ``_get_hf_token`` caches for the process lifetime and falls back to
    ``~/.config/parkour/hf_token``, so without this a developer who happens to
    have a real token on disk would exercise a different auth path than CI.
    """
    monkeypatch.setattr(_hf_mod._hf_limiter, "min_interval", 0.0)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr(
        _hf_mod, "HF_CONFIG_PATH", pathlib.Path("/nonexistent/hf_token"),
    )
    _hf_mod._reset_hf_state()
    yield
    _hf_mod._reset_hf_state()


@pytest.fixture(autouse=True)
def _disable_ietf_rate_limit(monkeypatch):
    """Disable the 1s Datatracker rate limiter in unit tests."""
    monkeypatch.setattr(_ietf_mod._datatracker_limiter, "min_interval", 0.0)


@pytest.fixture(autouse=True)
def _disable_depsdev_rate_limit(monkeypatch):
    """Disable the 1s deps.dev rate limiter in unit tests.

    The limiter lives in ``common.py`` (shared between Packages and
    Scorecard); monkeypatch against the canonical location.
    """
    monkeypatch.setattr(_common_mod._depsdev_limiter, "min_interval", 0.0)


@pytest.fixture(autouse=True)
def _disable_discourse_rate_limit(monkeypatch):
    """Disable Discourse per-host rate limiters in unit tests."""
    monkeypatch.setattr(_discourse_mod, "_DEFAULT_DISCOURSE_INTERVAL", 0.0)
    _discourse_mod._discourse_limiters.clear()


@pytest.fixture(autouse=True)
def _disable_mediawiki_rate_limit(monkeypatch):
    """Disable the 1s MediaWiki rate limiter in unit tests."""
    monkeypatch.setattr(_mediawiki_mod._mediawiki_limiter, "min_interval", 0.0)


# Sample markdown document used across multiple test modules
SAMPLE_MARKDOWN = """\
# Main Title

Some intro text.

## Section One

Content of section one.

## Section Two

Content of section two.

### Subsection A

Nested content under section two.

## Section Three

More content here.
"""

SAMPLE_MARKDOWN_WITH_DUPLICATES = """\
# Page

## Overview

First overview.

### Details

First details.

## History

Some history.

### Details

Second details.
"""

# Minimal MediaWiki API response fixtures

# What a host probe sees when no page was named: siteinfo and nothing else.
# Resolving a host asks only whether an API answers, so this is the shape the
# generator gate has to accept on its own.
MEDIAWIKI_SITEINFO_ONLY = {
    "query": {
        "general": {
            "sitename": "Test Wiki",
            "generator": "MediaWiki 1.43.9",
        },
    }
}

# Anonymous read switched off ($wgGroupPermissions['*']['read'] = false).
# A MediaWiki envelope, so it identifies the software while withholding the
# answer — the distinction the probe exists to report.
MEDIAWIKI_READ_DENIED = {
    "error": {
        "code": "readapidenied",
        "info": "You need read permission to use this module.",
    }
}

MEDIAWIKI_QUERY_RESPONSE = {
    "query": {
        "pages": {
            "42": {
                "pageid": 42,
                "title": "Test_Page",
                "length": 5000,
            }
        },
        "general": {
            "sitename": "Test Wiki",
            "generator": "MediaWiki 1.39.7",
        },
    }
}

MEDIAWIKI_QUERY_MISSING_PAGE = {
    "query": {
        "pages": {
            "-1": {
                "title": "Nonexistent_Page",
                "missing": "",
            }
        },
        "general": {
            "sitename": "Test Wiki",
            "generator": "MediaWiki 1.39.7",
        },
    }
}

MEDIAWIKI_PARSE_FULL_RESPONSE = {
    "parse": {
        "displaytitle": "Test Page",
        "text": {
            "*": '<h2>Section One</h2><p>Content of section one.</p>'
                 '<h2>Section Two</h2><p>Content of section two.</p>'
        },
        "sections": [
            {"index": "1", "line": "Section One", "level": "2"},
            {"index": "2", "line": "Section Two", "level": "2"},
        ],
    }
}

MEDIAWIKI_PARSE_WITH_INLINE_CITATIONS = {
    "parse": {
        "displaytitle": "Test Page",
        "text": {
            "*": (
                '<h2>Appeals in other fields</h2>'
                '<p>Several authors have commented, including '
                '<a href="#CITEREFFranzén2005">Franzén (2005)</a>, and '
                '<a href="#CITEREFSokalBricmont1999">Sokal &amp; Bricmont (1999)</a>. '
                'A second mention of '
                '<a href="#CITEREFFranzén2005">Franzén (2005)</a> '
                'appears later.</p>'
                '<h2>Bibliography</h2>'
                '<cite id="CITEREFFranzén2005">'
                'Franzén, Torkel (2005). '
                '<a class="external" href="https://example.com/franzen">'
                "Gödel's Theorem: An Incomplete Guide"
                '</a>.'
                '</cite>'
                '<cite id="CITEREFSokalBricmont1999">'
                'Sokal, A.; Bricmont, J. (1999). Fashionable Nonsense.'
                '</cite>'
            ),
        },
        "sections": [
            {"index": "1", "line": "Appeals in other fields", "level": "2"},
            {"index": "2", "line": "Bibliography", "level": "2"},
        ],
    }
}

MEDIAWIKI_PARSE_WITH_CITATIONS = {
    "parse": {
        "displaytitle": "Test Page",
        "text": {
            "*": '<h2>Section One</h2><p>Content of section one.[^1]</p>'
                 '<h2>References</h2>'
                 '<ol class="references">'
                 '<li><span class="reference-text">First reference source.</span></li>'
                 '<li><span class="reference-text">Second reference source.</span></li>'
                 '<li><span class="reference-text">Third reference source.</span></li>'
                 '</ol>'
        },
        "sections": [
            {"index": "1", "line": "Section One", "level": "2"},
            {"index": "2", "line": "References", "level": "2"},
        ],
    }
}

MEDIAWIKI_PARSE_SECTIONS_RESPONSE = {
    "parse": {
        "displaytitle": "Test Page",
        "sections": [
            {"index": "1", "line": "Section One", "level": "2"},
            {"index": "2", "line": "<i>Section Two</i>", "level": "2"},
        ],
    }
}

MEDIAWIKI_PARSE_SECTION_TEXT = {
    "parse": {
        "text": {
            "*": "<h2>Section Two</h2><p>Content of section two.</p>"
        }
    }
}

# MediaWiki list=search API response fixture — for the MediaWiki tool's
# search action.  Shape mirrors what action=query&list=search returns,
# including the <span class="searchmatch"> highlighting around matched
# terms in the snippet field.
MEDIAWIKI_SEARCH_RESPONSE = {
    "batchcomplete": "",
    "query": {
        "searchinfo": {
            "totalhits": 1337,
        },
        "search": [
            {
                "ns": 0,
                "title": "Gödel's incompleteness theorems",
                "pageid": 12345,
                "size": 85000,
                "wordcount": 12500,
                "snippet": (
                    'Two theorems of mathematical logic by Kurt '
                    '<span class="searchmatch">Gödel</span>, '
                    'establishing limits of axiomatic systems.'
                ),
                "timestamp": "2025-11-01T08:30:00Z",
            },
            {
                "ns": 0,
                "title": "Kurt Gödel",
                "pageid": 67890,
                "size": 42000,
                "wordcount": 6800,
                "snippet": (
                    'Austrian-American logician known for his work on '
                    '<span class="searchmatch">incompleteness</span> '
                    'theorems.'
                ),
                "timestamp": "2025-10-15T14:22:00Z",
            },
        ],
    },
}

MEDIAWIKI_SEARCH_EMPTY_RESPONSE = {
    "batchcomplete": "",
    "query": {
        "searchinfo": {
            "totalhits": 0,
        },
        "search": [],
    },
}

# Sample HTML responses for fetch_direct tests

SAMPLE_HTML_PAGE = """\
<html>
<head><title>Test Page</title></head>
<body>
<h1>Main Heading</h1>
<p>This is a paragraph with enough text to pass the span length filter for extraction.</p>
<h2>Second Section</h2>
<p>Another paragraph with sufficient content to be included in the extracted output.</p>
<h3>Subsection</h3>
<p>Some nested subsection content that should also appear in the extracted text output.</p>
</body>
</html>
"""

SAMPLE_JSON_CONTENT = '{"key": "value", "list": [1, 2, 3]}'

SAMPLE_PLAIN_TEXT = """\
First paragraph of plain text content.

Second paragraph of plain text content.

Third paragraph with enough words to pass filters.
"""

# Semantic Scholar API response fixtures

S2_PAPER_SEARCH_RESPONSE = {
    "total": 1542,
    "offset": 0,
    "data": [
        {
            "paperId": "204e3073870fae3d05bcbc2f6a8e263d9b72e776",
            "title": "Attention is All you Need",
            "year": 2017,
            "authors": [
                {"authorId": "1234", "name": "Ashish Vaswani"},
                {"authorId": "5678", "name": "Noam Shazeer"},
            ],
            "citationCount": 120000,
            "referenceCount": 44,
            "publicationTypes": ["JournalArticle", "Conference"],
            "journal": {"name": "Advances in Neural Information Processing Systems"},
            "openAccessPdf": {"url": "https://arxiv.org/pdf/1706.03762"},
            "tldr": {"model": "tldr@v2", "text": "A new network architecture based solely on attention mechanisms."},
        },
        {
            "paperId": "abcdef1234567890abcdef1234567890abcdef12",
            "title": "BERT: Pre-training of Deep Bidirectional Transformers",
            "year": 2019,
            "authors": [
                {"authorId": "9999", "name": "Jacob Devlin"},
            ],
            "citationCount": 85000,
            "referenceCount": 52,
            "publicationTypes": ["JournalArticle"],
            "journal": {"name": "NAACL"},
            "openAccessPdf": None,
            "tldr": None,
        },
    ],
}

S2_PAPER_DETAIL_RESPONSE = {
    "paperId": "204e3073870fae3d05bcbc2f6a8e263d9b72e776",
    "title": "Attention is All you Need",
    "year": 2017,
    "authors": [
        {
            "authorId": "1234",
            "name": "Ashish Vaswani",
            "affiliations": ["Google Brain"],
            "externalIds": {"ORCID": "0000-0002-1234-5678"},
        },
        {
            "authorId": "5678",
            "name": "Noam Shazeer",
            "affiliations": ["Google Brain"],
            "externalIds": {},
        },
    ],
    "abstract": "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks.",
    "venue": "NeurIPS",
    "citationCount": 120000,
    "referenceCount": 44,
    "publicationTypes": ["JournalArticle", "Conference"],
    "journal": {"name": "Advances in Neural Information Processing Systems"},
    "externalIds": {
        "DOI": "10.48550/arXiv.1706.03762",
        "ArXiv": "1706.03762",
    },
    "openAccessPdf": {"url": "https://arxiv.org/pdf/1706.03762"},
    "tldr": {"model": "tldr@v2", "text": "A new network architecture based solely on attention mechanisms."},
    "publicationDate": "2017-06-12",
    "citationStyles": {
        "bibtex": "@Article{Vaswani2017AttentionIA,\n author = {Ashish Vaswani and Noam Shazeer},\n journal = {Advances in Neural Information Processing Systems},\n title = {Attention is All you Need},\n year = {2017}\n}",
    },
}

# `contexts` sits on the citation edge beside `citedPaper`, not inside it.
# Verified against the live /references endpoint on 2026-08-14: an edge item's
# keys are exactly ['citedPaper', 'contexts']. An earlier hand-written version
# of this fixture nested contexts inside citedPaper and left it empty, which
# made the reference handler's context lifting untestable in two ways at once.
S2_REFERENCE_RESPONSE = {
    "offset": 0,
    "next": 1,
    "data": [
        {
            "contexts": [
                "We build on the attention mechanism introduced by [Bahdanau et al., 2015].",
            ],
            "citedPaper": {
                "paperId": "bbb222bbb222bbb222bbb222bbb222bbb222bbb2",
                "title": "Neural Machine Translation by Jointly Learning to Align and Translate",
                "year": 2015,
                "authors": [{"authorId": "4444", "name": "Dzmitry Bahdanau"}],
                "citationCount": 25000,
                "venue": "ICLR",
            },
        },
    ],
}

S2_AUTHOR_SEARCH_RESPONSE = {
    "total": 5,
    "offset": 0,
    "data": [
        {
            "authorId": "1234",
            "name": "Ashish Vaswani",
            "affiliations": ["Google Brain"],
            "paperCount": 42,
            "citationCount": 200000,
            "hIndex": 25,
        },
    ],
}

S2_AUTHOR_DETAIL_RESPONSE = {
    "authorId": "1234",
    "name": "Ashish Vaswani",
    "affiliations": ["Google Brain"],
    "paperCount": 42,
    "citationCount": 200000,
    "hIndex": 25,
}

S2_AUTHOR_PAPERS_RESPONSE = {
    "data": [
        {
            "paperId": "204e3073870fae3d05bcbc2f6a8e263d9b72e776",
            "title": "Attention is All you Need",
            "year": 2017,
            "citationCount": 120000,
            "venue": "NeurIPS",
        },
    ],
}

S2_TEXT_AVAILABILITY_FULLTEXT = {
    "paperId": "204e3073870fae3d05bcbc2f6a8e263d9b72e776",
    "title": "Attention is All you Need",
    "textAvailability": "fulltext",
}

S2_TEXT_AVAILABILITY_NONE = {
    "paperId": "204e3073870fae3d05bcbc2f6a8e263d9b72e776",
    "title": "Attention is All you Need",
    "textAvailability": "abstract",
}

S2_SNIPPET_RESPONSE = {
    "data": [
        {
            "score": 0.95,
            "paper": {
                "corpusId": 204,
                "title": "Attention is All you Need",
                "authors": [
                    {"name": "Ashish Vaswani"},
                    {"name": "Noam Shazeer"},
                ],
            },
            "snippet": {
                "text": "Multi-head attention allows the model to jointly attend to information from different representation subspaces at different positions.",
                "snippetKind": "body",
                "section": "Multi-Head Attention",
                "snippetOffset": 1234,
                "annotations": [],
            },
        },
        {
            "score": 0.88,
            "paper": {
                "corpusId": 204,
                "title": "Attention is All you Need",
                "authors": [
                    {"name": "Ashish Vaswani"},
                    {"name": "Noam Shazeer"},
                ],
            },
            "snippet": {
                "text": "We propose a new simple network architecture, the Transformer, based solely on attention mechanisms.",
                "snippetKind": "abstract",
                "section": "Abstract",
                "snippetOffset": 0,
                "annotations": [],
            },
        },
        {
            "score": 0.82,
            "paper": {
                "corpusId": 204,
                "title": "Attention is All you Need",
                "authors": [
                    {"name": "Ashish Vaswani"},
                    {"name": "Noam Shazeer"},
                ],
            },
            "snippet": {
                "text": "An attention function can be described as mapping a query and a set of key-value pairs to an output.",
                "snippetKind": "body",
                "section": "Scaled Dot-Product Attention",
                "snippetOffset": 2345,
                "annotations": [],
            },
        },
    ],
}

S2_SNIPPET_CORPUS_RESPONSE = {
    "data": [
        {
            "score": 0.95,
            "paper": {
                "corpusId": 204,
                "title": "Attention is All you Need",
                "authors": [{"name": "Ashish Vaswani"}],
            },
            "snippet": {
                "text": "Multi-head attention allows the model to jointly attend to information.",
                "snippetKind": "body",
                "section": "Multi-Head Attention",
                "snippetOffset": 1234,
                "annotations": [],
            },
        },
        {
            "score": 0.85,
            "paper": {
                "corpusId": 999,
                "title": "BERT: Pre-training of Deep Bidirectional Transformers",
                "authors": [{"name": "Jacob Devlin"}],
            },
            "snippet": {
                "text": "We use multi-headed self-attention to encode the input sequence.",
                "snippetKind": "body",
                "section": "Model Architecture",
                "snippetOffset": 567,
                "annotations": [],
            },
        },
    ],
}


# ---------------------------------------------------------------------------
# Liveness gate for third-party hosts used by the live suite
# ---------------------------------------------------------------------------

# Each key names a host the live suite depends on; the value is a URL a healthy
# host answers 2xx for.  It is deliberately *not* a URL any test asserts
# against: `httpbin` carries a test that expects a 404, and probing that would
# report a healthy host as down.
#
# `www.maytag.com` is absent on purpose.  Its reachability cannot be
# established independently of the behaviour `TestLiveAkamaiHttp2` asserts:
# Akamai resets a plain connection, returns 403 to HTTP/2 with a browser UA,
# and only `guarded_fetch`'s full header set gets 200.  A probe weak enough to
# be independent reports the host down while it is serving; a probe strong
# enough to succeed *is* the test.  That one test reports its own transport
# failure clearly enough to stand alone.
_LIVE_PROBES = {
    "ultimacodex": "https://wiki.ultimacodex.com/wiki/Ultima_VIII_books",
    "httpbin": "https://httpbin.org/json",
    "github-api": "https://api.github.com/",
    "github-raw": (
        "https://raw.githubusercontent.com/pallets/flask/main/README.md"
    ),
}

_PROBE_TIMEOUT = 15.0

# key -> None when serving, else a one-line reason.  Populated once per session:
# a host that is down is down for every test that needs it, and re-probing per
# test turns one outage into dozens of slow timeouts.
_probe_results: dict[str, str | None] = {}


def _probe_host(key: str) -> str | None:
    """Return None when the host is serving, else why it is not."""
    if key in _probe_results:
        return _probe_results[key]
    url = _LIVE_PROBES[key]
    try:
        with httpx.Client(timeout=_PROBE_TIMEOUT, follow_redirects=True) as client:
            response = client.get(url)
        reason = None if response.status_code < 400 else f"HTTP {response.status_code}"
    except Exception as exc:  # noqa: BLE001 - any transport failure means down
        reason = f"{type(exc).__name__}"
    _probe_results[key] = reason
    return reason


def pytest_runtest_setup(item):
    """Short-circuit a test whose third-party host is not serving.

    Without this an outage surfaces as a spray of assertion errors that read
    exactly like a regression, and diagnosing it costs a manual walk of every
    failing test before the cause is even a hypothesis.  Reporting it once per
    host, by name and status code, leaves no ambiguity.

    **Fails rather than skips, deliberately.**  pytest's own guidance names
    skip as the idiom for "an external resource which is not available at the
    moment", and for an ordinary suite that is right.  These tests gate a
    release, where a quiet skip converts "we could not verify this" into a
    green run, and "all tests passed" only means something once you know which
    tests ran.  A suite that silently sheds its live coverage during an outage
    would ship unverified and say nothing about it.

    **Reports as ERROR, not FAILED, and that is the accurate label.**  pytest
    classifies a setup-phase failure as an error, which reads as "this test did
    not run" rather than "this test ran and its assertions failed".  That is
    exactly the distinction the gate exists to draw, so the check stays in
    setup rather than being pushed into the call phase to force a FAILED.

    **Probes at runtime rather than collection time.**  Collection-time
    conditions are ordinarily preferable because they surface in the collection
    output, but collection runs for every invocation, so probing there would
    fire four network requests on every mocked run.  The marker only ever
    attaches to live tests, so setup is the earliest point that costs nothing
    when the live suite is deselected.
    """
    for marker in item.iter_markers(name="requires_live"):
        for key in marker.args:
            if key not in _LIVE_PROBES:
                pytest.fail(
                    f"requires_live({key!r}) names no host in _LIVE_PROBES; "
                    f"known keys: {sorted(_LIVE_PROBES)}",
                    pytrace=False,
                )
            if reason := _probe_host(key):
                pytest.fail(
                    f"liveness gate: {_LIVE_PROBES[key]} unreachable ({reason}). "
                    f"Third-party host is not serving, so this test was not run "
                    f"and its result says nothing about this repo.",
                    pytrace=False,
                )


# ---------------------------------------------------------------------------
# wreq transport double
# ---------------------------------------------------------------------------
# The generic fetch path runs on wreq, which respx cannot see: respx hooks
# httpx's transport, and wreq never touches it.  Rather than restate ~240
# mocked routes in a second vocabulary, the double replaces `build_client`
# (the single seam through which `_transport` reaches wreq) with a client that
# issues the request through httpx.  respx therefore keeps intercepting every
# route exactly as written, for the generic path and the httpx fast paths
# alike, which is what lets both live in one test file.
#
# What this deliberately does NOT cover is wreq itself: fingerprint emulation,
# real address pinning, and `remote_addr` verification are all absent here
# because httpx is standing in.  `tests/test_transport.py` covers the module's
# own guarantees against hand-built responses, and `test_live.py` covers the
# parts that only a real connection can prove.

from wreq import exceptions as wreq_exceptions  # noqa: E402

import parkour_mcp._transport as _transport_mod  # noqa: E402


class _WreqStatus:
    def __init__(self, code: int) -> None:
        self._code = code

    def as_int(self) -> int:
        return self._code

    def is_redirection(self) -> bool:
        return 300 <= self._code < 400


class _WreqHeaders:
    """wreq's HeaderMap shape: iterates (bytes, bytes), no items()."""

    def __init__(self, headers) -> None:
        self._pairs = [(k.lower().encode(), v.encode()) for k, v in headers.items()]

    def __iter__(self):
        return iter(self._pairs)

    def get(self, key):
        wanted = key.lower().encode()
        return next((v for k, v in self._pairs if k == wanted), None)


class _WreqVersion:
    def __init__(self, name: str) -> None:
        self.name = name


class _WreqStream:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def __aiter__(self):
        async def gen():
            if self._body:
                yield self._body

        return gen()


class _WreqShapedResponse:
    """Presents an ``httpx.Response`` through the surface `_transport` reads."""

    def __init__(self, resp: httpx.Response) -> None:
        self.status = _WreqStatus(resp.status_code)
        self.headers = _WreqHeaders(resp.headers)
        # httpx exposes no peer address, so pin verification degrades to
        # "unknown" here.  _verify_pin treats that as unpinned rather than
        # asserting, which is the safe direction for a stand-in.
        self.remote_addr = None
        self.version = _WreqVersion("HTTP_11")
        advertised = resp.headers.get("content-length")
        self.content_length = int(advertised) if advertised and advertised.isdigit() else 0
        self._body = resp.content

    def stream(self):
        return _WreqStream(self._body)


class _HttpxBackedClient:
    """Stands in for wreq's Client, issuing through httpx so respx sees it."""

    async def request(self, method, url, headers=None):
        # _transport dispatches through request() so it can issue HEAD; the
        # double has to carry the same surface or every call raises
        # AttributeError and surfaces as a transport failure.
        # wreq's Method is a pyo3 enum with no `.name`, and str() yields
        # "Method.GET", so the verb has to be taken off the tail.
        return await self._issue(str(method).rsplit(".", 1)[-1], url, headers)

    async def get(self, url, headers=None):
        return await self._issue("GET", url, headers)

    async def _issue(self, method, url, headers=None):
        # follow_redirects=False because `_transport._follow` walks the chain
        # itself, address-checking each hop; letting httpx follow too would
        # skip those checks and diverge from production.
        try:
            async with httpx.AsyncClient(follow_redirects=False) as client:
                resp = await client.request(method, url, headers=headers)
        except httpx.TimeoutException as exc:
            # The double owes callers wreq's contract, exceptions included.
            # Leaking httpx types here would make _transport's error mapping
            # untestable: a timeout would fall through to the catch-all and be
            # reported as a generic transport failure.
            raise wreq_exceptions.TimeoutError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise wreq_exceptions.ConnectionError(type(exc).__name__) from exc
        return _WreqShapedResponse(resp)


@pytest.fixture(autouse=True)
def _wreq_via_httpx(request, monkeypatch):
    """Route the generic fetch path through httpx so respx keeps working.

    Autouse so no test can reach the real network through `_transport` by
    omission.  Tests that want to drive the transport directly re-patch these
    same two names and win, since their own fixture resolves later.

    **Not applied to tests marked ``live``.**  Those exist to exercise the real
    wreq stack against real origins, and standing httpx in front of it makes
    them assert nothing about the transport they are named after: the
    Cloudflare case passes because httpx clears that zone over HTTP/1.1, and
    both pinning cases report ``pinned=False`` because httpx exposes no peer
    address.  The suite is deselected by default, so this failed silently as
    three green tests until ``just tag`` ran the live suite.
    """
    if request.node.get_closest_marker("live"):
        return

    monkeypatch.setattr(
        _transport_mod, "build_client", lambda **_kwargs: _HttpxBackedClient()
    )

    async def _stub_resolve(_host, _port):
        # A public address, so the check passes without a DNS lookup.  Tests
        # that exercise refusal patch this themselves.
        return ["93.184.216.34"]

    monkeypatch.setattr(_transport_mod, "_resolve_and_check", _stub_resolve)
