"""Claude Web Tools MCP Server - Web browsing and content extraction tools for Claude."""

import argparse
import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

import httpx
from bs4 import BeautifulSoup
from kagiapi import KagiClient
from markdownify import MarkdownConverter
from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright


class TextOnlyConverter(MarkdownConverter):
    """Custom converter that preserves link hrefs but strips non-text content like images."""

    def convert_img(self, el, text, parent_tags):
        # Images can't render as text - return alt text only if meaningful
        alt = el.get('alt', '').strip()
        return f'[Image: {alt}]' if alt else ''

    def convert_a(self, el, text, parent_tags):
        # If the link only contains an image reference, drop it entirely
        stripped = text.strip()
        if stripped.startswith('[Image:') or not stripped:
            return stripped
        # Otherwise use default link conversion
        return super().convert_a(el, text, parent_tags)


def md(html, **options):
    """Convert HTML to markdown using custom converter."""
    return TextOnlyConverter(**options).convert(html)


# DOM-based detection for apps with persistent connections (WebSocket/SSE)
# These apps never reach "networkidle" state, so we detect and use accelerated loading
LIVE_APP_MARKERS = [
    {"detect": "gradio-app, .gradio-container", "ready": ".gradio-container", "name": "Gradio"},
    {"detect": "[data-testid='stAppViewContainer']", "ready": ".stApp", "name": "Streamlit"},
]


def _detect_playwright_browser(playwright) -> tuple[str, str]:
    """Detect available Playwright browser with graceful fallback.

    Uses Playwright's own executable_path API to check browser availability.

    Priority:
    1. PLAYWRIGHT_BROWSER env var override (webkit, chromium, firefox)
    2. If only one browser installed, use it
    3. If multiple available, prefer by footprint: webkit > firefox > chromium
    4. Error if no browser found

    Args:
        playwright: The Playwright instance from async_playwright()

    Returns:
        Tuple of (browser_type, display_name) e.g. ("webkit", "WebKit")
    """
    browser_info = {
        "webkit": ("WebKit", playwright.webkit),
        "chromium": ("Chromium", playwright.chromium),
        "firefox": ("Firefox", playwright.firefox),
    }

    # Check for override
    override = os.environ.get("PLAYWRIGHT_BROWSER", "").lower()
    if override in browser_info:
        return (override, browser_info[override][0])

    # Detect installed browsers by checking executable paths
    available = []
    for name, (display, browser_type) in browser_info.items():
        if Path(browser_type.executable_path).exists():
            available.append(name)

    if not available:
        return ("none", "None")

    if len(available) == 1:
        return (available[0], browser_info[available[0]][0])

    # Multiple browsers available - prefer by footprint: webkit > firefox > chromium
    for preferred in ("webkit", "firefox", "chromium"):
        if preferred in available:
            return (preferred, browser_info[preferred][0])

    return ("none", "None")


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("claude-web-tools")

CONFIG_PATH = Path.home() / ".config" / "kagi" / "api_key"

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
        "code": """Fetch and interact with web content using a headless WebKit browser.

Use this when WebFetch returns incomplete content from JavaScript-heavy sites
(SPAs, React/Vue/Angular apps, dynamically loaded content).

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
        "desktop": """Fetch and interact with web content using a headless WebKit browser.

Use this when web_fetch returns incomplete content from JavaScript-heavy sites
(SPAs, React/Vue/Angular apps, dynamically loaded content).

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
    # web_fetch_direct is desktop-only (registered conditionally in main)
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


def get_api_key() -> str:
    """Load API key from config file or environment."""
    # Environment variable takes precedence
    if key := os.environ.get("KAGI_API_KEY"):
        return key
    # Fall back to config file
    if CONFIG_PATH.exists():
        return CONFIG_PATH.read_text().strip()
    return ""


def get_client() -> Optional[KagiClient]:
    """Create a Kagi client with the configured API key."""
    api_key = get_api_key()
    if not api_key:
        return None
    return KagiClient(api_key=api_key)


async def search(query: str, limit: int = 5) -> str:
    """Search the web using Kagi's curated search index.

    Use this as an alternative to the built-in WebSearch tool when WebSearch
    returns few or poor quality results. Kagi's index is independently curated,
    resistant to SEO spam, and may surface different sources. Returns raw search
    results with snippets and timestamps, plus related search suggestions.

    Args:
        query: The search query
        limit: Maximum number of results to return (default 5)
    """
    client = get_client()
    if not client:
        return "Error: API key not found. Create ~/.config/kagi/api_key or set KAGI_API_KEY env var."

    try:
        response = client.search(query, limit=limit)
    except Exception as e:
        logger.exception("Error during search")
        error_msg = str(e)
        if "401" in error_msg or "Unauthorized" in error_msg:
            return "Error: Invalid API key. Check ~/.config/kagi/api_key or KAGI_API_KEY env var."
        if "402" in error_msg:
            return "Error: Insufficient API credits. Add funds at https://kagi.com/settings/billing"
        return f"Error: {error_msg}"

    # Parse results
    results = []
    related_searches = []

    for item in response.get("data", []):
        item_type = item.get("t")

        if item_type == 0:  # Search result
            title = item.get("title", "Untitled")
            item_url = item.get("url", "")
            snippet = item.get("snippet", "")
            published = item.get("published")

            # Format as markdown
            if published:
                results.append(f"[{title}]({item_url}) - {snippet} ({published})")
            else:
                results.append(f"[{title}]({item_url}) - {snippet}")

        elif item_type == 1:  # Related searches
            related_searches = item.get("list", [])

    # Build output
    output_parts = []

    if results:
        output_parts.append("Results:")
        for i, result in enumerate(results, 1):
            output_parts.append(f"{i}. {result}")
    else:
        output_parts.append("No results found.")

    if related_searches:
        output_parts.append("")
        output_parts.append(f"Related searches: {', '.join(related_searches)}")

    return "\n".join(output_parts)


async def summarize(
    url: Optional[str] = None,
    text: Optional[str] = None,
    summary_type: str = "summary"
) -> str:
    """Summarize content from a URL or text using Kagi's Universal Summarizer.

    Supports web pages, PDFs, YouTube videos, audio files, and documents.
    Use this when WebFetch fails due to agent blacklisting or access restrictions.

    Args:
        url: URL to summarize (PDFs, YouTube, articles, audio)
        text: Raw text to summarize (alternative to url)
        summary_type: Output format - "summary" for prose, "takeaway" for bullet points
    """
    client = get_client()
    if not client:
        return "Error: API key not found. Create ~/.config/kagi/api_key or set KAGI_API_KEY env var."

    if not url and not text:
        return "Error: Either 'url' or 'text' must be provided."

    if url and text:
        return "Error: Provide either 'url' or 'text', not both."

    if summary_type not in ("summary", "takeaway"):
        return "Error: summary_type must be 'summary' or 'takeaway'."

    try:
        if url:
            response = client.summarize(url=url, summary_type=summary_type, target_language="EN")
        else:
            response = client.summarize(text=text, summary_type=summary_type, target_language="EN")
    except Exception as e:
        logger.exception("Error during summarization")
        error_msg = str(e)
        if "401" in error_msg or "Unauthorized" in error_msg:
            return "Error: Invalid API key. Check ~/.config/kagi/api_key or KAGI_API_KEY env var."
        if "402" in error_msg:
            return "Error: Insufficient API credits. Add funds at https://kagi.com/settings/billing"
        return f"Error: {error_msg}"

    # Extract summary
    output = response.get("data", {}).get("output", "")

    if not output:
        return "Error: No summary returned from API."

    return output


# Default headers for web_fetch_direct
_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _extract_text_spans(soup: BeautifulSoup) -> list[str]:
    """Extract text content from HTML, split into spans by block elements."""
    # Remove script and style elements
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    # Find main content area if possible
    main = soup.find("main") or soup.find("article") or soup.find("body") or soup

    spans = []
    # Block elements that should create span boundaries
    block_tags = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
                  "li", "td", "th", "blockquote", "pre", "figcaption"}

    for element in main.find_all(block_tags):
        text = element.get_text(separator=" ", strip=True)
        # Clean up whitespace
        text = re.sub(r"\s+", " ", text).strip()
        if text and len(text) > 20:  # Skip very short fragments
            spans.append(text)

    # Deduplicate while preserving order (nested elements can cause duplicates)
    seen = set()
    unique_spans = []
    for span in spans:
        if span not in seen:
            seen.add(span)
            unique_spans.append(span)

    return unique_spans


def _build_document_xml(
    title: str,
    spans: list[str],
    url: str,
    mime_type: str,
    doc_index: int = 1
) -> str:
    """Build XML document structure matching Claude Desktop's format."""
    lines = [f'<document index="{doc_index}">']
    lines.append(f"  <source>{xml_escape(title)}</source>")
    lines.append("  <document_content>")

    for i, span_text in enumerate(spans, 1):
        span_index = f"{doc_index}-{i}"
        lines.append(f'    <span index="{span_index}">{xml_escape(span_text)}</span>')

    lines.append("  </document_content>")
    lines.append('  <metadata key="content_type">html</metadata>')
    lines.append(f'  <metadata key="destination_url">{xml_escape(url)}</metadata>')
    lines.append(f'  <metadata key="mime_type">{xml_escape(mime_type)}</metadata>')
    lines.append("</document>")

    return "\n".join(lines)


# Helper functions for web_fetch_js interactive element extraction
async def _get_unique_selector(element) -> str:
    """Generate a unique CSS selector for an element."""
    # Try id first
    elem_id = await element.get_attribute("id")
    if elem_id:
        return f"#{elem_id}"

    # Try name attribute
    name = await element.get_attribute("name")
    tag = await element.evaluate("el => el.tagName.toLowerCase()")
    if name:
        return f"{tag}[name='{name}']"

    # Fall back to data-testid or other common attributes
    testid = await element.get_attribute("data-testid")
    if testid:
        return f"[data-testid='{testid}']"

    # Last resort: tag + class combination
    classes = await element.get_attribute("class")
    if classes:
        primary_class = classes.split()[0]
        return f"{tag}.{primary_class}"

    return tag


async def _get_label_for_element(page, element) -> Optional[str]:
    """Find associated label for form element."""
    elem_id = await element.get_attribute("id")
    if elem_id:
        label = await page.query_selector(f"label[for='{elem_id}']")
        if label:
            return await label.inner_text()
    return None


async def _extract_interactive_elements(page, max_elements: int = 25) -> tuple[list[dict], bool]:
    """Extract interactive elements from page for ReAct chaining.

    Returns:
        Tuple of (elements list, was_truncated bool)
    """
    elements = []

    # Extract select/dropdown elements
    selects = await page.query_selector_all("select")
    for sel in selects:
        if not await sel.is_visible():
            continue
        selector = await _get_unique_selector(sel)
        options = await sel.evaluate(
            "el => Array.from(el.options).map(o => o.text || o.value)"
        )
        label = await _get_label_for_element(page, sel)
        elements.append({
            "type": "select",
            "selector": selector,
            "options": options,
            "label": label
        })

    # Extract input fields
    inputs = await page.query_selector_all("input:not([type=hidden])")
    for inp in inputs:
        if not await inp.is_visible():
            continue
        input_type = await inp.get_attribute("type") or "text"
        if input_type in ("text", "search", "email", "number", "tel", "url"):
            selector = await _get_unique_selector(inp)
            placeholder = await inp.get_attribute("placeholder")
            label = await _get_label_for_element(page, inp)
            elements.append({
                "type": f"input[{input_type}]",
                "selector": selector,
                "placeholder": placeholder,
                "label": label
            })

    # Extract buttons
    buttons = await page.query_selector_all("button, input[type=submit]")
    for btn in buttons:
        if not await btn.is_visible():
            continue
        try:
            text = await btn.inner_text()
        except Exception:
            text = await btn.get_attribute("value")
        if text and text.strip():
            selector = await _get_unique_selector(btn)
            elements.append({
                "type": "button",
                "selector": selector,
                "label": text.strip()[:50]
            })

    # Extract navigation links (for tab/menu navigation)
    nav_links = await page.query_selector_all("nav a, [role=navigation] a, .nav a, .tabs a, .menu a")
    for link in nav_links:
        if not await link.is_visible():
            continue
        try:
            text = await link.inner_text()
            href = await link.get_attribute("href")
            if text and text.strip() and href:
                selector = await _get_unique_selector(link)
                elements.append({
                    "type": "link",
                    "selector": selector,
                    "label": text.strip()[:50],
                    "href": href
                })
        except Exception:
            pass

    # Check if we hit the limit
    was_truncated = len(elements) > max_elements
    return elements[:max_elements], was_truncated


# Note: web_fetch_js is registered in main() with profile-specific naming
async def web_fetch_js(
    url: str,
    actions: Optional[list] = None,
    wait_for: Optional[str] = None,
    timeout: int = 30000,
    include_interactive: bool = True,
    max_elements: int = 25,
    max_tokens: int = 5000
) -> str:
    """Fetch web content with full JavaScript rendering and optional interactions.

    Args:
        url: The URL to fetch
        actions: List of interaction actions to perform before extraction.
                 Each action: {"action": "click"|"fill"|"select"|"wait",
                              "selector": "CSS selector", "value": "optional value"}
        wait_for: CSS selector to wait for before extracting content
        timeout: Max wait time in milliseconds (default 30000)
        include_interactive: If True, annotate interactive elements in output (default True)
        max_elements: Maximum number of interactive elements to extract (default 25)
        max_tokens: Limit on output length in approximate token count (default 5000)
    """
    detected_app = None  # Track if we detected a live app framework

    try:
        async with async_playwright() as p:
            # Detect available browser engine
            browser_type, browser_name = _detect_playwright_browser(p)
            if browser_type == "none":
                return (
                    "Error: No Playwright browser installed. Run one of:\n"
                    "  playwright install webkit\n"
                    "  playwright install chromium\n\n"
                    "Or set PLAYWRIGHT_BROWSER env var to specify a browser."
                )

            # Launch detected/configured browser
            browser_launcher = getattr(p, browser_type)
            browser = await browser_launcher.launch(headless=True)
            context = await browser.new_context(
                user_agent=_FETCH_HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 720}
            )
            page = await context.new_page()

            # Wait for load event - gives JS time to create framework elements
            await page.goto(url, wait_until="load", timeout=timeout)

            # Check for live app frameworks that use persistent connections
            for marker in LIVE_APP_MARKERS:
                element = await page.query_selector(marker["detect"])
                if element:
                    detected_app = marker["name"]
                    # Wait for app-specific ready selector instead of networkidle
                    await page.wait_for_selector(marker["ready"], timeout=timeout)
                    break

            # If no live app detected, try networkidle with short timeout
            # Some sites have persistent connections that prevent networkidle
            if not detected_app:
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass  # Proceed with content extraction anyway

            # Execute actions if provided
            if actions:
                for act in actions:
                    action_type = act.get("action")
                    selector = act.get("selector")
                    value = act.get("value", "")

                    if action_type == "click":
                        await page.click(selector, timeout=timeout)
                    elif action_type == "fill":
                        await page.fill(selector, value, timeout=timeout)
                    elif action_type == "select":
                        await page.select_option(selector, value, timeout=timeout)
                    elif action_type == "wait":
                        await page.wait_for_selector(selector, timeout=timeout)

                    # Brief pause for UI to update
                    try:
                        await page.wait_for_load_state("networkidle", timeout=2000)
                    except Exception:
                        pass  # Proceed anyway

            # Optional: wait for specific element
            if wait_for:
                await page.wait_for_selector(wait_for, timeout=timeout)

            # Extract title
            title = await page.title() or "Untitled"

            # Get rendered HTML from main page
            html = await page.content()
            iframe_source = None  # Track if we extracted from iframe

            # Check if main content is sparse but iframe exists
            # This handles HuggingFace Spaces, embedded Gradio apps, etc.
            main_text_length = len(BeautifulSoup(html, "html.parser").get_text(strip=True))

            if main_text_length < 500:  # Sparse main content
                # Look for content-bearing iframes
                frames = page.frames
                for frame in frames:
                    if frame == page.main_frame:
                        continue
                    try:
                        frame_html = await frame.content()
                        frame_text_length = len(BeautifulSoup(frame_html, "html.parser").get_text(strip=True))

                        # Use iframe content if it's more substantial
                        if frame_text_length > main_text_length:
                            html = frame_html
                            iframe_source = frame.url
                            # Re-check for framework markers in iframe
                            for marker in LIVE_APP_MARKERS:
                                element = await frame.query_selector(marker["detect"])
                                if element:
                                    detected_app = marker["name"]
                                    break
                            break
                    except Exception:
                        # Cross-origin or other access issue - try next frame
                        continue

            # Extract interactive elements for ReAct chaining
            interactive_elements = []
            elements_truncated = False
            if include_interactive:
                interactive_elements, elements_truncated = await _extract_interactive_elements(
                    page, max_elements
                )

            await browser.close()

    except Exception as e:
        return f"Error: Failed to render page - {type(e).__name__}: {e}"

    # Parse and clean HTML
    soup = BeautifulSoup(html, "html.parser")

    # Remove noise elements
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript"]):
        tag.decompose()

    # Find main content
    main = soup.find("main") or soup.find("article") or soup.find("body") or soup

    # Convert to markdown (preserve anchor hrefs for navigation)
    markdown_content = md(str(main), heading_style="ATX")
    markdown_content = re.sub(r'\n{3,}', '\n\n', markdown_content).strip()

    # Apply token limit and generate truncation hint
    truncation_hint = None
    total_chars = len(markdown_content)
    char_limit = max_tokens * 4
    if total_chars > char_limit:
        total_kb = total_chars / 1024
        total_tokens_est = total_chars // 4
        markdown_content = markdown_content[:char_limit] + "\n\n[content truncated]"
        truncation_hint = (
            f"Full page is {total_kb:.1f} KB (~{total_tokens_est:,} tokens), "
            f"showing first ~{max_tokens:,} tokens. "
            f"Use max_tokens to adjust, or kagi_summarize for a summary of large pages."
        )

    # Build output with YAML frontmatter hints
    output_parts = ["---"]
    output_parts.append(f"title: {title}")
    output_parts.append(f"source: {url}")
    output_parts.append(f"browser: {browser_name}")
    if detected_app:
        output_parts.append(f"detected_app: {detected_app}")
    if iframe_source:
        output_parts.append(f"iframe_source: {iframe_source}")
    if truncation_hint:
        output_parts.append(f"truncated: {truncation_hint}")
    output_parts.append("---")

    output_parts.extend(["\n", markdown_content])

    if interactive_elements:
        output_parts.append("\n---\n")
        output_parts.append("## Interactive Elements (for follow-up actions)\n")
        for elem in interactive_elements:
            output_parts.append(f"- **{elem['type']}**: `{elem['selector']}`")
            if elem.get('options'):
                output_parts.append(f"  Options: {', '.join(elem['options'][:10])}")
            if elem.get('placeholder'):
                output_parts.append(f"  Placeholder: {elem['placeholder']}")
            if elem.get('label'):
                output_parts.append(f"  Label: {elem['label']}")
            if elem.get('href'):
                output_parts.append(f"  Href: {elem['href']}")
        if elements_truncated:
            output_parts.append(
                f"\n*[List truncated to {max_elements} elements. "
                "Use max_elements parameter to increase limit.]*"
            )

    return "\n".join(output_parts)


# Desktop-only tool - registered conditionally in main()
async def web_fetch_direct(url: str, max_tokens: int = 5000) -> str:
    """Fetch raw content from a URL without summarization.

    Supports HTML, plain text, JSON, and XML content types.
    Use this as an alternative to web_fetch when web_fetch fails due to
    agent blacklisting or access restrictions. Returns content
    in XML document format with span indices for citation.

    Args:
        url: The URL to fetch
        max_tokens: Limit on content length in approximate token count (default 5000)
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(url, headers=_FETCH_HEADERS)
            response.raise_for_status()
    except httpx.TimeoutException:
        return f"Error: Request timed out for {url}"
    except httpx.HTTPStatusError as e:
        return f"Error: HTTP {e.response.status_code} for {url}"
    except httpx.RequestError as e:
        return f"Error: Failed to fetch {url} - {type(e).__name__}"

    # Check content type
    content_type = response.headers.get("content-type", "")
    is_html = "text/html" in content_type or "application/xhtml" in content_type
    is_plain = "text/plain" in content_type
    is_json = "application/json" in content_type or "text/json" in content_type
    is_xml = (
        "application/xml" in content_type or "text/xml" in content_type
    ) and not is_html

    if not any([is_html, is_plain, is_json, is_xml]):
        return (
            f"Error: Unsupported content type '{content_type}'. "
            f"Supported: text/html, text/plain, application/json, application/xml."
        )

    if is_plain or is_json or is_xml:
        # Return structured/plain content as single-span document
        text = response.text.strip()
        if not text:
            return f"Error: No content extracted from {url}"
        title = url.rsplit("/", 1)[-1] or "Untitled"
        if is_plain:
            spans = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
        else:
            # JSON and XML: preserve as a single span to keep structure intact
            spans = [text]
    else:
        # Parse HTML
        try:
            soup = BeautifulSoup(response.text, "html.parser")
        except Exception as e:
            return f"Error: Failed to parse HTML - {e}"

        # Extract title (prefer <title>, fallback to <h1>)
        title_tag = soup.find("title") or soup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else "Untitled"

        # Extract spans
        spans = _extract_text_spans(soup)

    if not spans:
        return f"Error: No content extracted from {url}"

    # Apply token limit (approximate: ~4 chars per token)
    total_chars = sum(len(s) for s in spans)
    char_limit = max_tokens * 4
    truncation_hint = None
    if total_chars > char_limit:
        total_kb = total_chars / 1024
        total_tokens_est = total_chars // 4
        truncation_hint = (
            f"Full page is {total_kb:.1f} KB (~{total_tokens_est:,} tokens), "
            f"showing first ~{max_tokens:,} tokens. "
            f"Use max_tokens to adjust, or kagi_summarize for a summary of large pages."
        )
        char_count = 0
        truncated_spans = []
        for span in spans:
            if char_count + len(span) > char_limit:
                truncated_spans.append("[content truncated]")
                break
            truncated_spans.append(span)
            char_count += len(span)
        spans = truncated_spans

    # Build XML output
    mime_type = content_type.split(";")[0].strip() if content_type else "text/html"
    xml_output = _build_document_xml(title, spans, str(response.url), mime_type)

    if truncation_hint:
        return f"<!-- truncated: {truncation_hint} -->\n{xml_output}"
    return xml_output


# NOTE: search_and_summarize is commented out to reduce API costs.
# It performs 1 search + N summarize calls, which adds up quickly.
# Uncomment to re-enable.
#
# @mcp.tool()
# async def search_and_summarize(
#     query: str,
#     limit: int = 3,
#     summary_type: str = "takeaway"
# ) -> str:
#     """Search the web and summarize top results for a synthesized overview.
#
#     Combines Kagi Search with the Universal Summarizer to provide deeper
#     insight than search snippets alone. Each result URL is summarized to
#     extract key points.
#
#     Args:
#         query: The search query
#         limit: Number of results to summarize (default 3, max 5)
#         summary_type: "takeaway" for bullet points (default), "summary" for prose
#     """
#     client = get_client()
#     if not client:
#         return "Error: API key not found. Create ~/.config/kagi/api_key or set KAGI_API_KEY env var."
#
#     # Cap limit to avoid excessive API usage
#     limit = min(limit, 5)
#
#     if summary_type not in ("summary", "takeaway"):
#         return "Error: summary_type must be 'summary' or 'takeaway'."
#
#     # Step 1: Perform search
#     try:
#         search_response = client.search(query, limit=limit)
#     except Exception as e:
#         logger.exception("Error during search")
#         error_msg = str(e)
#         if "401" in error_msg or "Unauthorized" in error_msg:
#             return "Error: Invalid API key. Check ~/.config/kagi/api_key or KAGI_API_KEY env var."
#         if "402" in error_msg:
#             return "Error: Insufficient API credits. Add funds at https://kagi.com/settings/billing"
#         return f"Error during search: {error_msg}"
#
#     # Parse search results
#     results = []
#     related_searches = []
#
#     for item in search_response.get("data", []):
#         item_type = item.get("t")
#
#         if item_type == 0:  # Search result
#             results.append({
#                 "title": item.get("title", "Untitled"),
#                 "url": item.get("url", ""),
#                 "snippet": item.get("snippet", ""),
#                 "published": item.get("published"),
#             })
#         elif item_type == 1:  # Related searches
#             related_searches = item.get("list", [])
#
#     if not results:
#         return "No results found."
#
#     # Step 2: Summarize each result URL in parallel
#     async def summarize_url(result: dict) -> dict:
#         """Summarize a single URL, returning result with summary."""
#         url = result["url"]
#         if not url:
#             return {**result, "summary": None, "error": "No URL"}
#
#         try:
#             response = await asyncio.to_thread(
#                 client.summarize,
#                 url=url,
#                 summary_type=summary_type,
#                 target_language="EN"
#             )
#             summary = response.get("data", {}).get("output", "")
#             return {**result, "summary": summary, "error": None}
#         except Exception as e:
#             logger.warning(f"Failed to summarize {url}: {e}")
#             return {**result, "summary": None, "error": str(e)}
#
#     summarized_results = await asyncio.gather(*[summarize_url(r) for r in results])
#
#     # Step 3: Build formatted output
#     output_parts = []
#     output_parts.append(f"# Search: {query}\n")
#
#     # Sources section
#     output_parts.append("## Sources\n")
#     for i, result in enumerate(summarized_results, 1):
#         title = result["title"]
#         url = result["url"]
#         published = result.get("published")
#         if published:
#             output_parts.append(f"{i}. [{title}]({url}) ({published})")
#         else:
#             output_parts.append(f"{i}. [{title}]({url})")
#     output_parts.append("")
#
#     # Key findings section
#     output_parts.append("## Key Findings\n")
#     for i, result in enumerate(summarized_results, 1):
#         title = result["title"]
#         summary = result.get("summary")
#         error = result.get("error")
#
#         output_parts.append(f"### {i}. {title}\n")
#
#         if summary:
#             output_parts.append(summary)
#         elif error:
#             output_parts.append(f"*Could not summarize: {error}*")
#         else:
#             # Fall back to snippet
#             snippet = result.get("snippet", "No content available.")
#             output_parts.append(f"*{snippet}*")
#
#         output_parts.append("")
#
#     # Related searches
#     if related_searches:
#         output_parts.append("## Related Searches\n")
#         output_parts.append(", ".join(related_searches))
#
#     return "\n".join(output_parts)


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
        mcp.add_tool(web_fetch_direct)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
