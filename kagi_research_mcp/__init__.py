"""Kagi Research MCP Server - Web browsing and content extraction tools for Claude."""

import argparse
import logging

from mcp.server.fastmcp import FastMCP

from .kagi import search, summarize
from .fetch_js import web_fetch_js
from .fetch_direct import web_fetch_direct, web_fetch_sections
from .semantic_scholar import semantic_scholar
from .arxiv import arxiv
from .shelf import research_shelf, _get_shelf

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("kagi-research-mcp")

# Profile-specific tool names to match Claude client conventions
# code profile: PascalCase (WebSearch, WebFetch, WebFetchJS)
# desktop profile: snake_case (web_search, web_fetch, web_fetch_js)
TOOL_NAMES = {
    "search": {"code": "KagiSearch", "desktop": "kagi_search"},
    "web_fetch_sections": {"code": "WebFetchSections", "desktop": "web_fetch_sections"},
    "web_fetch_direct": {"code": "WebFetchDirect", "desktop": "web_fetch_direct"},
    "web_fetch_js": {"code": "WebFetchJS", "desktop": "web_fetch_js"},
    "summarize": {"code": "KagiSummarize", "desktop": "kagi_summarize"},
    "semantic_scholar": {"code": "SemanticScholar", "desktop": "semantic_scholar"},
    "arxiv": {"code": "ArXiv", "desktop": "arxiv"},
    "research_shelf": {"code": "ResearchShelf", "desktop": "research_shelf"},
}

# Profile variables for description templates
# code profile: PascalCase built-in tool names (WebSearch, WebFetch)
# desktop profile: snake_case built-in tool names (web_search, web_fetch)
PROFILE_VARS = {
    "code": {"search": "WebSearch", "fetch": "WebFetch", "fetch_direct": "WebFetchDirect", "summarize": "KagiSummarize"},
    "desktop": {"search": "web_search", "fetch": "web_fetch", "fetch_direct": "web_fetch_direct", "summarize": "kagi_summarize"},
}

# Tool description templates — profile vars are replaced per-profile.
# Per-profile overrides in TOOL_DESCRIPTION_OVERRIDES replace the whole value.
TOOL_DESCRIPTION_TEMPLATES = {
    "search": """Search the web using Kagi's curated search index.

Use this as an alternative to {search} when it returns few or poor quality
results. Kagi's index is independently curated, resistant to SEO spam, and
may surface different sources. Returns search results with snippets and
timestamps, plus related search suggestions.""",

    "web_fetch_sections": """Understand a document's composition by listing its section headings.

A lightweight way to survey what a page covers before committing to a full
fetch. Returns a section tree with heading names and anchor slugs. Use this
to plan targeted extractions — identify the sections you need, then fetch
them individually with {fetch_direct}'s section parameter. If the URL
contains a fragment (e.g. #section-name), resolves it against the heading
tree.

For light summarization, prefer this tool over {summarize} — the section
tree reveals document structure and scope at minimal cost. Reserve
{summarize} for when you need a prose summary of the content itself.""",

    "web_fetch_direct": """Fetch a URL directly from the local machine without JavaScript rendering.

Returns markdown. Use the section parameter to extract specific sections by
heading name. For Wikipedia/MediaWiki pages, inline footnotes appear as [^N]
markers; use the footnotes parameter to retrieve specific entries by number.
Supports HTML, plain text, JSON, and XML content types.

For long or poorly-sectioned pages, use search="terms" for BM25 keyword
search over ~500-token slices (ranked by relevance, terms matched
independently). Use slices=[3, 4, 5] to retrieve specific slices by index.""",

    "web_fetch_js": """Fetch and interact with web content using a headless browser.

Use this when {fetch} returns incomplete content from JavaScript-heavy sites
(SPAs, React/Vue/Angular apps, dynamically loaded content). Use the section
parameter to extract specific sections by heading name.

For long or poorly-sectioned pages, use search="terms" for BM25 keyword
search over ~500-token slices (ranked by relevance, terms matched
independently). Use slices=[3, 4, 5] to retrieve specific slices by index.
For Wikipedia/MediaWiki pages, inline footnotes appear as [^N] markers; use
the footnotes parameter to retrieve specific entries by number.

Supports ReAct-style interaction chains:
1. First call: Fetch page, observe available interactive elements
2. Subsequent calls: Use 'actions' parameter to interact (click, fill, select)
3. Extract updated content after interactions

Actions format (JSON array of objects):
- {{"action": "click", "selector": "button#submit"}}
- {{"action": "fill", "selector": "input[name=query]", "value": "search term"}}
- {{"action": "select", "selector": "select#region", "value": "us-east"}}
- {{"action": "wait", "selector": ".results-loaded"}}

Returns markdown with interactive elements annotated for follow-up actions.""",

    "summarize": """Summarize content from a URL or text using Kagi's Universal Summarizer.

Supports web pages, PDFs, YouTube videos, audio files, and documents.
Use this when {fetch} fails due to agent blacklisting or access restrictions.""",

    "arxiv": """Search and retrieve academic papers from arXiv.

Use this for arXiv paper lookups: search by query, get paper details
(abstract, authors, categories, affiliations, DOI, journal refs), or
browse recent papers by category. arXiv abstract and PDF URLs are also
handled automatically by {fetch_direct} tools.

IMPORTANT: Search uses arXiv query syntax, NOT natural language:
- Field prefixes: ti: (title), au: (author), abs: (abstract),
  cat: (category), all: (all fields), co: (comment), jr: (journal ref)
- Boolean operators: AND, OR, ANDNOT
- Examples: "ti:attention AND cat:cs.CL", "au:vaswani AND ti:transformer"

Actions: search, paper, category.

For citation counts and cross-references, use SemanticScholar with
ARXIV:<id> after retrieving the arXiv ID.""",

    "semantic_scholar": """Search and retrieve academic paper data from Semantic Scholar.

Use this for academic paper lookups: search by keywords, get paper details
(abstract, authors, citation counts, references), and find authors. Paper
details include total and influential citation counts. Accepts paper IDs,
DOI:10.xxx, ARXIV:2301.xxx, or S2 URLs. Semantic Scholar URLs are also
handled automatically by {fetch} tools.

Actions: search, paper, references, author_search, author, snippets.

The snippets action does BM25 keyword search within paper body text
(~500-word excerpts tagged by section, terms matched independently).
Use paper_id to scope to a single paper, or omit for corpus-wide search.
Example: action="snippets", query="multi-head attention",
paper_id="204e3073870fae3d05bcbc2f6a8e263d9b72e776".""",

    "research_shelf": """Manage the research shelf — a persistent tracker for papers inspected during research.

Papers are automatically added when you use ArXiv, SemanticScholar, or DOI
tools to inspect individual papers. Use this tool to review, score, confirm,
or remove tracked papers, and to export citations in BibTeX or RIS format.

The shelf persists across context resets. Use export json / import to save
and restore shelf state via agent memory for cross-session persistence.""",
}

# Per-profile description overrides (replaces the template entirely)
TOOL_DESCRIPTION_OVERRIDES = {
    "web_fetch_direct": {
        "code": """Fetch a URL directly from the local machine without JavaScript rendering.

Unlike {fetch}, returns full unsummarized page text as markdown. Use this
when you need to extract specific data, compare sections, or preserve details
that summarization would discard. Use the section parameter to extract
specific sections by heading name. For Wikipedia/MediaWiki pages, inline
footnotes appear as [^N] markers; use the footnotes parameter to retrieve
specific entries by number.

For long or poorly-sectioned pages, use search="terms" for BM25 keyword
search over ~500-token slices (ranked by relevance, terms matched
independently). Use slices=[3, 4, 5] to retrieve specific slices by index.

Supports HTML, plain text, JSON, and XML content types.""",
        "desktop": """Fetch a URL directly from the local machine without JavaScript rendering.

Unlike {fetch}, fetches from the user's device instead of proxying through
Anthropic's servers. Use this as a fallback when {fetch} returns HTTP 403
errors (target site blocking data-center IPs) or rejects the tool use with
PERMISSIONS_ERROR (URL not yet present in the conversation context).

Returns markdown. Use the section parameter to extract specific sections by
heading name. For Wikipedia/MediaWiki pages, inline footnotes appear as [^N]
markers; use the footnotes parameter to retrieve specific entries by number.

For long or poorly-sectioned pages, use search="terms" for BM25 keyword
search over ~500-token slices (ranked by relevance, terms matched
independently). Use slices=[3, 4, 5] to retrieve specific slices by index.

Supports HTML, plain text, JSON, and XML content types.""",
    },
}


def _build_description(tool_name: str, profile: str) -> str:
    """Build a tool description by resolving templates and overrides."""
    overrides = TOOL_DESCRIPTION_OVERRIDES.get(tool_name, {})
    template = overrides.get(profile, TOOL_DESCRIPTION_TEMPLATES[tool_name])
    return template.format(**PROFILE_VARS[profile])


def main():
    """Run the MCP server."""
    parser = argparse.ArgumentParser(description="Kagi Research MCP Server")
    parser.add_argument(
        "--profile",
        choices=["code", "desktop"],
        default="desktop",
        help="Target client profile (default: desktop)",
    )
    args = parser.parse_args()

    # Register all tools with profile-specific names and descriptions
    tools = [
        ("search", search),
        ("web_fetch_sections", web_fetch_sections),
        ("web_fetch_direct", web_fetch_direct),
        ("web_fetch_js", web_fetch_js),
        ("summarize", summarize),
        ("semantic_scholar", semantic_scholar),
        ("arxiv", arxiv),
        ("research_shelf", research_shelf),
    ]
    for internal_name, func in tools:
        name = TOOL_NAMES[internal_name][args.profile]
        desc = _build_description(internal_name, args.profile)
        mcp.add_tool(func, name=name, description=desc)

    # MCP resource: read-only shelf summary
    @mcp.resource("research://shelf")
    async def shelf_resource() -> str:
        """Current research shelf contents."""
        shelf = _get_shelf()
        records = shelf.list_all()
        if not records:
            return "Research shelf is empty."
        from .shelf import _format_shelf_list
        return _format_shelf_list(records)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
