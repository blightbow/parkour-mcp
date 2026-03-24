"""Shared fixtures for kagi-research-mcp tests."""

import sys

import pytest

import kagi_research_mcp.semantic_scholar
_s2_mod = sys.modules["kagi_research_mcp.semantic_scholar"]


@pytest.fixture(autouse=True)
def _disable_s2_rate_limit(monkeypatch):
    """Disable the 1s rate limiter in unit tests."""
    monkeypatch.setattr(_s2_mod, "_S2_MIN_INTERVAL", 0.0)


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
        {"authorId": "1234", "name": "Ashish Vaswani"},
        {"authorId": "5678", "name": "Noam Shazeer"},
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
