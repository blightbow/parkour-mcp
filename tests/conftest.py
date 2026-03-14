"""Shared fixtures for claude-web-tools tests."""

import pytest


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
