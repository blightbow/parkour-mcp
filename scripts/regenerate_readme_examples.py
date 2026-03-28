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

import httpx
import respx

from kagi_research_mcp.fetch_direct import web_fetch_direct, web_fetch_sections
from kagi_research_mcp.semantic_scholar import semantic_scholar
from kagi_research_mcp.arxiv import arxiv

# Disable Reddit rate limiter for fixture-based generation
import kagi_research_mcp.reddit as _reddit_mod
_reddit_mod._reddit_limiter.min_interval = 0.0

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

# ── Reddit fixture (offline — no network) ───────────────────────────────────
REDDIT_THREAD_URL = "https://www.reddit.com/r/Python/comments/1abc234/trusted_publishers_discussion/"

def _reddit_comment(
    *, id: str, author: str, body: str, score: int,
    created_utc: float = 1774500000.0, replies: object = "",
) -> dict:
    return {"kind": "t1", "data": {
        "id": id, "author": author, "body": body, "score": score,
        "created_utc": created_utc, "replies": replies,
    }}

REDDIT_FIXTURE = [
    # Post listing
    {"data": {"children": [{"kind": "t3", "data": {
        "title": "Don't make your package repos trusted publishers",
        "author": "syllogism_",
        "selftext": (
            "A lot of Python projects have a GitHub Action that's configured as a "
            "trusted publisher. Some action such as a tag push triggers the release "
            "process, and ultimately leads to publication to PyPI.\n\n"
            "If your project repo is a trusted publisher, it's a single point of "
            "failure with a huge attack surface. It's much safer to have a wholly "
            "separate private repo that you register as the trusted publisher."
        ),
        "score": 31,
        "num_comments": 24,
        "subreddit": "Python",
        "created_utc": 1774481422.0,
        "is_self": True,
        "url": "https://old.reddit.com/r/Python/comments/1abc234/trusted_publishers_discussion/",
        "link_flair_text": "Discussion",
        "upvote_ratio": 0.68,
    }}]}},
    # Comment listing
    # Post is at 1774481422 (2026-03-25 23:30 UTC).
    # Comment timestamps are offsets from post time for realistic T+ values.
    {"data": {"children": [
        _reddit_comment(
            id="ochpsln", author="ManyInterests", score=54,
            created_utc=1774481422.0 + 2400,  # T+00:40:00
            body=(
                "It's definitely hazard-prone, but if you follow PyPI's guidance "
                "on how to configure this, you should be fine.\n\n"
                "Just configure a dedicated PyPI release environment in the "
                "GitHub settings, add yourself as a required approver."
            ),
            replies={"data": {"children": [
                _reddit_comment(
                    id="oci19t7", author="dan_ohn", score=11,
                    created_utc=1774481422.0 + 6168,  # T+01:42:48
                    body="I was going to say this, PyPI even have a clear message "
                         "explaining this when you set the environment to (any).",
                ),
                _reddit_comment(
                    id="ocjbfsz", author="syllogism_", score=-6,
                    created_utc=1774481422.0 + 25800,  # T+07:10:00
                    body="Even with the environment configured that way, if your "
                         "GitHub is configured to trigger a release once a tag is "
                         "pushed, then people just need to compromise the repo.",
                ),
            ]}},
        ),
        _reddit_comment(
            id="ochlh3a", author="latkde", score=48,
            created_utc=1774481422.0 + 978,  # T+00:16:18
            body=(
                "There are different aspects of security. A hyper secure airgapped "
                "workflow is pointless if it's so cumbersome that I don't use it.\n\n"
                "The \"trusted publisher\" approach is a big improvement over the "
                "previous best practices: there are no credentials to manage, thus "
                "no credentials that could be compromised."
            ),
            replies={"data": {"children": [
                _reddit_comment(
                    id="ocjbq9t", author="syllogism_", score=-4,
                    created_utc=1774481422.0 + 25680,  # T+07:08:00
                    body="You can build completely fine ergonomics around this. "
                         "Have a script on your machine that triggers the release.",
                ),
            ]}},
        ),
        _reddit_comment(
            id="ochqajo", author="denehoffman", score=11,
            created_utc=1774481422.0 + 2580,  # T+00:43:00
            body="None of this matters if your GitHub gets hacked. Just don't be "
                 "an idiot with Actions.",
        ),
    ]}},
]


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

    # ── Reddit examples (fixture-based, no network) ──────────────────
    json_url = "https://old.reddit.com/r/Python/comments/1abc234/trusted_publishers_discussion/.json"
    async with respx.MockRouter() as router:
        router.get(json_url).mock(
            return_value=httpx.Response(200, json=REDDIT_FIXTURE),
        )

        # 15. web_fetch_sections — Reddit comment tree
        out = await web_fetch_sections(REDDIT_THREAD_URL)
        results.append((
            'web_fetch_sections(REDDIT_THREAD_URL)',
            'README: Reddit comment tree discovery',
            out,
        ))

    async with respx.MockRouter() as router:
        router.get(json_url).mock(
            return_value=httpx.Response(200, json=REDDIT_FIXTURE),
        )

        # 16. web_fetch_direct — Reddit comment extraction
        out = await web_fetch_direct(REDDIT_THREAD_URL, section="ochpsln")
        results.append((
            'web_fetch_direct(REDDIT_THREAD_URL, section="ochpsln")',
            'README: Reddit comment extraction by ID',
            out,
        ))

    async with respx.MockRouter() as router:
        router.get(json_url).mock(
            return_value=httpx.Response(200, json=REDDIT_FIXTURE),
        )

        # 17. web_fetch_direct — Reddit BM25 search
        out = await web_fetch_direct(REDDIT_THREAD_URL, search="trusted publisher")
        results.append((
            'web_fetch_direct(REDDIT_THREAD_URL, search="trusted publisher")',
            'README: Reddit BM25 search across comments',
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
