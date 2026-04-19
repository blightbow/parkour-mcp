"""Parkour MCP Server - Web browsing and content extraction tools for Claude."""

import argparse
import base64
import logging
import pathlib
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import Icon, ToolAnnotations

from .kagi import search, summarize
from .fetch_js import web_fetch_js
from .fetch_direct import web_fetch_direct, web_fetch_sections
from .arxiv import arxiv
from .github import github
from .ietf import ietf
from .packages import packages
from .discourse import discourse
from .mediawiki import mediawiki
from .shelf import research_shelf, _get_shelf
from .common import TOOL_NAMES, init_tool_names, s2_enabled

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool icons — SVG glyphs extracted from Noto fonts (SIL OFL 1.1 licensed).
# Source files live in parkour_mcp/assets/icons/*.svg; encoded to data: URIs
# at startup since the MCP Icon spec requires https:// or data: URIs (no
# local paths). Shipped as package-data so the wheel and the mcpb bundle
# both carry them — see [tool.setuptools.package-data] in pyproject.toml.
# ---------------------------------------------------------------------------
_ICONS_DIR = pathlib.Path(__file__).parent / "assets" / "icons"

# Internal tool key → SVG filename (without .svg extension)
_ICON_FILES = {
    "search": "search",             # 🔍 U+1F50D MAGNIFYING GLASS (NotoSansSymbols2)
    "summarize": "summarize",       # Σ  U+03A3 GREEK CAPITAL SIGMA (NotoSansMono)
    "web_fetch_sections": "sections",  # §  U+00A7 SECTION SIGN (NotoSansMono)
    "web_fetch_direct": "exact",    # ⌖  U+2316 POSITION INDICATOR (NotoSansSymbols2)
    "web_fetch_js": "js",           # ⚡ U+26A1 HIGH VOLTAGE (NotoSansSymbols2)
    "arxiv": "arxiv",               # χ  U+03C7 GREEK SMALL CHI (NotoSansMono)
    "semantic_scholar": "scholar",  # ∴  U+2234 THEREFORE (NotoSansMono)
    "research_shelf": "shelf",      # ⊞  U+229E SQUARED PLUS (NotoSansMath)
    "github": "github",             # ⑂  U+2442 OCR FORK (NotoSansSymbols2)
    "ietf": "ietf",                 # 🐌 U+1F40C SNAIL (NotoEmoji)
    "packages": "packages",         # ⬡  U+2B21 WHITE HEXAGON (NotoSansMath)
    "discourse": "discourse",       # 💬 U+1F4AC SPEECH BALLOON (NotoEmoji)
    "mediawiki": "mediawiki",       # 🤙 U+1F919 SHAKA — "wiki" is Hawaiian (NotoEmoji)
}
_SERVER_ICON_FILE = "server"        # ∮  U+222E CONTOUR INTEGRAL (NotoSansMath)


def _load_icon(filename: str) -> Icon | None:
    """Read an SVG file from assets/icons/ and return it as a data: URI Icon."""
    path = _ICONS_DIR / f"{filename}.svg"
    if not path.is_file():
        logger.warning("Icon file not found: %s", path)
        return None
    svg_bytes = path.read_bytes()
    b64 = base64.b64encode(svg_bytes).decode("ascii")
    return Icon(src=f"data:image/svg+xml;base64,{b64}", mimeType="image/svg+xml")


def _load_tool_icon(key: str) -> list[Icon] | None:
    """Load a tool icon by internal tool key, or None if unavailable."""
    filename = _ICON_FILES.get(key)
    if filename is None:
        return None
    icon = _load_icon(filename)
    return [icon] if icon else None


def _load_server_icons() -> list[Icon] | None:
    """Load the server-level icon."""
    icon = _load_icon(_SERVER_ICON_FILE)
    return [icon] if icon else None


mcp = FastMCP(
    "parkour-mcp",
    icons=_load_server_icons(),
)

# Shared operator reference for tools that expose a ``search=`` parameter
# routed through the tantivy BM25 index in ``_pipeline.py``. Duplicated
# verbatim into each such tool description — tool descriptions must be
# self-contained (deferred loading can surface one tool without the others).
SEARCH_GRAMMAR_DOC = """search= operators (tantivy query language):
- foo bar             — match any term (whitespace is OR)
- +foo +bar           — require both terms
- foo -bar            — exclude 'bar'
- "exact phrase"      — adjacent words in order
- "some words"~3      — phrase with up to 3-word gaps
- (foo OR bar) baz    — grouping + AND/OR/NOT
- foo~                — fuzzy match (edit distance)
Matching is case-insensitive; no stemming (search for both 'prompt' and
'prompts' if you want either). Stray punctuation in natural-language
queries is silently dropped."""

# Per-profile template variables — tool names and description overrides.
# code profile: PascalCase (WebSearch, WebFetch)
# desktop profile: snake_case (web_search, web_fetch)
PROFILE_VARS = {
    "code": {
        "web_search": "WebSearch",
        "web_fetch": "WebFetch",
        "fetch_direct": "WebFetchIncisive",
        "fetch_sections": "WebFetchSections",
        "summarize": "KagiSummarize",
        "mediawiki_tool": "MediaWiki",
        "github_tool": "GitHub",
        "arxiv_tool": "ArXiv",
        "ietf_tool": "IETF",
        "semantic_scholar_tool": "SemanticScholar",
        "shelf_tool": "ResearchShelf",
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
        "web_search": "web_search",
        "web_fetch": "web_fetch",
        "fetch_direct": "web_fetch_incisive",
        "fetch_sections": "web_fetch_sections",
        "summarize": "kagi_summarize",
        "mediawiki_tool": "mediawiki",
        "github_tool": "github",
        "arxiv_tool": "arxiv",
        "ietf_tool": "ietf",
        "semantic_scholar_tool": "semantic_scholar",
        "shelf_tool": "research_shelf",
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

Use this as an alternative to {web_search} when it returns few or poor quality
results. Kagi's index is independently curated, resistant to SEO spam, and
may surface different sources. Returns compact results with snippets and
timestamps — much lighter on context than {web_search}'s summarized snippets,
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

Returns a heading tree with anchor slugs — a cheap structural preview
that avoids pulling the page body. Typical use: call this first to
decide which sections of a long page are worth fetching, then follow up
with {fetch_direct} using the returned heading names as section= or
slugs as slices=. URL fragments (e.g. #section-name) are resolved
against the heading tree.

For a quick sense of document scope, prefer this over {summarize} — the
section tree reveals structure at minimal cost and leaves the source
material unsummarized for precise follow-up.

For Reddit threads, returns the comment tree with author, score, and
content length metadata. Comment IDs serve as section identifiers for
follow-up extraction of specific subthreads.""",

    "web_fetch_direct": """Fetch and extract unsummarized content from URLs as markdown.

{fetch_direct_when_to_use}

Targeted extraction (preferred over fetching full pages):
- section="Syntax" — extract a specific section by heading name
- search="terms" — keyword search over ~500-token slices, ranked by BM25
- slices=[3, 4, 5] — retrieve specific slices by index
- URL fragments (#section-name) are resolved automatically as sections

RECOMMENDED WORKFLOW: For pages of substantial or unknown length, call
{fetch_sections} first to map the heading tree, then come back here with
precise section= or slices= targets. A full-page fetch is rarely the
right first move — it fills context with material you don't need and
discards the structural information that makes follow-up queries cheap.
For Reddit threads, {fetch_sections} returns the comment tree instead.

{search_grammar}

For Wikipedia and other MediaWiki pages, a dedicated companion tool
offers footnote and inline-citation resolution that this fast path can't
provide. When the target page has those reference types, the response
frontmatter surfaces a see_also hint pointing at it.

Always use this tool for Reddit URLs — built-in fetch tools cannot access
Reddit content when proxied. The Reddit fast path targets whole-post
URLs (/r/sub/comments/POSTID/...); individual comment permalinks with a
trailing comment ID return only a context-scoped subtree, not the full
thread. To target a specific comment, fetch the post URL and use
section=COMMENTID.

Supports HTML, plain text, JSON, and XML content types.""",

    "web_fetch_js": """Fetch and interact with JavaScript-rendered web content.

Use this when {fetch_direct} returns incomplete content from JS-heavy sites
(SPAs, React/Vue/Angular apps, dynamically loaded content). For pages that
are fully rendered in the initial HTML response, {fetch_direct} is cheaper
and should be tried first.

Targeted extraction (preferred over fetching full pages):
- section="Syntax" — extract a specific section by heading name
- search="terms" — keyword search over ~500-token slices, ranked by BM25
- slices=[3, 4, 5] — retrieve specific slices by index
- URL fragments (#section-name) are resolved automatically as sections

{search_grammar}

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

    "summarize": """Summarize content from a URL using Kagi's Universal Summarizer.

Position relative to the two fetch tools:
- {web_fetch} summarizes through Anthropic's layer (fast, default;
  subject to agent blacklisting and IP bans)
- {fetch_direct} returns raw unsummarized content (local fetch through
  the user's device; bypasses agent/IP bans that block {web_fetch})
- this tool summarizes through Kagi's layer — returns a condensed
  digest only, never raw content

Reach for this when even {fetch_direct} can't retrieve the source
(captcha-gated pages, anti-bot walls), or for formats neither built-in
fetcher processes: PDFs, YouTube videos, audio files, podcasts. Each
call is billed against the user's Kagi API credits — treat it as
higher-effort than {web_fetch}.

Two summary modes via summary_type= parameter:
- "summary" (default) — flowing prose paragraphs
- "takeaway" — bullet-point key takeaways

Summary length is determined by the source.""",

    "arxiv": """Search and retrieve academic papers from arXiv.

Use this for arXiv paper lookups: search by query, get paper details
(abstract, authors, categories, affiliations, DOI, journal refs), or
browse recent papers by category. arXiv abstract and PDF URLs are also
handled automatically by {fetch_direct}.

Actions: search, paper, category.

Query formats:
- search: arXiv query syntax, NOT natural language (see operators below)
- paper: arXiv ID (e.g. "2301.00001", "cs.CL/0501001") or arXiv URL
- category: arXiv category code (e.g. "cs.CL", "math.CO", "astro-ph.GA")

search operators:
- Field prefixes: ti: (title), au: (author), abs: (abstract),
  cat: (category), all: (all fields), co: (comment), jr: (journal ref)
- Boolean operators: AND, OR, ANDNOT
- Examples: "ti:attention AND cat:cs.CL", "au:vaswani AND ti:transformer"

Papers retrieved via the paper action are automatically tracked on the
research shelf.""",

    "semantic_scholar": """Search and retrieve academic paper data from Semantic Scholar.

Use this for academic paper lookups: search by keywords, get paper details
(abstract, authors, citation counts, references), and find authors. Paper
details include total and influential citation counts. Semantic Scholar
URLs are also handled automatically by {fetch_direct}.

Actions: search, paper, references, author_search, author, snippets.

Query formats:
- search: keyword search terms (full-text across titles, abstracts, authors)
- paper: S2 paper hash, DOI:10.xxx, ARXIV:2301.xxx, or Semantic Scholar URL
- references: same identifier formats as paper — returns papers cited by that work
- author_search: author name string (e.g. "Yoshua Bengio")
- author: S2 author ID (numeric, as returned by author_search)
- snippets: BM25 keyword search within paper body text (~500-word excerpts
  tagged by section, terms matched independently). Use paper_id= to scope
  to a single paper; omit for corpus-wide search.

Example snippet call: action="snippets", query="multi-head attention",
paper_id="204e3073870fae3d05bcbc2f6a8e263d9b72e776".

Papers retrieved via the paper action are automatically tracked on the
research shelf.""",

    "github": """Search and retrieve code, issues, pull requests, and repositories from GitHub.

Use this for GitHub lookups: search issues/PRs across repositories, search for
repositories by topic/stars/language, search code, get issue or PR details with
comments, fetch file content from a specific ref, get repo metadata with README,
or inspect a repo's custom issue submission flow (forms, markdown templates,
contact-link routing) before filing a new issue. GitHub URLs are also handled
automatically by {fetch_direct} — this tool is for structured queries by
owner/repo/number.

Actions: search_issues, search_repos, search_code, issue, pull_request, file, repo, tree, issue_templates.

Query formats vary by action:
- search_issues/search_code: GitHub search query with qualifiers (repo:, is:, label:, language:, path:)
- search_repos: GitHub search query with qualifiers (topic:, stars:, language:, forks:, license:)
- issue/pull_request: "owner/repo#number" (e.g. "pallets/flask#5618")
- file/tree: "owner/repo/path" (e.g. "pallets/flask/src/flask/app.py") — use ref= for branch/tag
- repo: "owner/repo" (e.g. "pallets/flask")
- issue_templates: "owner/repo" (e.g. "pallets/flask") — call before filing an issue if the repo action's frontmatter hints at custom submission flow

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

For repository details (README, issues, code), use {fetch_direct} or {github_tool}.""",

    "discourse": """Search and browse Discourse forum topics.

Use this for Discourse forum lookups: fetch a topic with all posts, search
a forum, or browse recent topics. Discourse URLs are also detected
automatically by {fetch_direct} via response headers — this tool is for
structured queries when you know the forum's base URL.

Actions: topic, search, latest.

Query formats:
- topic: full topic URL (e.g. 'https://meta.discourse.org/t/topic-slug/12345')
- search: search query string (requires base_url)
- latest: ignored (requires base_url to identify the forum)

The base_url parameter identifies which Discourse instance to query
(e.g. 'https://meta.discourse.org'). For the topic action, base_url
is inferred from the URL if not provided.

No authentication required for public forums.""",

    "research_shelf": """Manage the research shelf — an in-memory tracker for papers inspected during research.

Papers are automatically added when the following tools resolve a paper,
RFC, or citable repository: {arxiv_tool}, {semantic_scholar_tool}, {ietf_tool},
{github_tool} (for repos with CITATION.cff), and {fetch_direct} (via its DOI
fast path). Use this tool to review, score, confirm, or remove tracked
entries, and to export citations in BibTeX, RIS, or JSON format.

Actions: list, confirm, remove, score, note, export, import, clear.

Query formats:
- list: section name (active, retracted, all) — default active
- confirm/note: DOI of the paper (note takes DOI + space + note text)
- remove: comma-separated DOIs
- score: DOI + space + integer (e.g. "10.1234/foo 8")
- export: format name (bibtex, ris, json), optionally "with_retracted"
  (e.g. "bibtex with_retracted")
- import: JSON export string (merges with current shelf)
- clear: ignored

The shelf survives context compaction within the same session. For cross-session
persistence, use export json to save the shelf to a memory file, then import
it in a future session.""",

    "mediawiki": """Search and retrieve content from Wikipedia and other MediaWiki sites.

Use this for direct Wikipedia access without resorting to {web_search} with site: filters.
Fetches articles by title (no URL guessing), runs native full-text wiki search, and
resolves footnotes/inline citations on a specific article. Wikipedia URLs are also
handled automatically by {fetch_direct}.

Actions: page, search, references.

PARAMETER SPLIT: unlike other dedicated tools, this one uses two primary parameters:
- title= for 'page' and 'references' (article identifier: title or URL)
- query= for 'search' only (search terms)
The dispatcher will reject mismatches with a specific error.

Query formats:
- page: title (e.g. "Gödel's incompleteness theorems") or full Wikipedia URL.
  Supports section=, search= (within-page BM25), and slices= for targeted extraction.
- search: keywords (e.g. "quantum entanglement"). Supports MediaWiki search operators.
- references: title identifying the page; supply footnotes=[1,2] and/or
  citations=["#CITEREFFoo2005"] to resolve numbered footnotes and/or inline
  author-date citations. Both can be passed in one call.

{search_grammar}

Wiki instance via wiki= parameter:
- Language code: "en" (default), "de", "simple", "zh-yue", "pt-br"
- Sister project: "commons", "wikidata", "meta", "species"
- Hostname/URL: "en.wikipedia.org", "https://wiki.archlinux.org"
- Ignored when title= is a full URL (URL wins)""",
}


def _build_description(tool_name: str, profile: str) -> str:
    """Build a tool description by resolving placeholders for the given profile."""
    return TOOL_DESCRIPTIONS[tool_name].format(
        **PROFILE_VARS[profile],
        search_grammar=SEARCH_GRAMMAR_DOC,
    )


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

    # Conditionally enrich descriptions when S2 is opted in
    _s2_on = s2_enabled()
    if _s2_on:
        TOOL_DESCRIPTIONS["arxiv"] += (
            "\n\nFor citation counts and cross-references, use SemanticScholar with\n"
            "ARXIV:<id> after retrieving the arXiv ID."
        )
        TOOL_DESCRIPTIONS["research_shelf"] = TOOL_DESCRIPTIONS["research_shelf"].replace(
            "ArXiv, DOI, or IETF",
            "ArXiv, SemanticScholar, DOI, or IETF",
        )

    # Register all tools with profile-specific names and descriptions
    tools: list[tuple[str, Callable[..., Any]]] = [
        ("search", search),
        ("web_fetch_sections", web_fetch_sections),
        ("web_fetch_direct", web_fetch_direct),
        ("web_fetch_js", web_fetch_js),
        ("summarize", summarize),
        ("arxiv", arxiv),
        ("research_shelf", research_shelf),
        ("github", github),
        ("ietf", ietf),
        ("packages", packages),
        ("discourse", discourse),
        ("mediawiki", mediawiki),
    ]
    if _s2_on:
        from .semantic_scholar import semantic_scholar
        tools.append(("semantic_scholar", semantic_scholar))
    for internal_name, func in tools:
        name = TOOL_NAMES[internal_name][args.profile]
        # Title is the canonical PascalCase form regardless of active profile
        # — clients display this in tool pickers (Anthropic Software Directory
        # Policy 5.E effectively requires it).
        title = TOOL_NAMES[internal_name]["code"]
        desc = _build_description(internal_name, args.profile)
        icons = _load_tool_icon(internal_name)
        if internal_name == "research_shelf":
            annotations = ToolAnnotations(destructiveHint=True)
        else:
            annotations = ToolAnnotations(readOnlyHint=True)
        mcp.add_tool(func, name=name, title=title, description=desc,
                     icons=icons, annotations=annotations)

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
