"""Parkour MCP Server - Web browsing and content extraction tools for Claude."""

import argparse
import logging

from mcp.server.fastmcp import FastMCP

from .kagi import search, summarize
from .fetch_js import web_fetch_js
from .fetch_direct import web_fetch_direct, web_fetch_sections
from .semantic_scholar import semantic_scholar
from .arxiv import arxiv
from .github import github
from .ietf import ietf
from .packages import packages
from .shelf import research_shelf, _get_shelf
from .common import TOOL_NAMES, init_tool_names

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("parkour-mcp")

# Per-profile template variables — tool names and description overrides.
# code profile: PascalCase (WebSearch, WebFetch)
# desktop profile: snake_case (web_search, web_fetch)
PROFILE_VARS = {
    "code": {
        "search": "WebSearch",
        "fetch": "WebFetch",
        "fetch_direct": "WebFetchExact",
        "summarize": "KagiSummarize",
        "fetch_direct_when_to_use": (
            "Unlike WebFetch, fetches through the user's device instead of proxying through\n"
            "Anthropic's servers. Uses precise content extraction techniques and clean\n"
            "first-party APIs for navigating content instead of summarization.\n"
            "Use this for a rich content exploring experience that is not subject to 403\n"
            "bans of data-center subnets, or for extracting specific details that\n"
            "summarization would discard."
        ),
    },
    "desktop": {
        "search": "web_search",
        "fetch": "web_fetch",
        "fetch_direct": "web_fetch_exact",
        "summarize": "kagi_summarize",
        "fetch_direct_when_to_use": (
            "Unlike web_fetch, fetches through the user's device instead of proxying through\n"
            "Anthropic's servers. Uses precise content extraction techniques and clean\n"
            "first-party APIs for navigating content instead of summarization.\n"
            "Use this for a rich content exploring experience that is not subject to 403\n"
            "bans of data-center subnets, or when web_fetch is rejected with PERMISSIONS_ERROR.\n"
        ),
    },
}

# Tool descriptions — one entry per tool, with {var} placeholders resolved per-profile.
TOOL_DESCRIPTIONS = {
    "search": """Search the web using Kagi's curated search index.

Use this as an alternative to {search} when it returns few or poor quality
results. Kagi's index is independently curated, resistant to SEO spam, and
may surface different sources. Returns compact results with snippets and
timestamps — much lighter on context than {search}'s summarized snippets,
making it better suited for multi-query research workflows.

Supports search operators in the query string:
- site:example.com — restrict to a domain
- filetype:pdf — restrict to a file type
- intitle:term — match in page title
- inurl:term — match in URL
- "exact phrase" — exact match
- +term / -term — require / exclude a term
- (A AND B), (A OR B) — boolean grouping, e.g. recipes (szechuan OR cantonese)
- * — wildcard word substitution, e.g. best * ever""",

    "web_fetch_sections": """List a document's section headings to understand page composition or plan targeted extraction.

Returns a heading tree with anchor slugs. Use this to identify sections of
interest, then extract them with {fetch_direct}'s section parameter. URL
fragments (e.g. #section-name) are resolved against the heading tree.

For a quick sense of document scope, prefer this over {summarize} — the
section tree reveals structure at minimal cost.

For Reddit threads, returns a comment tree with author, score, and content
length metadata. Comment IDs serve as section headings for targeted
extraction with {fetch_direct}.""",

    "web_fetch_direct": """Fetch and extract unsummarized content from URLs as markdown.

{fetch_direct_when_to_use}

Targeted extraction (preferred over fetching full pages):
- section="Syntax" — extract a specific section by heading name
- search="terms" — BM25 keyword search over ~500-token slices
- slices=[3, 4, 5] — retrieve specific slices by index
- footnotes=[1, 3] — retrieve specific [^N] entries from MediaWiki pages
- URL fragments (#section-name) are resolved automatically as sections

Always use this tool for Reddit URLs — built-in fetch tools cannot access
Reddit content when proxied.

Supports HTML, plain text, JSON, and XML content types.""",

    "web_fetch_js": """Fetch and interact with JavaScript-rendered web content.

Use this when {fetch_direct} returns incomplete content from JS-heavy sites
(SPAs, React/Vue/Angular apps, dynamically loaded content). Supports the
same targeted extraction as {fetch_direct}: section, search, slices, and
footnotes parameters.

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
handled automatically by {fetch_direct}.

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
handled automatically by {fetch_direct}.

Actions: search, paper, references, author_search, author, snippets.

The snippets action does BM25 keyword search within paper body text
(~500-word excerpts tagged by section, terms matched independently).
Use paper_id to scope to a single paper, or omit for corpus-wide search.
Example: action="snippets", query="multi-head attention",
paper_id="204e3073870fae3d05bcbc2f6a8e263d9b72e776".""",

    "github": """Search and retrieve code, issues, and pull requests from GitHub.

Use this for GitHub lookups: search issues/PRs across repositories, search code,
get issue or PR details with comments, fetch file content from a specific ref, or
get repo metadata with README. GitHub URLs are also handled automatically by
{fetch_direct} — this tool is for structured queries by owner/repo/number.

Actions: search_issues, search_code, issue, pull_request, file, repo, tree.

Query formats vary by action:
- search_issues/search_code: GitHub search query with qualifiers (repo:, is:, label:, language:, path:)
- issue/pull_request: "owner/repo#number" (e.g. "pallets/flask#5618")
- file/tree: "owner/repo/path" (e.g. "pallets/flask/src/flask/app.py") — use ref= for branch/tag
- repo: "owner/repo" (e.g. "pallets/flask")

Authentication: Set GITHUB_TOKEN env var or create ~/.config/parkour/github_token
for 5000 req/hr (vs 60/hr unauthenticated). No special scopes needed for public repos.""",

    "ietf": """Search and retrieve IETF RFCs, Internet-Drafts, and standards-track documents.

Use this for RFC lookups: get RFC details (abstract, authors, status, relationship
chains), search RFCs by keyword, look up Internet-Drafts, or resolve STD/BCP/FYI
subseries bundles. RFC Editor and Datatracker URLs are also handled automatically
by {fetch_direct}.

Actions: rfc, search, draft, subseries.

Query formats:
- rfc: RFC number (e.g. "9110"), RFC URL, or DOI (10.17487/RFC9110)
- search: keywords for title search via IETF Datatracker
- draft: Internet-Draft name (e.g. "draft-ietf-httpbis-semantics") or URL
- subseries: subseries identifier (e.g. "STD97", "BCP14", "FYI36")

Optional filters for search: status (ps, std, bcp, inf, exp, hist), wg (working
group acronym like "httpbis" or "tls").

RFCs have native DOIs (10.17487/RFC{{N}}) and are automatically tracked on the
research shelf when inspected.""",

    "packages": """Search and inspect software packages across language ecosystems via deps.dev.

Use this for package lookups: get version history, licenses, security advisories,
dependency graphs, OpenSSF Scorecards, and SLSA provenance data. Covers 7 ecosystems:
npm, PyPI, Go, Maven, Cargo, NuGet, and RubyGems.

Actions: package, version, dependencies, project, advisory.

Query formats:
- package/version/dependencies: ecosystem/name[@version] (e.g. "pypi/requests", "npm/express@4.18.2")
- project: github.com/owner/repo (e.g. "github.com/psf/requests")
- advisory: advisory ID (e.g. "GHSA-9hjg-9r4m-mvj7")

Ecosystem aliases: pypi, npm, cargo/crates, go/golang, maven, nuget, rubygems/gems.

For repository details (README, issues, code), use {fetch_direct} or the GitHub tool.""",

    "research_shelf": """Manage the research shelf — an in-memory tracker for papers inspected during research.

Papers are automatically added when you use ArXiv, SemanticScholar, DOI, or IETF
tools to inspect individual papers or RFCs. Use this tool to review, score, confirm,
or remove tracked papers, and to export citations in BibTeX or RIS format.

The shelf survives context compaction within the same session. For cross-session
persistence, use export json to save the shelf to a memory file, then import
it in a future session.""",
}


def _build_description(tool_name: str, profile: str) -> str:
    """Build a tool description by resolving placeholders for the given profile."""
    return TOOL_DESCRIPTIONS[tool_name].format(**PROFILE_VARS[profile])


def main():
    """Run the MCP server."""
    parser = argparse.ArgumentParser(description="Parkour MCP Server")
    parser.add_argument(
        "--profile",
        choices=["code", "desktop"],
        default="desktop",
        help="Target client profile (default: desktop)",
    )
    args = parser.parse_args()

    init_tool_names(args.profile)

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
        ("github", github),
        ("ietf", ietf),
        ("packages", packages),
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
        records = await shelf.list_all()
        if not records:
            return "Research shelf is empty."
        from .shelf import _format_shelf_list
        return _format_shelf_list(records)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
