"""JavaScript-rendered web content fetching via Playwright."""

import logging
import os
from pathlib import Path
from typing import Optional, Union

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .common import _FETCH_HEADERS
from .markdown import html_to_markdown
from ._pipeline import (
    _extract_fragment, _normalize_sections, _resolve_fragment_source,
    _mediawiki_fast_path, _arxiv_fast_path, _s2_fast_path,
    _process_markdown_sections,
)

logger = logging.getLogger(__name__)


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
    for name, (_display, browser_type) in browser_info.items():
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


# Helper functions for interactive element extraction
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

    # Extract TOC / anchor links (in-page navigation to sections)
    toc_links = await page.query_selector_all(
        "[class*='toc'] a[href^='#'], nav a[href^='#'], "
        "[role='navigation'] a[href^='#'], .sidebar a[href^='#']"
    )
    seen_toc_hrefs: set[str] = set()
    for link in toc_links:
        if not await link.is_visible():
            continue
        try:
            text = await link.inner_text()
            href = await link.get_attribute("href")
            if text and text.strip() and href and href not in seen_toc_hrefs:
                seen_toc_hrefs.add(href)
                selector = await _get_unique_selector(link)
                elements.append({
                    "type": "link",
                    "selector": selector,
                    "label": text.strip()[:120],
                    "href": href
                })
        except Exception:
            pass

    # Extract navigation links (for tab/menu navigation, excluding TOC anchors)
    nav_links = await page.query_selector_all("nav a, [role=navigation] a, .nav a, .tabs a, .menu a")
    for link in nav_links:
        if not await link.is_visible():
            continue
        try:
            text = await link.inner_text()
            href = await link.get_attribute("href")
            if text and text.strip() and href:
                # Skip anchor links already captured as TOC links
                if href.startswith("#") and href in seen_toc_hrefs:
                    continue
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


async def web_fetch_js(
    url: str,
    actions: Optional[list] = None,
    wait_for: Optional[str] = None,
    timeout: int = 30000,
    include_interactive: bool = True,
    max_elements: int = 25,
    max_tokens: int = 5000,
    section: Optional[Union[str, list[str]]] = None,
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
        section: Section name or list of section names to extract from the page
    """
    # Extract fragment from URL (e.g. #section-name) as implicit section request
    url, fragment = _extract_fragment(url)
    section_names = _normalize_sections(section)
    if fragment and not section_names:
        section_names = [fragment]
    source_url, fragment_warning = _resolve_fragment_source(url, fragment, section)

    # --- arXiv fast path (before launching browser) ---
    try:
        result = await _arxiv_fast_path(url)
        if result is not None:
            return result
    except Exception:
        pass

    # --- Semantic Scholar fast path (before launching browser) ---
    try:
        result = await _s2_fast_path(url)
        if result is not None:
            return result
    except Exception:
        pass

    # --- MediaWiki fast path (before launching browser) ---
    try:
        result = await _mediawiki_fast_path(
            url, section_names, max_tokens,
            extra_entries={"source": source_url, "warning": fragment_warning},
        )
        if result is not None:
            return result
    except Exception:
        pass  # Fall through to browser path

    # --- Browser path ---
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
            soup = BeautifulSoup(html, "html.parser")
            main_text_length = len(soup.get_text(strip=True))

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

    # Convert HTML to markdown (reuses the soup from sparse-content check if html unchanged)
    _title, markdown_content = html_to_markdown(html)
    # Prefer the browser's title (from <title> tag + JS modifications)
    if title == "Untitled":
        title = _title

    # Section handling, truncation, and frontmatter via shared pipeline
    frontmatter_entries = {
        "title": title,
        "source": source_url,
        "warning": fragment_warning,
        "browser": browser_name,
        "detected_app": detected_app or None,
        "iframe_source": iframe_source or None,
    }
    output = _process_markdown_sections(
        markdown_content, section_names, max_tokens, frontmatter_entries,
        cache_url=url,
    )

    if interactive_elements:
        output += "\n---\n"
        output += "## Interactive Elements (for follow-up actions)\n"
        for elem in interactive_elements:
            output += f"\n- **{elem['type']}**: `{elem['selector']}`"
            if elem.get('options'):
                output += f"\n  Options: {', '.join(elem['options'][:10])}"
            if elem.get('placeholder'):
                output += f"\n  Placeholder: {elem['placeholder']}"
            if elem.get('label'):
                output += f"\n  Label: {elem['label']}"
            if elem.get('href'):
                output += f"\n  Href: {elem['href']}"
        if elements_truncated:
            output += (
                f"\n\n*[List truncated to {max_elements} elements. "
                "Use max_elements parameter to increase limit.]*"
            )

    return output
