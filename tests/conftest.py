"""Shared fixtures for parkour-mcp tests."""

import sys

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

import parkour_mcp.reddit  # noqa: E402, F401
_reddit_mod = sys.modules["parkour_mcp.reddit"]

import parkour_mcp.github  # noqa: E402, F401
_github_mod = sys.modules["parkour_mcp.github"]

import parkour_mcp.ietf  # noqa: E402, F401
_ietf_mod = sys.modules["parkour_mcp.ietf"]

import parkour_mcp.packages  # noqa: E402, F401
_packages_mod = sys.modules["parkour_mcp.packages"]

import parkour_mcp.discourse  # noqa: E402, F401
_discourse_mod = sys.modules["parkour_mcp.discourse"]


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
def _disable_github_rate_limit(monkeypatch):
    """Disable the 1s GitHub rate limiter in unit tests."""
    monkeypatch.setattr(_github_mod._github_limiter, "min_interval", 0.0)


@pytest.fixture(autouse=True)
def _disable_ietf_rate_limit(monkeypatch):
    """Disable the 1s Datatracker rate limiter in unit tests."""
    monkeypatch.setattr(_ietf_mod._datatracker_limiter, "min_interval", 0.0)


@pytest.fixture(autouse=True)
def _disable_depsdev_rate_limit(monkeypatch):
    """Disable the 1s deps.dev rate limiter in unit tests."""
    monkeypatch.setattr(_packages_mod._depsdev_limiter, "min_interval", 0.0)


@pytest.fixture(autouse=True)
def _disable_discourse_rate_limit(monkeypatch):
    """Disable Discourse per-host rate limiters in unit tests."""
    monkeypatch.setattr(_discourse_mod, "_DEFAULT_DISCOURSE_INTERVAL", 0.0)
    _discourse_mod._discourse_limiters.clear()


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

S2_CITATION_RESPONSE = {
    "total": 120000,
    "offset": 0,
    "data": [
        {
            "citingPaper": {
                "paperId": "aaa111aaa111aaa111aaa111aaa111aaa111aaa1",
                "title": "BERT: Pre-training of Deep Bidirectional Transformers",
                "year": 2019,
                "authors": [{"authorId": "9999", "name": "Jacob Devlin"}],
                "citationCount": 85000,
                "venue": "NAACL",
                "contexts": ["Building on the Transformer architecture from [Vaswani et al., 2017]..."],
            }
        },
    ],
}

S2_REFERENCE_RESPONSE = {
    "offset": 0,
    "next": 1,
    "data": [
        {
            "citedPaper": {
                "paperId": "bbb222bbb222bbb222bbb222bbb222bbb222bbb2",
                "title": "Neural Machine Translation by Jointly Learning to Align and Translate",
                "year": 2015,
                "authors": [{"authorId": "4444", "name": "Dzmitry Bahdanau"}],
                "citationCount": 25000,
                "venue": "ICLR",
                "contexts": [],
            }
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
