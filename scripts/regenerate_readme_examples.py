#!/usr/bin/env python3
"""Regenerate README.md example outputs from live tool calls.

Calls each tool used in the README examples, captures the real output, and
writes labeled blocks to stdout.  Volatile fields (citation counts, category
listings, slice content) are marked so you can compare structurally without
being tripped up by data that changes daily.

Usage:
    uv run python3 scripts/regenerate_readme_examples.py

Requirements:
    - S2_API_KEY or KAGI_API_KEY env vars as needed
    - Network access to MDN, Wikipedia, arXiv, Semantic Scholar
"""

import asyncio
import logging

from kagi_research_mcp.fetch_direct import web_fetch_direct, web_fetch_sections
from kagi_research_mcp.semantic_scholar import semantic_scholar
from kagi_research_mcp.arxiv import arxiv

# ── URLs used across examples ───────────────────────────────────────────────
MDN_UA = "https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/User-Agent"
WIKI_42 = "https://en.wikipedia.org/wiki/42_(number)"
WIKI_42_FRAGMENT = f"{WIKI_42}#The_Hitchhiker%27s_Guide_to_the_Galaxy"
ARXIV_ATTN = "https://arxiv.org/abs/1706.03762"
S2_ATTN_URL = (
    "https://www.semanticscholar.org/paper/"
    "Attention-Is-All-You-Need-Vaswani-Shazeer/"
    "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
)
S2_ATTN_ID = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
HTTPBIN_JSON = "https://httpbin.org/json"


def _banner(label: str, note: str = "") -> str:
    rule = "─" * 72
    suffix = f"  ({note})" if note else ""
    return f"\n{rule}\n## {label}{suffix}\n{rule}\n"


async def main() -> None:
    results: list[tuple[str, str, str]] = []  # (label, note, output)

    # ── 1. web_fetch_sections — MDN User-Agent ──────────────────────────
    out = await web_fetch_sections(MDN_UA)
    results.append((
        'web_fetch_sections(MDN_UA)',
        'README line ~25',
        out,
    ))

    # ── 2. web_fetch_direct — MDN truncation ────────────────────────────
    out = await web_fetch_direct(MDN_UA, max_tokens=300)
    results.append((
        'web_fetch_direct(MDN_UA, max_tokens=300)',
        'README line ~45',
        out,
    ))

    # ── 3. web_fetch_direct — MDN section extraction ────────────────────
    out = await web_fetch_direct(MDN_UA, section="Syntax")
    results.append((
        'web_fetch_direct(MDN_UA, section="Syntax")',
        'README line ~71',
        out,
    ))

    # ── 4. web_fetch_direct — Wikipedia BM25 search ─────────────────────
    out = await web_fetch_direct(WIKI_42, search="Hitchhiker Guide")
    results.append((
        'web_fetch_direct(WIKI_42, search="Hitchhiker Guide")',
        'README line ~114',
        out,
    ))

    # ── 5. web_fetch_direct — Wikipedia slice retrieval ─────────────────
    out = await web_fetch_direct(WIKI_42, slices=[3, 4, 5])
    results.append((
        'web_fetch_direct(WIKI_42, slices=[3, 4, 5])',
        'README line ~144  [VOLATILE: slice content may shift]',
        out,
    ))

    # ── 6. web_fetch_direct — Wikipedia fragment ────────────────────────
    out = await web_fetch_direct(WIKI_42_FRAGMENT)
    results.append((
        'web_fetch_direct(WIKI_42#fragment)',
        'README line ~176',
        out,
    ))

    # ── 7. web_fetch_direct — Wikipedia footnotes ───────────────────────
    out = await web_fetch_direct(WIKI_42, footnotes=[14, 15])
    results.append((
        'web_fetch_direct(WIKI_42, footnotes=[14, 15])',
        'README line ~211  [VOLATILE: footnote numbering may shift]',
        out,
    ))

    # ── 8. web_fetch_direct — arXiv abs interception ────────────────────
    out = await web_fetch_direct(ARXIV_ATTN)
    results.append((
        'web_fetch_direct(ARXIV_ATTN)',
        'README line ~231',
        out,
    ))

    # ── 9. arxiv search ─────────────────────────────────────────────────
    out = await arxiv(action="search", query="ti:attention AND cat:cs.CL", limit=3)
    results.append((
        'arxiv(action="search", query="ti:attention AND cat:cs.CL", limit=3)',
        'README line ~263',
        out,
    ))

    # ── 10. arxiv category ──────────────────────────────────────────────
    out = await arxiv(action="category", query="cs.AI", limit=3)
    results.append((
        'arxiv(action="category", query="cs.AI", limit=3)',
        'README line ~279  [VOLATILE: latest papers change daily]',
        out,
    ))

    # ── 11. web_fetch_direct — S2 URL interception ──────────────────────
    out = await web_fetch_direct(S2_ATTN_URL)
    results.append((
        'web_fetch_direct(S2_ATTN_URL)',
        'README line ~301',
        out,
    ))

    # ── 12. semantic_scholar paper ──────────────────────────────────────
    out = await semantic_scholar(action="paper", query=S2_ATTN_ID)
    results.append((
        'semantic_scholar(action="paper", query=S2_ATTN_ID)',
        'README line ~315  [VOLATILE: citation counts change]',
        out,
    ))

    # ── 13. semantic_scholar snippets ───────────────────────────────────
    out = await semantic_scholar(
        action="snippets", query="multi-head attention", paper_id=S2_ATTN_ID,
    )
    results.append((
        'semantic_scholar(action="snippets", query="multi-head attention", paper_id=S2_ATTN_ID)',
        'README line ~343',
        out,
    ))

    # ── 14. web_fetch_direct — httpbin JSON ─────────────────────────────
    out = await web_fetch_direct(HTTPBIN_JSON)
    results.append((
        'web_fetch_direct(HTTPBIN_JSON)',
        'README line ~391',
        out,
    ))

    # ── Print all results ───────────────────────────────────────────────
    for label, note, output in results:
        print(_banner(label, note))
        print(output)
        print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
