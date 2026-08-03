# Changelog

All notable changes to parkour-mcp will be documented in this file.

Format: https://keepachangelog.com/en/1.1.0/
Versioning: https://semver.org/spec/v2.0.0.html

## [2.1.4] 2026-08-02

### Fixed
- the quant audit no longer clears a base as having no floating-point grid to preserve when the Hub simply did not report its scale arrays, a false all-clear that invited the one requantization most likely to silently damage an FP4-native model.
- the microscaling-grid signal is now withheld on every repo whose published histogram counts parameters instead of stored bytes, rather than being reported from arithmetic that could not have established it.
- the tool no longer tells a reader that an NVFP4 release carries a microscaling grid it does not have, nor accuses a vendor's own 4-bit release of upcast bloat at nearly double its true bits-per-weight.
- a quantization that keeps part of its base's native microscaling backbone is measured again instead of being silently discarded, without restoring the false grid claim over releases that carry no such scales.
- quant_audit's grid-preservation verdict was inferred from a dtype histogram that cannot support it, leaving it silent on bases whose scale arrays the Hub omits and ranking a partly-affine quantization above a bit-exact one; it now rules from each repo's declared per-module map, separating a preserved grid from a float-to-float regrid from a collapse onto a uniform integer lattice.
- a checkpoint split across both unsharded files and per-weight parts is measured whole rather than half, so repos shipping that layout no longer report roughly two-thirds of their true bits-per-weight or lose their quantization-grid signal to a partition artefact.
- a per-channel or per-tensor quantization whose metadata is complete now reports its quantization-grid measurement instead of having it withheld by a bound that only ever applied to group-wise schemes.


### Documentation
- Make quant-detection comments declarative

## [2.1.3] 2026-08-01

### Fixed
- a natively-quantized FP4 model whose weights are packed two-to-a-byte no longer reports roughly double its true bits-per-weight, so comparing a quant against such a base no longer reads as though the quant discarded half the precision it actually preserved.
- a checkpoint whose experts sit on a preserved microscaling grid is no longer labelled affine, which on an FP4-native base is the one word that signals the quant regridded onto a lattice the model was never trained for.
- the FP-microscaling note no longer reports a checkpoint as preserving a grid it was never compared against, and quant_audit now returns the preservation verdict itself, including the silent case where a native FP4 lattice was requantized onto a uniform one.

## [2.1.2] 2026-07-30

### Changed
- Discourse and GitHub timestamps parse directly instead of being rewritten first, with identical results.
- a multi-line prose entry in a list literal is now visibly one element rather than indistinguishable from a missing comma.
- nine expressions across the package are now written in their simpler form, min over a sorted-then-indexed list, .values() over a discarded key binding, and one tuple endswith over three chained calls, with behaviour verified identical.
- fifteen internal functions that took six or more parameters positionally now require names for their optional tail, and the separator was placed from an AST sweep of every call site in the package and tests, so no existing caller changes and none could silently break.
- the seven remaining helpers whose callers did pass past the boundary now name those arguments at all fifteen call sites, so reading a call to _dispatch_slicing no longer means counting commas to tell whether the fifth argument was max_tokens or source_url.


### Fixed
- failures on the DOI and Reddit fallback paths now record where they happened instead of only that they happened.
- a failed metadata, caption, chapter or template fetch now leaves a traceback in debug logs instead of vanishing into an empty result.
- four circular-import suppressions sat on lines long enough that an import rewrap would carry the directive off the line it suppresses and silently stop it working, measured as eight new findings with the comment count unchanged; they are now short enough that the rewrap cannot reach them.
- reading a model's config.json stayed silent on MLX, the one checkpoint format that does not declare itself, reported no dimensions at all for current-generation releases that nest them under text_config, missed the expert count on any MoE config not spelling it num_experts, and implied a uniform quant width on checkpoints whose config declares per-layer overrides; it now names the format (TensorRT-LLM included), descends into the nested block, reads every expert spelling, distinguishes latent attention from the MHA it would otherwise be reported as, names the pre-extension context window when RoPE scaling extended it, and counts the overrides the summary drops.


### Documentation
- Correct perf entry against measured baselines
- Make Why: posture section-aware
- Assess the ruff 0.16 migration and plan the overhaul
- Fold the completed audit into the plan and drop dead claims


### Miscellaneous
- State two intents the code already depended on

## [2.1.1] 2026-07-28

### Fixed
- `uvx parkour-mcp` and `uv tool install parkour-mcp` work again instead of failing at startup with ModuleNotFoundError, after the mcp 2.0.0 release made every unbounded fresh install resolve an incompatible major.

## [2.1.0] 2026-07-27

### Added
- a new HuggingFace tool inspects Hub models in a single call: architecture, parameter count, checkpoint size, gated state, base-model lineage, and per-file checksums, plus the effective bits-per-weight of a quantized release. Where the Hub's own metadata cannot support that arithmetic it says so and stays silent, rather than publishing a number that would misread an honest release as bloated. Weight files are described, never downloaded: a `.safetensors` read returns its checksum and the byte-range recipe for reading the header.


### Changed
- lazy imports across the package claimed to break cycles that did not exist and defer dependencies already loaded at startup, so the one suppression that genuinely mattered was indistinguishable from the fifty that did not; the surviving four now cite a reproducible failure, and the rest became a policy ruff enforces in both directions.


### Documentation
- Add Hub tool design spec
- Correct the checker-authority note
- Record the ruff 0.16 migration with triage

## [2.0.0] 2026-07-26

### Added
- viewing a GitHub repo or reading a file through parkour now reports the OpenSSF Scorecard rating in frontmatter, giving agents an at-a-glance trust signal before consuming third-party code.
- YouTube video URLs now resolve to structured metadata and the video description through the toolkit, surfacing channel, duration, view counts, captions availability, language, and upload date as frontmatter alongside fenced description content, instead of forcing callers through a Kagi summarizer or a generic HTTP fetch that YouTube blocks.
- transcripts now resolve to coalesced ~30s windows in one of four output shapes (compact / absolute / none / structured), surfacing caption text alongside timing in a form an LLM can both reason over and cite, with the chunking strategy adapting to caption quality so unpunctuated auto-captions degrade gracefully instead of being forced through prose-shaped splitting.
- YouTube transcripts are now queryable like a document. Callers can find specific phrases without reading the whole transcript, restrict to a time range when they know roughly when something was said, retrieve adjacent windows for context, and choose between score-ordered and chronological hits — all backed by a per-video Tantivy index that builds lazily, so the basic transcript-fetch path pays no indexing cost.
- callers can now orient themselves on a YouTube channel without manually navigating tabs, list a channel's recent uploads in a single tool call, and walk a playlist's items, all returning a structured frontmatter + numbered entry list shape that downstream agents can paginate or follow into individual videos via the existing ``video`` and ``transcript`` actions.
- callers can run YouTube-wide searches without leaving the toolkit, with results matching the website's search-page ordering and feeding directly into the existing video/transcript actions for follow-up.
- PoTokenRequired and IP-block failures from youtube-transcript-api can now recover automatically when yt-dlp's caption code path is reachable, instead of forcing the caller to switch tools or wait the rate-limit out, and the recovery is transparent — the frontmatter's ``api:`` line declares which path produced the transcript so downstream agents can adapt.
- video metadata, description, and comments now compose cleanly: the metadata frontmatter is constant and the body block is one of three independent artifacts (description, transcript via the dedicated action, or comments via the boolean pivot), so a caller's investigation of one doesn't pull in the cost of the others.
- the Youtube tool's schema stays focused on video metadata, transcripts, and channel/playlist/search investigations; comment exploration lives behind one dedicated tool whose three parameters are all directly relevant to its purpose, with a frontmatter pivot from the video action so callers starting from a watch URL can discover the comments path without reading the tool registry themselves.
- a YouTube video's chapters are exactly the section structure the toolkit's existing search methodology builds around — surfacing them as both a navigation aid in frontmatter and a filter dimension on the Tantivy index lets an LLM use the natural unit a creator already chose to partition the content with, instead of having to derive structure from time-window boundaries.
- the YouTube tools now ship with the same visual identity the rest of the toolkit maintains, and no future tool can land without an icon — the test gate fails CI when ``_ICON_FILES`` lacks an entry, when the SVG is missing, or when the regeneration registry drifts from the runtime registry.
- Claude Desktop now sees a clear path to fetch a transcript on non-English videos, picks the right language code shape on the first try, and doesn't spend tokens on YouTube's auto-translation matrix unless an explicit miss makes it relevant.
- an LLM that requests an unfetchable language now sees the full browser-visible auto-translate menu alongside the directly-fetchable subset and a clear note that auto-translations are rate-limited, so the next retry converges on a source-language code instead of looping on entries pulled from a misleadingly-narrowed list.
- tools that previously emitted no follow-up steering (KagiSearch, KagiSummarize, ResearchShelf, IETF subseries and obsoleted-by) now point the LLM at the right next action, and existing hint phrasing no longer leaks engine internals (BM25, tantivy, tree-sitter grammar) into the dashboard the agent reads to decide what to do next.
- tool responses can now surface a once-per-session educational tip that fires a single time and never repeats, giving the calling agent a lightweight teaching channel distinct from per-result hints.
- the first time an agent pulls a whole uninspected page, the response now teaches it once that WebFetchSections can map the heading layout first, so later fetches can target a section instead of spending context on the full page.
- parkour can now be installed as a first-class Hermes Agent plugin whose carefully formatted, unsummarized tool output reaches the model intact instead of being JSON-escaped by the MCP transport.
- a Hermes user can now route the model's web fetch and search through parkour's unsummarized, frontmatter-steered tools by setting two config flags, instead of parkour competing with the built-ins as extra prefixed alternatives.
- a single kagi_search call can now target images, videos, news, or podcasts directly, apply a Kagi Lens to scope the search, paginate through results, and constrain by region or date.
- callers that need to enumerate Kagi's accepted region codes now consult kagi://regions instead of getting a partial list from the parameter prose, and maintainers refresh the set with one script invocation when upstream changes.
- callers that need to apply a built-in Kagi lens can now consult kagi://lenses to discover the 11 documented lens slugs and their activation status, instead of being told "built-in lens slug" with no examples.
- a driver that runs region=de on workflow=images now sees a warning naming the no-op the moment the response returns; a driver that runs a query with filters and gets nothing back is told exactly which filters were active and which one to drop first, rather than being sent down a query-syntax rabbit hole.
- Reddit comment threads, subreddit listings, and user pages work again through web_fetch_incisive and web_fetch_sections after Reddit's 2026-05-29 shutdown of the anonymous .json endpoints, with no account or API key required.


### Changed
- GitHub repo and file views now carry the same fresh, dated OpenSSF Scorecard rating that the Packages project action does, fed by deps.dev's weekly cron feed rather than the stale ossf/scorecard-action upload registry, so agents get an accurate trust signal they can weigh against the assessment date.
- commits and their `Why:` trailers are now the single source of truth for changelog content in both CHANGELOG.md (local assembly during /release) and GitHub Release bodies (CI regeneration on tag push), eliminating the prior split where git-cliff assembled and a separate Python script sliced.
- a sixth protected key now updates three doc places automatically via cog -r, and a behavioral change to FMEntries surfaces the relink-gate prompt instead of silently desyncing the prose.
- cog'd tables now read cleanly in source — each row ends at its content rather than 200+ trailing spaces — while still rendering as a clean GFM table on GitHub.
- a narrow cog-derived table now reads as a sharp grid, while a wide one stays compact at the longest row, so neither aesthetic suffers the artifact of the other style being applied uniformly.
- a new always-on tool now triggers CI failure if its manifest entry is forgotten, instead of silently shipping a build where Claude Desktop can't see the tool.
- visiting a video's metadata, transcript, and any chapter-aware retrieval on the same URL is a common workflow, and the response now amortizes the cost of yt-dlp's full info-dict extraction across all three paths instead of paying for it once per path.
- WebFetchJS is gone; JavaScript-rendered pages are now reached through WebFetchIncisive's requires_js parameter, with actions for ReAct interaction, so callers no longer pick between two fetch tools and the frontmatter steers static-first, browser-on-demand.
- content-type handling is now defined once, so a new supported type or a fixed misclassification lands in a single place instead of three.
- no behavior change; the renderer no longer takes a parameter that restates two others it already has.
- no behavior change for the MCP server; this is groundwork that lets parkour expose its tools through a second entrypoint without duplicating the description and Semantic Scholar gating logic.
- the summarize tool now describes the host's built-in web fetch in host-neutral terms instead of asserting Claude-specific behavior that does not hold under other agent runtimes.
- the Hermes plugin and MCP server register the same tools as before, with less duplicated and indirect wiring behind them.
- search calls the v1 endpoint with Bearer auth and parses the new category-keyed response, surfaces v1 error codes as readable messages, and stops reporting balance state that the v1 surface no longer exposes.
- kagi_summarize is no longer a callable MCP tool on this release; trying to invoke it returns "unknown tool" until Kagi ships /summarize on v1 and a future release re-registers it against the new endpoint.
- each search parameter now publishes its enum, format, and constraints into the MCP tool schema, so LLM clients see lens_id's discovery URL, the page/limit relationship, the ISO 8601 date format, the ISO 3166 region code, and the workflow enum without having to dig into the tool-level prose.
- parameter docs now match v1's verified region case sensitivity, lens capability set, and cross-workflow filter applicability, and the prose is trimmed to what the LLM driver needs at invocation time rather than what we logged while debugging.
- a caller now reads either parameter and gets the full page-size and pagination-reach picture without having to cross-reference the other one.
- in clients that cannot autonomously read MCP resources, kagi_search's parameter prose now stands on its own — the always-on lens slugs are inline and the tool description does not steer the LLM toward URIs it has no way to dereference.
- in the default 'search' workflow nothing changes; on a non-default workflow with a lens set, the response now carries a frontmatter warning naming the unreliable combination and pointing at the retry, so an LLM that didn't read the schema prose still gets actionable guidance the moment the call returns.
- a consumer of multiple paginated pages now knows to deduplicate, a consumer of date filters now knows undated content drops out, and same-page repeatability is stated explicitly so the LLM doesn't have to assume.
- the driver now knows region errors loudly and lens fails silently, knows region's effect weakens on images, and won't confuse the news 360 lens with the news workflow — three failure modes UAT had to discover empirically because the docs were silent on them.
- limit and page no longer appear to contradict each other; a driver passing 'MM-DD-YYYY' learns immediately that strict ISO is required; an LLM that capitalized a slug gets a runtime warning naming the lowercase form to retry with; the images workflow's filter degradation is documented once, not three times.
- every tool response that carries untrusted web or API content now declares it as untrusted so downstream agents treat it with the right suspicion, and Reddit links in Kagi results now point at the tools that can actually fetch them.
- frontmatter behavior is unchanged for callers, and the container, builder, fence, and tip ledger are now an independently versioned library that other MCP servers can adopt instead of copying parkour's implementation.


### Fixed
- the LLM caller now sees what was bypassed, why recovery was possible at all, and (when both paths fail) a precise account of what was tried so the obvious workaround isn't repeatedly suggested.
- server startup once again completes cleanly, and the regression test catches any future tool description that accidentally introduces a brace literal that ``str.format()`` would treat as a placeholder.
- chapters were silently disappearing from rendered transcript output when a creator placed two chapter markers within the same ~30s span — common on educational videos with rapid-fire section transitions — and the chapters that did render carried timestamps that didn't match the chapter list above the fence, breaking the LLM's ability to cite a moment by chapter name.
- insufficient-credit failures now surface the actionable "add funds at https://kagi.com/settings/billing_api" message to the calling LLM instead of the raw HTTPError string, which never named the underlying cause.
- section= queries against heading-only dividers now explain why the body is empty and direct the caller to the section tree, instead of returning a silent empty fence.
- tool responses are no longer transmitted twice per call, cutting wire payload roughly in half for clients that render both MCP content channels.
- web_fetch_direct now retrieves pages behind Akamai-class bot managers, which 403 an HTTP/1.1 request that claims to be Chrome, by speaking HTTP/2 and sending the browser-consistent Sec-Fetch and Client-Hint headers a real Chrome emits.
- setting MCP_ALLOW_PRIVATE_IPS to True or YES (not only 1) now correctly allows fetching private and internal addresses, matching the other opt-in gates.
- Reddit search URLs (global, subreddit-scoped, and the type=sr / type=user variants) now return real results with fetchable permalinks through web_fetch_incisive, instead of a misleading empty listing or an HTTP 400.
- Reddit search no longer hides adult results by default, so it behaves consistently with Kagi and direct Reddit fetches; callers wanting a SFW search can pass include_over_18=0.
- an S2 call that fails because the configured API key is invalid now names that as the cause and links the reactivation form, instead of surfacing a bare HTTP 403 that reads like a transient outage.


### Security
- every YouTube tool response now respects the frontmatter trust boundary the standard documents — user-uploaded titles, channel names, uploader handles, and chapter titles all render only inside the fenced content zone where the per-line ``│`` prefix and label sanitization neutralize injection vectors.
- a requires_js fetch of a raw JSON or XML endpoint now gets the same 5 MiB size cap and 60-second wall-clock deadline as a plain fetch, instead of an unbounded read that a slow-drip endpoint could stall indefinitely.
- lint exemptions for tooling no longer create a permanent security blind spot for future scripts and tests — a new urlopen-on-untrusted-input or unsafe XML parse is still rejected at the gate.


### Documentation
- CHANGELOG.md and GitHub Release bodies render in web browsers where GitHub's GFM pipeline translates source newlines inside paragraphs to <br> elements (in the Releases rendering context, distinct from README.md which does not). Hard wrapping at 72-79 cols therefore produced a narrow ragged column in wide browser windows. Flowing prose lets the renderer wrap to the container width as intended. This commit aligns historical entries with the going-forward git-cliff output (which never wraps), and gh release edit was run against v1.0.0, v1.1.0, v1.1.2, and v1.2.0 to push the flowed slices to their published Release bodies (v1.0.1 left untouched; its original "Minor bugfix release" prose remains).
- Document protected multi-contributor keys
- Cite code by path#Symbol, not path:line
- Synthesize session-derived patterns and anti-patterns
- Record YouTube tool deferrals from the v1 implementation
- Record v1 spec URLs and content-type rejection gap
- Document v1 cutover state and dormant summarize island


### Miscellaneous
- Reset content caches between tests
- Stabilize test_full_pipeline with best_of timing

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
