"""Claude Web Tools MCP Server - Web browsing and content extraction tools for Claude."""

import argparse
import logging

from mcp.server.fastmcp import FastMCP

from .kagi import search, summarize
from .fetch_js import web_fetch_js
from .fetch_direct import web_fetch_direct

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("claude-web-tools")

# Profile-specific tool names to match Claude client conventions
# code profile: PascalCase (WebSearch, WebFetch, WebFetchJS)
# desktop profile: snake_case (web_search, web_fetch, web_fetch_js)
TOOL_NAMES = {
    "search": {"code": "KagiSearch", "desktop": "kagi_search"},
    "summarize": {"code": "KagiSummarize", "desktop": "kagi_summarize"},
    "web_fetch_js": {"code": "WebFetchJS", "desktop": "web_fetch_js"},
}

# Profile-specific tool descriptions for different Claude clients
TOOL_DESCRIPTIONS = {
    "search": {
        "code": """Search the web using Kagi's curated search index.

Use this as an alternative to the built-in WebSearch tool when WebSearch
returns few or poor quality results. Kagi's index is independently curated,
resistant to SEO spam, and may surface different sources. Returns raw search
results with snippets and timestamps, plus related search suggestions.""",
        "desktop": """Search the web using Kagi's curated search index.

Use this as an alternative to the built-in web_search tool when web_search
returns few or poor quality results. Kagi's index is independently curated,
resistant to SEO spam, and may surface different sources. Returns raw search
results with snippets and timestamps, plus related search suggestions.""",
    },
    "summarize": {
        "code": """Summarize content from a URL or text using Kagi's Universal Summarizer.

Supports web pages, PDFs, YouTube videos, audio files, and documents.
Use this when WebFetch fails due to agent blacklisting or access restrictions.""",
        "desktop": """Summarize content from a URL or text using Kagi's Universal Summarizer.

Supports web pages, PDFs, YouTube videos, audio files, and documents.
Use this when web_fetch fails due to agent blacklisting or access restrictions.""",
    },
    "web_fetch_js": {
        "code": """Fetch and interact with web content using a headless browser.

Use this when WebFetch returns incomplete content from JavaScript-heavy sites
(SPAs, React/Vue/Angular apps, dynamically loaded content). For MediaWiki sites
(URLs containing /wiki/), content is fetched directly via the API without
launching a browser. Use the section parameter to extract specific sections
by heading name.

Supports ReAct-style interaction chains:
1. First call: Fetch page, observe available interactive elements
2. Subsequent calls: Use 'actions' parameter to interact (click, fill, select)
3. Extract updated content after interactions

Actions format (JSON array of objects):
- {"action": "click", "selector": "button#submit"}
- {"action": "fill", "selector": "input[name=query]", "value": "search term"}
- {"action": "select", "selector": "select#region", "value": "us-east"}
- {"action": "wait", "selector": ".results-loaded"}

Returns markdown with interactive elements annotated for follow-up actions.""",
        "desktop": """Fetch and interact with web content using a headless browser.

Use this when web_fetch returns incomplete content from JavaScript-heavy sites
(SPAs, React/Vue/Angular apps, dynamically loaded content). For MediaWiki sites
(URLs containing /wiki/), content is fetched directly via the API without
launching a browser. Use the section parameter to extract specific sections
by heading name.

Supports ReAct-style interaction chains:
1. First call: Fetch page, observe available interactive elements
2. Subsequent calls: Use 'actions' parameter to interact (click, fill, select)
3. Extract updated content after interactions

Actions format (JSON array of objects):
- {"action": "click", "selector": "button#submit"}
- {"action": "fill", "selector": "input[name=query]", "value": "search term"}
- {"action": "select", "selector": "select#region", "value": "us-east"}
- {"action": "wait", "selector": ".results-loaded"}

Returns markdown with interactive elements annotated for follow-up actions.""",
    },
    "web_fetch_direct": {
        "desktop": """Fetch raw content from a URL without JavaScript rendering.

Returns markdown by default. For MediaWiki sites (URLs containing /wiki/),
content is fetched directly via the API. Use the section parameter to extract
specific sections by heading name. Use cite=True for legacy XML format with
span indices for citation.

Supports HTML, plain text, JSON, and XML content types.""",
    },
}


def apply_profile(profile: str) -> None:
    """Apply tool descriptions for the specified profile.

    Note: Uses _tool_manager, a private FastMCP API.
    """
    for tool_name, descriptions in TOOL_DESCRIPTIONS.items():
        # Skip tools with profile-specific names (registered separately in main)
        if tool_name in TOOL_NAMES:
            continue
        tool = mcp._tool_manager.get_tool(tool_name)
        if tool:
            tool.description = descriptions[profile]


def main():
    """Run the MCP server."""
    parser = argparse.ArgumentParser(description="Claude Web Tools MCP Server")
    parser.add_argument(
        "--profile",
        choices=["code", "desktop"],
        default="desktop",
        help="Target client profile (default: desktop)",
    )
    args = parser.parse_args()

    # Register tools with profile-specific names
    tools = [
        ("search", search),
        ("summarize", summarize),
        ("web_fetch_js", web_fetch_js),
    ]
    for internal_name, func in tools:
        name = TOOL_NAMES[internal_name][args.profile]
        desc = TOOL_DESCRIPTIONS[internal_name][args.profile]
        mcp.add_tool(func, name=name, description=desc)

    # Register desktop-only tools
    if args.profile == "desktop":
        desc = TOOL_DESCRIPTIONS["web_fetch_direct"]["desktop"]
        mcp.add_tool(web_fetch_direct, description=desc)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
