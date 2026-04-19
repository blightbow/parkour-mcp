# Changelog

All notable changes to parkour-mcp will be documented in this file.

Format: https://keepachangelog.com/en/1.1.0/
Versioning: https://semver.org/spec/v2.0.0.html

## [1.2.0] 2026-04-19

### Added

- Reddit comment-permalink URLs now return the full thread with the linked comment pre-selected, instead of a silent 1-of-N truncated response. Previously, fetching a URL like `/r/SUB/comments/POSTID/slug/COMMENTID/` returned only a context-scoped subtree (the linked comment and its replies) while silently dropping the rest of the thread. Empirical test against a 62-comment post: the permalink returned 1 comment where the post URL returned all 62. The fast path now strips the permalink to its post URL and injects `section=<comment_id>` so the output lands on the linked comment, with a frontmatter `note` explaining the rewrite. Caller-supplied `section=` or `search=` parameters win silently and disable the rewrite. A subsequent fetch of the bare post URL reuses the cache populated by the earlier permalink call, so search and slice follow-ups are near-instant.
- Section names matching the search term now carry stronger weight than in-prose mentions. A search for `troubleshooting` surfaces slices from the troubleshooting section even when that exact word does not appear inside the body prose; navigation-style queries (`configuration`, `methodology`) behave more predictably. Internally this adds a boosted `heading` field to the tantivy index populated from the section ancestry breadcrumb, with a 2.0x boost over body-only matches. Tuned against a 29-slice document with known structure; ranking order is stable across boost magnitudes so the behavior is not fragile.

### Changed

- Frontmatter fields that aggregate contributions from multiple subsystems (`hint`, `warning`, `note`, `see_also`, `alert`) now compose correctly when two subsystems deposit on the same key in a single request. Previously a second contributor would silently clobber the first, so callers who expected to see both a rate-limit advisory and a fragment-resolution warning saw only the last one written. Internally this is enforced via a `FMEntries(UserDict)` subclass that raises `TypeError` on direct subscript assignment to protected keys, routing all mutations through the sanctioned `.append()` path. Single-item fields still render as scalars; two-or-more items render as YAML sequences per the frontmatter standard.

### Fixed

- Natural-language search queries with stray punctuation no longer crash the request. A query like `search="System Prompt: Git status"` previously failed with the cryptic error `Field does not exist: 'Prompt'` because tantivy's strict parser interpreted the colon as a field qualifier. The lenient parser now degrades unparseable tokens to match-nothing while valid terms still rank; a new `warning` frontmatter entry surfaces the parse error so callers can see when their query was silently rewritten. Side benefit: the full tantivy query grammar (phrases, booleans, slop, fuzzy) is now usable from `search=` without risking crashes on punctuation.
- Search callers now see a `warning` field in the response frontmatter when their query was silently modified by the lenient parser. A query like `search="System Prompt: Git status NEW"` previously dropped the word `Prompt` (the lenient parser reads `Prompt:` as a field qualifier) and returned a full-looking result with the original query echoed back, giving the caller no signal that the query had been rewritten. The raw tantivy error is now surfaced on-wire alongside a concrete fix suggestion (wrap multi-word terms in double quotes, or use the documented search operators). The `warning` field composes cleanly with existing contributors such as fragment-resolution advisories and parameter-conflict notices, rendering as a YAML list when multiple warnings fire.
- The Reddit fast path works again. Between 2026-03-27 and 2026-04-18, Reddit tightened JA3/JA4 TLS-fingerprint-based bot detection, and the httpx-backed fetch started returning 403 blocked-page HTML instead of the expected JSON. The fast path now uses `curl_cffi` with Safari 18.4 impersonation; empirical verification confirms Safari and Firefox profiles pass reliably while Chrome profiles still get 403 (Reddit's detector is Chrome-targeted).

### Security

- Three static-analysis rules now enforce security- and standards-sensitive invariants at CI time, complementing the existing runtime guards:

  - **SSRF precedence**: any outbound HTTP fetch (`guarded_fetch` or raw `httpx.get`) must be preceded by `check_url_ssrf` in the same function.
  - **Content fence discipline**: only `_fence_content` in `markdown.py` may emit trust-boundary fence markers; hand-rolled markers elsewhere are flagged.
  - **Frontmatter key discipline**: `fm_entries` variables must be constructed via `FMEntries(...)`, never via plain `dict` literals, so the runtime guard cannot be bypassed at construction.

  These rules run as part of the pytest suite (~1.5s overhead) and surface violations with file, line, and rule id. Agentic coding makes quiet regressions on these invariants plausible; static analysis closes the gap that runtime guards and human review miss.

### Documentation

- Tool descriptions are now LLM-first: they focus on when to pick each tool and how to call it accurately rather than documenting implementation details callers do not need. Specific corrections: phantom tool references removed (`research_shelf` no longer lists a nonexistent "DOI tool"); the full tantivy search grammar is exposed on every tool that accepts `search=`; cross-references between sibling tools survive deferred tool loading in Claude Code and Claude Desktop where one tool may be surfaced without the other. The `summarize` tool's guidance now frames it as the right choice only when the built-in `WebFetch` summarizer cannot reach the page (captcha-gated, PDFs, YouTube, audio).


## [1.1.2] 2026-04-16

This is v1.1.1 in a trenchcoat. A naive workflow accident burned the v1.1.1 workflow and it was simplest to reconverge the pipeline with a second release bump. `v1.1.1` exists in the git history but has no corresponding GitHub Release.

### Added
- `web_fetch_sections` TOC is now paginated via a `slice` parameter (#8). The previous 100-section cap silently hid entries on long documents: RFC 9110 has 311 sections, so the TOC dump ran out at §8.6 and callers had no way to discover §17 Security Considerations. `slice=N` returns the Nth 100-section window, negative indices count from the end, and new `total_sections` / `total_slices` frontmatter plus a same-tool `hint` advertise advancement.

### Fixed
- IETF RFC-Editor fast path narrowed to metadata URLs only: bare path or `.json` suffix (#7). Previously every `rfc-editor.org/rfc/rfc{N}` URL shape (`.html`, `.txt`, `.xml`) was intercepted and returned only structured metadata, trapping the caller in a cycle because the `full_text` hint pointed back at an intercepted URL. Body-text suffixes now fall through to the generic HTTP path and get real `section=` / `search=` support. Related hint text for the IETF branch of `web_fetch_sections` was also corrected.
- Pipeline DoS hardened (#6) via two defenses. First, a lazy slice/index build: the MarkdownSplitter and tantivy index now run only on first access to slices or search, so callers that only read the rendered markdown or section tree never pay that cost (the WHATWG HTML Living Standard fixture drops from 6.07s to 0.71s on `web_fetch_sections`). Second, a circuit breaker that rejects any line longer than 1 MB before it reaches MarkdownSplitter's char-level fallback: the known 73.6s hang on a 6 MiB single-paragraph body now returns in 0.13ms with a structured "page lacks structural boundaries" response and `matched_slices: unavailable` frontmatter.
- GitHub blob fetches defend via `max_tokens` and the existing 60s wall-clock deadline, replacing the uniform 5 MiB wire-bytes gate that was rejecting legitimately large source files. Callers can now raise `max_tokens` to read more of a large file; the truncation hint spells out that option alongside `section=`, `search=`, and `#L` anchor targeting. Blobs without a tree-sitter grammar (`.txt`, `.log`, unknown extensions) get the same 1 MB single-line circuit breaker as #6.
- `web_fetch_sections` honors a relaxed 50 MiB response cap for section-extraction fetches. The uniform 5 MiB cap defeated the tool's purpose on monolithic specs (WHATWG HTML at ~15 MiB, ECMAScript, C++ drafts): it produced a menu that couldn't be ordered from, because follow-up `web_fetch_direct(section=X)` calls would be rejected by the same cap. Unconstrained fetches keep the 5 MiB cap; the 60s deadline still applies to both paths.
- Section-by-name matching now works on spec-sized documents. Two bugs were broken together: the heading link regex required non-empty anchor text, so empty-text self-link permalinks common to spec documents leaked their anchor syntax into stored section names (callers typing the human-visible heading saw a miss); and the 5 MiB cap rejection described above gated the follow-up fetch.
- Section matching tolerates spec-numbered headings. The heading link regex now handles backslash-escaped parens in anchor URLs (closing a WHATWG display corruption that duplicated the suffix of `Attribute value (double-quoted) state`), and the number-prefix stripper handles both `15.` (literal trailing period) and `15\.` (CommonMark-escaped) forms so callers can look up `section="Security Considerations"` on RFC-Editor headings like `15. Security Considerations`.
- Title extraction skips fenced code blocks. WHATWG's real `<h1>` is nested inside a `<header><hgroup>` subtree that the noise-tag filter decomposes, so the first surviving `# ` line was a bash comment inside a `<textarea>` example (producing titles like "System-wide .bashrc file for interactive bash(1) shells" on the HTML Living Standard).
- `scripts/regenerate_readme_examples.py` passes ruff E402 after the post-v1.1.0 reorder.

### Changed
- Version tag pushes are now gated on both the mocked and the live test suites.

## [1.1.0] 2026-04-16

Significant performance increases by changing from markdownify to htmd-py in the HTML to Markdown core. This was a long series of rc version bumps while we waited for the upstream dependencies to get properly aligned so that a pinned fork was no longer needed.

Candidly, the RCs were because I needed to keep bumping the version number on the manifest.json to keep Claude Desktop happy during UAT. That was a naive dev mistake; next time I'll just bump the file locally with -dev semversioning.

### Added
- `search_repos` action on the GitHub tool, distinct from `search_issues`. Prevents callers from guessing that repo search terms might work inside the issues endpoint.
- Dedicated MediaWiki/Wikipedia tool with a `references` action that resolves inline `CITEREF` links into full citation entries.
- `issue_templates` action on the GitHub tool, surfacing per-form header steering hints so callers know what information a specific issue form expects before drafting.
- Scripted icon generation with Discourse and MediaWiki glyphs added to the icon set.

### Changed
- HTML-to-markdown conversion moved from `markdownify` to the Rust-backed `htmd-py`. Measured throughput on captured fixtures: 11x on small pages (88ms to 8ms, PEP 8), 33x on medium (6.9s to 208ms, ECMA-262), and 46x on the pathological 15 MB WHATWG HTML spec (17.2s to 372ms). The swap also fixes a silent truncation defect in the previous candidate library, where the WHATWG fixture collapsed to 439 KB of output (losing 96% of the document) with no warning.
- `htmd-py` pinned to upstream v0.1.2 on PyPI after `lmmx/htmd#41` landed the four text-only handler fields parkour-mcp uses (`skip_tags`, `image_placeholder`, `drop_empty_alt_images`, `drop_image_only_links`). The temporary `blightbow/htmd-py` fork is retired.
- `WebFetchExact` renamed to `WebFetchIncisive` for clearer intent.
- `lint-deep` promoted from advisory to hard gate; `vulture` adopted for dead-code scanning.
- Version tag pushes gated on the live test suite.

### Fixed
- Unsubstituted MCPB template literals in environment variables are now rejected at startup instead of propagating as literal `${VAR_NAME}` strings to downstream API calls.
- Interactive-element truncation in WebFetchJS is surfaced to callers via frontmatter instead of being silently dropped.
- Page cache is populated from all `sections=` path handlers, not just the happy path. A fast-path fetch followed by a `sections=` drill-in on the same URL no longer re-fetches.
- Truncation chunks are packed to retain body content when hard token limits are hit, instead of spending the budget on boilerplate.
- Image assets are bundled with the wheel, fixing icon display in Claude Desktop.
- Repo labels are surfaced when a `search_issues label:` filter misses, aiding query correction.
- Defense-in-depth response size limits added across the fetch path.
- Workaround for an upstream Claude Desktop bug that corrupted the perceived GitHub API key when the Desktop GUI text field was left empty. The bug also caused the `~/.config/parkour/github_token` fallback to be ignored.
- 4xx and 5xx errors from GitHub no longer masked as cache-population failures.

## [1.0.1] 2026-04-10

### Fixed
- Discourse tool handles modern Discourse API response shapes. Schema drift was preventing actions from completing on some sites.
- MCPB manifest description trimmed to 100 characters or less, allowing the `.mcpb` artifact to push to the MCP Registry.

## [1.0.0] 2026-04-10

It's an initial 1.0 release! What could possibly go wrong?

🐛🐞 🪱 🪲

Initial public release of parkour-mcp, an MCP server for content exploration and research synthesis. See README.md for the full tool inventory and usage.

### Added
- Twelve API-backed content tools covering search (Kagi), academic research (arXiv, Semantic Scholar, DOI content negotiation), IETF RFCs and Internet-Drafts, package ecosystems (deps.dev across npm, PyPI, Go, Maven, Cargo, NuGet, RubyGems), GitHub, MediaWiki / Wikipedia, Reddit, and Discourse forums.
- Research shelf: passive citation tracking across arXiv, Semantic Scholar, DOI, and GitHub tools, with cross-DOI dedup for preprint versus journal versions, scoring, notes, and export to BibTeX, RIS, and JSON.
- Claude Desktop Extension (`.mcpb`) packaging with per-tool `title` fields.
- Dual profile registration: the `code` profile uses PascalCase tool names, the `desktop` profile uses snake_case.
- Fast-path URL detection chain routing known sources (arXiv, Semantic Scholar, IETF, DOI, Reddit, GitHub, MediaWiki, Discourse) through structured APIs instead of HTML scraping.
- BM25 keyword search and slice retrieval over cached pages via tantivy.
- Content fencing for indirect prompt injection defense, with YAML frontmatter provenance metadata emitted outside the fence.
- 2Q (two-queue) scan-resistant page cache and multi-entry LRU wiki cache.
- Section discovery with fuzzy slug matching, fragment resolution, and GFM-style heading anchors.
- Playwright-backed WebFetchJS for JS-rendered pages, with live-app detection for Gradio and Streamlit.

### Security
- SSRF protections hoisted before the fast-path chain to close a MediaWiki bypass.
- DOI redirect following restricted to trusted hosts.
- ReDoS and AST traversal hardening (H1-H4, M1-M5).
