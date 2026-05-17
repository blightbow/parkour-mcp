"""Headless-browser rendering for the WebFetchIncisive ``requires_js`` path.

``_render_js`` is the generic browser-render fallback invoked by
``web_fetch_direct`` when the caller sets ``requires_js`` or supplies an
``actions`` chain.  It is not a registered tool: the caller owns fragment
resolution, the SSRF check, parameter validation, and the API-backed fast
paths, so this module handles only what genuinely needs a browser.
"""

import logging
import os
from pathlib import Path
from typing import Optional, Union

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .common import _FETCH_HEADERS, _classify_content_type, guarded_fetch, ResponseTooLarge
from .markdown import (
    FMEntries,
    html_to_markdown, _build_frontmatter, _apply_hard_truncation,
    _fence_content, _TRUST_ADVISORY,
)
from ._pipeline import (
    _discourse_fast_path, _process_markdown_sections, _dispatch_slicing,
)

# Per-operation budget for navigation, actions, and selector waits.  Not
# caller-tunable: an agent has no basis to pick a millisecond value, and a
# page that needs more than this is pathological.
_PLAYWRIGHT_TIMEOUT_MS = 30000

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


async def _extract_interactive_elements(page, max_elements: int = 25) -> tuple[list[dict], int]:
    """Extract interactive elements from page for ReAct chaining.

    Returns:
        Tuple of ``(elements_list, total_count)`` where ``elements_list``
        is truncated to ``max_elements`` and ``total_count`` is the number
        of elements discovered before truncation. Callers should compare
        the two to detect truncation and surface a hint to the agent.
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

    return elements[:max_elements], len(elements)


async def _render_js(
    url: str,
    source_url: str,
    fragment_warning: Optional[str],
    section_names: Optional[list[str]],
    want_slicing: bool,
    search: Optional[str],
    slices: Optional[Union[int, list[int]]],
    slices_list: list[int],
    *,
    max_tokens: int,
    actions: Optional[list],
    max_elements: int,
    premature: bool = False,
) -> str:
    """Render *url* through a headless browser and run the shared pipeline.

    Handles the three steps that genuinely need a browser: a content-type
    precheck (non-HTML is returned raw, no browser), the headless render
    with optional ReAct ``actions``, and the section/slice pipeline.

    ``max_elements`` caps the interactive-element appendix; ``0`` omits it.
    ``premature`` is set by the caller when the agent reached for a render
    without evidence the page needs one; it emits a one-time teaching tip.
    """
    timeout = _PLAYWRIGHT_TIMEOUT_MS

    # --- Content-type pre-check (skip browser for non-HTML) ---
    if not actions:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
                head_resp = await client.head(url, headers=_FETCH_HEADERS)
                head_resp.raise_for_status()

                # Discourse detection — avoid launching Playwright
                try:
                    from .discourse import _detect_discourse_headers
                    if _detect_discourse_headers(head_resp.headers):
                        result = await _discourse_fast_path(url, head_resp.headers, max_tokens)
                        if result is not None:
                            if want_slicing:
                                return _dispatch_slicing(
                                    url, search, slices,
                                    slices_list if slices is not None else [],
                                    max_tokens, source_url, warning=fragment_warning,
                                    fallback=result,
                                )
                            return result
                except Exception:
                    pass

                ct = head_resp.headers.get("content-type", "")
                content_kind = _classify_content_type(ct)

                if content_kind is not None and content_kind != "html":
                    # Route the body fetch through guarded_fetch so a raw
                    # JSON/XML payload gets the same size cap and wall-clock
                    # deadline the static web_fetch_direct path applies.
                    # web_fetch_direct runs check_url_ssrf before dispatching
                    # here, so url is already SSRF-validated.
                    try:
                        get_resp = await guarded_fetch(  # nosemgrep: ssrf-check-precedes-outbound-fetch
                            url, headers=_FETCH_HEADERS,
                        )
                    except ResponseTooLarge as e:
                        return f"Error: Response too large for {url} — {e}"
                    get_resp.raise_for_status()
                    text = get_resp.text.strip()
                    if not text:
                        return f"Error: No content extracted from {url}"

                    title = url.rsplit("/", 1)[-1] or "Untitled"
                    skip_warning = f"Content-type is {content_kind}; JavaScript rendering was skipped"

                    text, truncation_hint = _apply_hard_truncation(
                        text, max_tokens,
                        hint_prefix="Full content",
                        hint_suffix="Use max_tokens to adjust.",
                    )

                    warnings = [skip_warning, fragment_warning] if fragment_warning else skip_warning
                    fm = _build_frontmatter({
                        "source": source_url,
                        "trust": _TRUST_ADVISORY,
                        "warning": warnings,
                        "content_type": content_kind,
                        "truncated": truncation_hint,
                    })
                    return fm + "\n\n" + _fence_content(text, title=title)
        except Exception:
            pass  # HEAD failed or ambiguous — fall through to Playwright

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

            # Block cross-origin navigations after initial load to prevent
            # JS redirects from steering the browser to internal services.
            from urllib.parse import urlparse as _urlparse
            _initial_host = _urlparse(url).hostname

            async def _block_cross_origin_nav(route):
                if (route.request.is_navigation_request()
                        and _urlparse(route.request.url).hostname != _initial_host):
                    logger.debug(
                        "Blocked cross-origin navigation: %s -> %s",
                        _initial_host, route.request.url,
                    )
                    await route.abort("blockedbyclient")
                else:
                    await route.continue_()

            await page.route("**/*", _block_cross_origin_nav)

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

            # Extract title
            title = await page.title() or "Untitled"

            # Get rendered HTML from main page (cap at 10MB to prevent OOM)
            html = await page.content()
            _MAX_HTML_BYTES = 10 * 1024 * 1024
            if len(html) > _MAX_HTML_BYTES:
                html = html[:_MAX_HTML_BYTES]
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

            # Extract interactive elements for ReAct chaining (max_elements=0 omits)
            interactive_elements: list[dict] = []
            elements_total = 0
            if max_elements > 0:
                interactive_elements, elements_total = await _extract_interactive_elements(
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
    frontmatter_entries = FMEntries({
        "source": source_url,
        "warning": fragment_warning,
        "browser": browser_name,
        "detected_app": detected_app or None,
        "iframe_source": iframe_source or None,
    })
    if premature:
        frontmatter_entries.set_tip("incisive_premature_playwright")
    output = _process_markdown_sections(
        markdown_content, section_names, max_tokens, frontmatter_entries,
        title=title, cache_url=url, renderer="js",
    )

    # If search/slices was requested, cache is now populated — dispatch
    if want_slicing:
        return _dispatch_slicing(
            url, search, slices, slices_list if slices is not None else [],
            max_tokens, source_url, warning=fragment_warning,
            fallback=output,
        )

    if interactive_elements:
        elem_lines = ["## Interactive Elements (for follow-up actions)\n"]
        if elements_total > len(interactive_elements):
            elem_lines.append(
                f"_Showing {len(interactive_elements)} of {elements_total} "
                f"elements — raise max_elements to see more._\n"
            )
        for elem in interactive_elements:
            elem_lines.append(f"- **{elem['type']}**: `{elem['selector']}`")
            if elem.get('options'):
                elem_lines.append(f"  Options: {', '.join(elem['options'][:10])}")
            if elem.get('placeholder'):
                elem_lines.append(f"  Placeholder: {elem['placeholder']}")
            if elem.get('label'):
                elem_lines.append(f"  Label: {elem['label']}")
            if elem.get('href'):
                elem_lines.append(f"  Href: {elem['href']}")
        output += "\n" + _fence_content("\n".join(elem_lines))

    return output
