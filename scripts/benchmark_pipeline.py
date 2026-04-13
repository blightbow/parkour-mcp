"""End-to-end benchmark of the parkour-mcp content pipeline.

Covers the generic-HTTP path (phase-by-phase, three page-size tiers) and each
fast path (end-to-end wall clock). Used to catch latency regressions in
``html_to_markdown``, ``MarkdownSplitter``, ``_CacheEntry`` construction, and
the structured-API fetchers.

Usage:
    uv run python3 scripts/benchmark_pipeline.py
    uv run python3 scripts/benchmark_pipeline.py --update-baselines
    uv run python3 scripts/benchmark_pipeline.py --capture-fixtures

Output:
    - Readable table printed to stdout
    - scripts/benchmark_baselines.json updated if --update-baselines is passed
    - tests/fixtures/perf/*.gz written if --capture-fixtures is passed

Fixtures captured:
    - Generic-HTTP tiers: raw HTML, gzipped
    - Fast paths: raw API response body (JSON/XML/markdown), gzipped

Fixtures are consumed by tests/test_perf.py for deterministic regression tests.
"""
import argparse
import asyncio
import gzip
import json
import platform
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import tantivy
from semantic_text_splitter import MarkdownSplitter

from parkour_mcp.common import _FETCH_HEADERS
from parkour_mcp.markdown import (
    html_to_markdown,
    _extract_sections_from_markdown,
    _compute_slice_ancestry,
)

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
BASELINES_PATH = SCRIPT_DIR / "benchmark_baselines.json"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "perf"

SPLITTER = MarkdownSplitter((1600, 2000))

# Generic-HTTP tiers: (tier_name, url, fixture_filename)
GENERIC_HTTP_TIERS = [
    ("small", "https://peps.python.org/pep-0008/", "pep_8.html.gz"),
    ("medium", "https://tc39.es/ecma262/", "ecma262.html.gz"),
    ("pathological", "https://html.spec.whatwg.org/", "whatwg_html.html.gz"),
]

# Fast paths: (name, url, fixture_filename)
# Each URL should be a known-stable, long-form resource so the benchmark
# exercises realistic content sizes.
FAST_PATHS = [
    (
        "mediawiki",
        "https://en.wikipedia.org/wiki/Transformer_(deep_learning_architecture)",
        "mediawiki_transformer.json.gz",
    ),
    (
        "arxiv",
        "https://arxiv.org/abs/1706.03762",
        "arxiv_attention.xml.gz",
    ),
    (
        "ietf",
        "https://www.rfc-editor.org/rfc/rfc9110.html",
        "ietf_rfc9110.json.gz",
    ),
    (
        "doi",
        "https://doi.org/10.1038/nature14539",
        "doi_nature14539.json.gz",
    ),
    # Reddit: listing URL rather than a specific comment thread — Reddit's
    # old.reddit.com JSON endpoint returns 404 for individual comment threads
    # from most external IPs (anti-scraping) but serves listings fine.
    (
        "reddit",
        "https://www.reddit.com/r/Python/top/",
        "reddit_python_top.json.gz",
    ),
    (
        "discourse",
        "https://meta.discourse.org/t/about-the-meta-category/1",
        "discourse_meta_about.json.gz",
    ),
    (
        "github_blob",
        "https://github.com/python/cpython/blob/v3.12.0/Lib/asyncio/base_events.py",
        "github_blob_cpython_base_events.py.gz",
    ),
    (
        "github_issue",
        "https://github.com/python/cpython/issues/100000",
        "github_issue_cpython_100000.json.gz",
    ),
    (
        "github_pull",
        "https://github.com/python/cpython/pull/1",
        "github_pull_cpython_1.json.gz",
    ),
]


# ---------------------------------------------------------------------------
# Tantivy: local copy of _CacheEntry index build for phase-level timing
# ---------------------------------------------------------------------------

def _build_tantivy(slices: list[str]) -> tantivy.Index:
    """Build a BM25 index over slices.  Mirrors _CacheEntry._build_index."""
    builder = tantivy.SchemaBuilder()
    builder.add_text_field("body", stored=True)
    builder.add_unsigned_field("idx", stored=True)
    schema = builder.build()
    index = tantivy.Index(schema)
    writer = index.writer()
    for i, text in enumerate(slices):
        writer.add_document(tantivy.Document(body=text, idx=i))
    writer.commit()
    index.reload()
    return index


# ---------------------------------------------------------------------------
# Generic-HTTP phase measurement
# ---------------------------------------------------------------------------

async def bench_generic_http(
    client: httpx.AsyncClient, tier: str, url: str,
    capture_path: Optional[Path] = None,
) -> Optional[dict]:
    """Measure each phase of the generic-HTTP path against a real URL.

    Returns a timing dict, or None on HTTP failure.  Writes the raw HTML to
    *capture_path* (gzipped) if provided.
    """
    t0 = time.perf_counter()
    try:
        resp = await client.get(url, headers=_FETCH_HEADERS)
        resp.raise_for_status()
    except Exception as e:
        print(f"  FAIL fetch: {type(e).__name__}: {e}")
        return None
    html = resp.text
    t_fetch = time.perf_counter() - t0

    if capture_path:
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(capture_path, "wt", encoding="utf-8") as f:
            f.write(html)
        print(f"  captured {capture_path.relative_to(REPO_ROOT)} ({len(html)//1024} KB raw)")

    t0 = time.perf_counter()
    _, markdown = html_to_markdown(html)
    t_md = time.perf_counter() - t0

    t0 = time.perf_counter()
    chunks = SPLITTER.chunk_indices(markdown)
    slices = [text for _, text in chunks]
    offsets = [offset for offset, _ in chunks]
    t_splitter = time.perf_counter() - t0

    t0 = time.perf_counter()
    sections = _extract_sections_from_markdown(markdown)
    t_sections = time.perf_counter() - t0

    t0 = time.perf_counter()
    _ = _compute_slice_ancestry(sections, offsets)
    t_ancestry = time.perf_counter() - t0

    t0 = time.perf_counter()
    _build_tantivy(slices)
    t_tantivy = time.perf_counter() - t0

    return {
        "tier": tier,
        "url": url,
        "html_bytes": len(html),
        "md_bytes": len(markdown),
        "n_slices": len(slices),
        "n_sections": len(sections),
        "fetch_ms": t_fetch * 1000,
        "html_to_markdown_ms": t_md * 1000,
        "splitter_ms": t_splitter * 1000,
        "sections_ms": t_sections * 1000,
        "ancestry_ms": t_ancestry * 1000,
        "tantivy_ms": t_tantivy * 1000,
    }


# ---------------------------------------------------------------------------
# Fast-path end-to-end measurement
# ---------------------------------------------------------------------------

async def bench_fast_path(
    name: str, url: str,
    capture_path: Optional[Path] = None,
) -> Optional[dict]:
    """Measure a single fast path end-to-end via ``web_fetch_direct``.

    Captures the raw upstream response body (gzipped) if *capture_path* is
    provided.  The capture hook hooks httpx via a transport wrapper so it
    records the first HTTP response body seen during the call.
    """
    from parkour_mcp.fetch_direct import web_fetch_direct

    # Reset any page-cache state so each run starts fresh
    from parkour_mcp._pipeline import _page_cache
    _page_cache.clear()

    captured_bytes: list[bytes] = []
    if capture_path:
        _install_capture_hook(captured_bytes)

    t0 = time.perf_counter()
    try:
        result = await web_fetch_direct(url)
    except Exception as e:
        print(f"  FAIL {name}: {type(e).__name__}: {e}")
        return None
    finally:
        if capture_path:
            _remove_capture_hook()
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if result.startswith("Error:"):
        print(f"  FAIL {name}: {result.splitlines()[0][:120]}")
        return None

    # Detect fast-path responses that wrapped an upstream error in a
    # normal frontmatter + fence envelope — those appear as a fenced line
    # starting with ``│ Error:``.
    if "\n│ Error:" in result[:4096]:
        snippet = result[:240].replace("\n", " ")
        print(f"  FAIL {name}: upstream error in fence: {snippet}")
        return None

    if capture_path and captured_bytes:
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        body = captured_bytes[0]
        with gzip.open(capture_path, "wb") as f:
            f.write(body)
        print(f"  captured {capture_path.relative_to(REPO_ROOT)} ({len(body)//1024} KB raw)")

    return {
        "name": name,
        "url": url,
        "end_to_end_ms": elapsed_ms,
        "result_bytes": len(result),
    }


_original_send = None


def _install_capture_hook(sink: list[bytes]) -> None:
    """Monkeypatch httpx AsyncClient.send to capture the first response body.

    Simple capture strategy: wrap send(), and after each call append the
    response's content to sink.  The first non-empty body is written out.
    Lets us capture Reddit JSON, MediaWiki JSON, DOI CSL-JSON, etc. without
    having to know which client each fast path uses.
    """
    global _original_send
    if _original_send is not None:
        return
    _original_send = httpx.AsyncClient.send

    async def _wrapped(self, request, **kwargs):
        assert _original_send is not None
        resp = await _original_send(self, request, **kwargs)
        try:
            body = resp.content
            if body and not sink:
                sink.append(body)
        except Exception:
            pass
        return resp

    httpx.AsyncClient.send = _wrapped  # ty: ignore[invalid-assignment]


def _remove_capture_hook() -> None:
    global _original_send
    if _original_send is not None:
        httpx.AsyncClient.send = _original_send
        _original_send = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> dict:
    """Run all benchmarks and return the results dict."""
    results: dict = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "hardware": f"{platform.system()} {platform.machine()}",
        "python": platform.python_version(),
        "generic_http": {},
        "fast_paths": {},
    }

    print("Generic HTTP tiers:")
    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
        for tier, url, fixture_name in GENERIC_HTTP_TIERS:
            print(f"  {tier}: {url}")
            capture_path = (FIXTURES_DIR / fixture_name) if args.capture_fixtures else None
            r = await bench_generic_http(client, tier, url, capture_path=capture_path)
            if r:
                results["generic_http"][tier] = r

    print("\nFast paths:")
    for name, url, fixture_name in FAST_PATHS:
        print(f"  {name}: {url}")
        capture_path = (FIXTURES_DIR / fixture_name) if args.capture_fixtures else None
        r = await bench_fast_path(name, url, capture_path=capture_path)
        if r:
            results["fast_paths"][name] = r

    return results


def print_report(results: dict) -> None:
    gh = results.get("generic_http", {})
    if gh:
        print("\nGeneric-HTTP phase breakdown (ms):")
        hdr = (
            f"{'tier':<14} {'md_kb':>6} {'slices':>6} "
            f"{'fetch':>7} {'html→md':>9} {'splitter':>9} "
            f"{'sections':>9} {'ancestry':>9} {'tantivy':>8}"
        )
        print(hdr)
        for tier in ("small", "medium", "pathological"):
            r = gh.get(tier)
            if not r:
                continue
            print(
                f"{r['tier']:<14} "
                f"{r['md_bytes']//1024:>6} "
                f"{r['n_slices']:>6} "
                f"{r['fetch_ms']:>6.0f} "
                f"{r['html_to_markdown_ms']:>8.0f} "
                f"{r['splitter_ms']:>8.0f} "
                f"{r['sections_ms']:>8.0f} "
                f"{r['ancestry_ms']:>8.0f} "
                f"{r['tantivy_ms']:>7.0f}"
            )

    fp = results.get("fast_paths", {})
    if fp:
        print("\nFast-path end-to-end (ms):")
        print(f"{'name':<16} {'result_kb':>10} {'elapsed':>10}")
        for r in fp.values():
            print(
                f"{r['name']:<16} "
                f"{r['result_bytes']//1024:>10} "
                f"{r['end_to_end_ms']:>9.0f}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update-baselines", action="store_true",
        help="Overwrite scripts/benchmark_baselines.json with this run's results",
    )
    parser.add_argument(
        "--capture-fixtures", action="store_true",
        help="Write compressed fixtures into tests/fixtures/perf/",
    )
    args = parser.parse_args()

    results = asyncio.run(run(args))
    print_report(results)

    if args.update_baselines:
        BASELINES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(BASELINES_PATH, "w") as f:
            json.dump(results, f, indent=2)
            f.write("\n")
        print(f"\nBaselines written to {BASELINES_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
