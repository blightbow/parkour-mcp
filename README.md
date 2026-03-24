# Kagi Research MCP

A research synthesis pipeline for MCP. Enables agents to perform targeted content extraction from websites and research papers. Integrates with the APIs for Kagi Search, Kagi Summarize, Semantic Scholar, arXiv, and MediaWiki. It is primarily designed for Claude Code and Claude Desktop, but should be adaptable to most needs.

## Attribution

This tool accesses the [Semantic Scholar](https://www.semanticscholar.org/) API. Per the [S2 API license](https://www.semanticscholar.org/product/api/license), contributions to your work through the use of S2's API requires attribution to Semantic Scholar.

- If you are using this MCP server for purposes adjacent to research papers, _you should preemptively assume that this license applies to your outputs_.
- It goes without saying that any research you incorporate should also be credited as appropriate. Please be a responsible netizen.

**Note:** This project is a third-party tool unaffiliated with Kagi.com. Usage of their name has been generously allowed with this attribution.

## Purpose

There is a cavernous difference between good context and bad context. Modern LLM solutions have converged on agentic toolchains that pair cheaper text analysis LLMs (Haiku) with larger models that excel at reasoning (Opus), but sometimes the finer details get lost in this process. In a worst case scenario, sometimes these details get hallucinated during the summarization process...**including the attributed authors of the papers themselves**.

This MCP server implements a different approach that is grounded in targeted text extraction and reasoning chains. By breaking a page down into section headings and presenting it as a table of contents, the LLM can understand the composition of a document before making any further decisions.

### Section Extraction

**Section discovery** — lightweight table of contents with anchor slugs:

```
>>> web_fetch_sections("https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/User-Agent")
---
title: User-Agent header
source: https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/User-Agent
sections:
  - User-Agent header (#user-agent-header)
    - Syntax (#syntax)
      - Directives (#directives)
    - Firefox UA string (#firefox-ua-string)
    - Chrome UA string (#chrome-ua-string)
    - Crawler and bot UA strings (#crawler-and-bot-ua-strings) [header only]
      - Examples (Crawler and bot UA strings) (#examples)
    - Specifications (#specifications)
    - See also (#see-also)
---
```

**HTML page with truncation** — frontmatter includes a section TOC for follow-up requests:

```
>>> web_fetch_direct("https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/User-Agent", max_tokens=300)
---
title: User-Agent header
source: https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/User-Agent
truncated: Full page is 11.0 KB (~2,809 tokens), showing first ~300 tokens. ...
sections:
  - User-Agent header
    - Syntax
      - Directives
    - Firefox UA string
    - Chrome UA string
    - Crawler and bot UA strings [header only]
      - Examples (Crawler and bot UA strings)
    - Specifications
    - See also
---

# User-Agent header

The HTTP **User-Agent** request header is a characteristic string
that lets servers and network peers identify the application,
operating system, vendor, and/or version of the requesting user agent.
...
```

**Section extraction** — fetch a specific section by name:

```
>>> web_fetch_direct("https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/User-Agent", section="Syntax")
---
title: User-Agent header
source: https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/User-Agent
# User-Agent header > Syntax
section: Syntax
---

## Syntax

    User-Agent: <product> / <product-version> <comment>

Common format for web browsers:

    User-Agent: Mozilla/5.0 (<system-information>) <platform> (<platform-details>) <extensions>
```

Sometimes this is enough to decide that the document is of no relevence whatsoever. At this point the LLM can fetch specific sections of interest to either further evaluate relevence, or move on from the document entirely.

For documentation trapped in a JavaScript cage, the MCP server provides a Playwright enabled fetch tool that supports the same content extraction workflow. Tool chaining can also be used for limited interaction with webpage elements:

**ReAct interaction** — fetch a page, then interact with discovered elements:

```
>>> web_fetch_js(url="https://example.com/app")
---
title: Example App
source: https://example.com/app
browser: WebKit
---

# Example App
...

>>> web_fetch_js(url="https://example.com/app",
...              actions=[{"action": "fill", "selector": "input[name=query]", "value": "search term"},
...                       {"action": "click", "selector": "button#submit"}])
---
title: Example App — Search Results
source: https://example.com/app
browser: WebKit
---

# Search Results
...
```

### BM25 searching + content slicing

Not all websites are easily broken up into sections. For these, we need to be able to find text of interest and walk our way through the surrounding context.

**BM25 keyword search** — find relevant content in long or poorly-sectioned pages:

```
>>> web_fetch_direct("https://en.wikipedia.org/wiki/42_(number)", search="Hitchhiker Guide")
---
title: 42 (number)
source: https://en.wikipedia.org/wiki/42_(number)
total_slices: 7
search: "Hitchhiker Guide"
matched_slices: [4, 5]
hint: Use slices= to retrieve adjacent context by index
---

--- slice 4 (Popular culture > The Hitchhiker's Guide to the Galaxy (1/2)) ---
### The Hitchhiker's Guide to the Galaxy

The number 42 is, in *The Hitchhiker's Guide to the Galaxy* by Douglas Adams,
the "Answer to the Ultimate Question of Life, the Universe, and Everything",
calculated by an enormous supercomputer named Deep Thought over a period of
7.5 million years. Unfortunately, no one knows what the question is...

--- slice 5 (Popular culture > The Hitchhiker's Guide to the Galaxy (2/2)) ---
The fourth book in the series, the novel *So Long, and Thanks for All the Fish*,
contains 42 chapters. According to the novel *Mostly Harmless*, 42 is the
street address of Stavromula Beta.

In 1994, Adams created the *42 Puzzle*, a game based on the number 42.
Adams says he picked the number simply as a joke, with no deeper meaning...
```

**Slice retrieval** — fetch adjacent context by index after a search:

```
>>> web_fetch_direct("https://en.wikipedia.org/wiki/42_(number)", slices=[3, 4, 5])
---
title: 42 (number)
source: https://en.wikipedia.org/wiki/42_(number)
total_slices: 7
slices: [3, 4, 5]
hint: Use search= for BM25 keyword search, or slices= with adjacent indices for more context
---

--- slice 3 (Popular culture) ---
## Popular culture

--- slice 4 (Popular culture > The Hitchhiker's Guide to the Galaxy (1/2)) ---
### The Hitchhiker's Guide to the Galaxy
...

--- slice 5 (Popular culture > The Hitchhiker's Guide to the Galaxy (2/2)) ---
The fourth book in the series, the novel *So Long, and Thanks for All the Fish*,
contains 42 chapters...
```

This approach plays to the strength of LLMs:

- document exploration serves chain of thought; each step of the document walking process is procedural and informs the next step
- maintain high signal to noise ratio on the body text we **do** put into context
- expose the real citations so they can be followed into the next document
- place real contributors into context so they can be credited without hallucination

We can also save ourselves a tool invocation by treating a URL #fragment as a section.

**Wikipedia section via URL fragment** — resolves `#fragment` against the heading tree, with inline `[^N]` footnote markers:

```
>>> web_fetch_direct("https://en.wikipedia.org/wiki/42_(number)#The_Hitchhiker%27s_Guide_to_the_Galaxy")
---
title: 42 (number)
source: https://en.wikipedia.org/wiki/42_(number)#The_Hitchhiker%27s_Guide_to_the_Galaxy
site: Wikipedia
# Popular culture > The Hitchhiker's Guide to the Galaxy
section: The Hitchhiker's Guide to the Galaxy
matched_fragment: "#The_Hitchhiker%27s_Guide_to_the_Galaxy"
---

### The Hitchhiker's Guide to the Galaxy

The number 42 is, in *The Hitchhiker's Guide to the Galaxy* by Douglas Adams,
the "Answer to the Ultimate Question of Life, the Universe, and Everything",
calculated by an enormous supercomputer named Deep Thought over a period of
7.5 million years. Unfortunately, no one knows what the question is...

The Ultimate Question "What do you get when you multiply six by nine"[^14] is
found by Arthur Dent and Ford Prefect in the second book of the series,
*The Restaurant at the End of the Universe*.

Google also has a calculator easter egg when one searches "the answer to the
ultimate question of life, the universe, and everything." Once typed, the
calculator answers with the number 42.[^15]
```

### Special MediaWiki Handling

When one of the well-known MediaWiki URI schemas are detected, the tool automatically switches to fetching the article using the MediaWiki API and strips out the navigation boxes. This makes the Markdown conversion process less noisy (no extra HTML), and also plays nicely with Wikipedia's bot usage policy.

It also makes it easy to convert citation links into Markdown footnotes (seen above), which can then be obtained with another tool call. This surfaces additional content that can then be pulled into the research process.

**Footnote retrieval** — follow up with specific `[^N]` entries:

```
>>> web_fetch_direct("https://en.wikipedia.org/wiki/42_(number)", footnotes=[14, 15])
---
title: 42 (number)
source: https://en.wikipedia.org/wiki/42_(number)
footnotes_only: True
---

[^14]: ["Mathematical Fiction: Hitchhiker's Guide to the Galaxy (1979)"](http://kasmana.people.cofc.edu/MATHFICT/mfview.php?callnumber=mf458)
[^15]: ["17 amazing Google Easter eggs"](https://www.cbsnews.com/pictures/17-amazing-google-easter-eggs/2/)
```

### Special arXiv handling

arXiv `/abs/` and `/pdf/` URLs are intercepted by the fetch tools and served via the arXiv Atom API, returning structured metadata instead of scraped HTML. This gives you author affiliations, categories, version history, DOI crosslinks, and journal refs — data that would otherwise require manual extraction from the landing page. `/pdf/` URLs get a frontmatter hint noting that the original URL was a PDF link.

`/html/` URLs are deliberately **not** intercepted. arXiv's HTML endpoint serves the full rendered paper, which is more useful as full text with BM25 slicing support than as metadata-only. Not all papers have HTML renders (many older or pre-LaTeX papers lack them), so the `full_text` hint is only emitted after a HEAD check confirms availability. When HTML is unavailable, a `warning` field is emitted instead and the SemanticScholar cross-reference steers toward body text snippets as an alternative.

**arXiv URL interception** — `/abs/` URLs return structured metadata via API:

```
>>> web_fetch_direct("https://arxiv.org/abs/1706.03762")
---
title: Attention Is All You Need
source: https://arxiv.org/abs/1706.03762v7
api: arXiv
full_text: Use WebFetchDirect with https://arxiv.org/html/1706.03762v7 for full paper text with search/slices
see_also: ARXIV:1706.03762v7 with SemanticScholar for citations
---

# Attention Is All You Need

**Authors:** Ashish Vaswani, Noam Shazeer, Niki Parmar, Jakob Uszkoreit, ...

**Published:** 2017-06-12T17:57:34Z
**Updated:** 2023-08-02T00:41:18Z

**Primary category:** cs.CL
**Categories:** cs.LG

**Comment:** 15 pages, 5 figures

**Abstract:** https://arxiv.org/abs/1706.03762v7
**PDF:** https://arxiv.org/pdf/1706.03762v7
**HTML:** https://arxiv.org/html/1706.03762v7

*For citation data, use SemanticScholar with `ARXIV:1706.03762v7`*

## Abstract

The dominant sequence transduction models are based on complex recurrent
or convolutional neural networks in an encoder-decoder configuration...
```

**arXiv search** — uses arXiv query syntax with field prefixes and boolean operators:

```
>>> arxiv(action="search", query="ti:attention AND cat:cs.CL", limit=3)
---
api: arXiv
action: search
query: ti:attention AND cat:cs.CL
hint: Use paper action for full details, or SemanticScholar with ARXIV:<id> for citation data
---

1. **Attention Is All You Need** [cs.CL]
   Ashish Vaswani et al.
   arXiv:1706.03762v7
2. **An Attention Free Transformer** [cs.LG]
   Shuangfei Zhai et al.
   arXiv:2105.14103v2
...
```

**Category browsing** — recent papers in an arXiv category:

```
>>> arxiv(action="category", query="cs.AI", limit=3)
---
api: arXiv
action: category
category: cs.AI
hint: Use paper action for full details, or SemanticScholar with ARXIV:<id> for citation data
---

1. **DMMRL: Disentangled Multi-Modal Representation Learning...** [cs.LG]
   Long Xu et al.
   arXiv:2603.21108v1
...
```

The arXiv tool is designed to complement SemanticScholar. arXiv provides the canonical metadata (affiliations, categories, version history), while SemanticScholar provides citation counts, influential citation tracking, and body text snippet search. Frontmatter hints guide the LLM to cross-reference between the two.

### Special SemanticScholar.org handling

SemanticScholar.org bears its own special mention for research paper synthesis. S2 has emerged as an alternative to Google Scholar that is much more accessible to tool automation. The main limitation is that it cannot be crawled with standard HTTP tooling, but that's where the Semantic Scholar API comes into play. We expose this in two ways:

1. A dedicated SemanticScholar tool that exposes broader functionality than the standard page fetching tools.
2. Attempts to run the fetch tools against SemanticScholar are automatically converted into an equivalent SemanticScholar tool call, with a hint in the YAML frontmatter to use that tool for subsequent tool calls.

Our decision to use BM25 searching with the fetch tools was informed by SemanticScholar's own usage of it. By keeping the search mechanism uniform across tools, the LLM won't make mistakes that would otherwise emerge from pivoting between two search methodologies.

**Semantic Scholar URL interception** — S2 URLs are automatically handled by fetch tools:

```
>>> web_fetch_direct("https://www.semanticscholar.org/paper/Attention-Is-All-You-Need-Vaswani-Shazeer/204e3073870fae3d05bcbc2f6a8e263d9b72e776")
---
title: Attention is All you Need
source: https://www.semanticscholar.org/paper/204e3073870fae3d05bcbc2f6a8e263d9b72e776
api: Semantic Scholar
see_also: ARXIV:1706.03762 with ArXiv for categories and affiliations
---

# Attention is All you Need
...
```

**Semantic Scholar paper lookup** — structured paper data via API:

```
>>> semantic_scholar(action="paper", query="204e3073870fae3d05bcbc2f6a8e263d9b72e776")
---
title: Attention is All you Need
source: https://www.semanticscholar.org/paper/204e3073870fae3d05bcbc2f6a8e263d9b72e776
api: Semantic Scholar
see_also: ARXIV:1706.03762 with ArXiv for categories and affiliations
---

# Attention is All you Need

**Authors:** Ashish Vaswani, Noam Shazeer, Niki Parmar, Jakob Uszkoreit, ...

**Year:** 2017
**Venue:** Neural Information Processing Systems
**Published:** 2017-06-12

**Citations:** 170,259 (19,464 influential) | **References:** 41

**ArXiv:** [1706.03762](https://arxiv.org/abs/1706.03762)

## TL;DR

A new simple network architecture, the Transformer, based solely on
attention mechanisms, dispensing with recurrence and convolutions entirely...

## Abstract

The dominant sequence transduction models are based on complex recurrent
or convolutional neural networks in an encoder-decoder configuration...
```

**Semantic Scholar snippet search** — search within paper body text by section:

```
>>> semantic_scholar(action="snippets", query="multi-head attention",
...                  paper_id="204e3073870fae3d05bcbc2f6a8e263d9b72e776")
---
api: Semantic Scholar
action: snippets
query: multi-head attention
paper: 204e3073870fae3d05bcbc2f6a8e263d9b72e776
hint: Use paper action for abstract, TL;DR, and citation data
---

### Multi-Head Attention

Instead of performing a single attention function with d_model-dimensional
keys, values and queries, we found it beneficial to linearly project the
queries, keys and values h times with different, learned linear projections...

### Scaled Dot-Product Attention

We call our particular attention "Scaled Dot-Product Attention" (Figure 2).
The input consists of queries and keys of dimension d_k, and values of
dimension d_v...
```

Corpus-wide search (no `paper_id`) returns results grouped by paper then section. A pre-flight check gates scoped searches on full-text availability; papers without it get an informative message suggesting the `paper` action for abstract/TL;DR.

### Kagi Tooling


#### Kagi Search
We also found the built-in search tooling of major LLM providers to be somewhat lacking for our research purposes.

1. They tend to incorporate LLM based summarizations of page content. These are verbose on tokens and work against our toolchain's goal of reduced dependence on summarization.
2. We have observed censored search results for legitimate research topics for reasons that are not explained by the LLM provider's usage policies.

Our solution was to integrate the Kagi search engine as a more neutral third party in the research process. Kagi's SEO resistant search results were already a good fit for research purposes, but their business model is much less likely to produce the conflict of interests that led us to implementing a dedicated search engine tool.

As for the practical difference between the tooling, I'll let Claude Desktop have the floor for a moment:

> The practical implication is that the two tools slot into different phases of a research workflow. The built-in search is optimized for "search and immediately synthesize" — the deep snippets and citation indexing mean I can often compose a cited answer from search results alone without any follow-up fetches. Kagi is optimized for "search and triage" — the compact snippets let you quickly scan which sources are worth a deeper pull via `web_fetch_direct` or `kagi_summarize`. It's a scout vs. a quartermaster.
> There's a context budget trade-off hiding in there too. Ten built-in search results with their deep snippets consume substantially more context window than five Kagi results with compact snippets. For a single-query task that's fine — you want the depth. But in a multi-source research workflow where you might run 5-10 searches, Kagi's lighter footprint per query leaves more room for the actual synthesis work.

#### Kagi Summarize

We've integrated access to the Kagi Universal Summarizer API for similar reasons. If a LLM provider's default search tool is censoring the search results, it only stands to reason that contamination of summaries may also be occurring. The tool descriptions gently steer the LLM away from the Kagi Summarize tool in favor of the standard workflows, because:

- it's cheaper for the user (no API cost)
- our original use case is to avoid summarization regardless

### Everything Else

While the intended use of these tools is to assist with long form content, the fetch tools will handle attempts for text/plain, application/json, and application/xml without throwing an error. The tools do not enrich these contents in any way, but surfacing simple content is preferable to throwing an avoidable error.

**JSON endpoint** — returns raw content with type metadata:

```
>>> web_fetch_direct("https://httpbin.org/json")
---
title: json
source: https://httpbin.org/json
content_type: json
---

{
  "slideshow": {
    "author": "Yours Truly",
    "title": "Sample Slide Show"
  }
}
```
## Usage

```bash
# Default (desktop profile, snake_case naming)
uv run kagi-research-mcp

# Claude Code profile (PascalCase naming)
uv run kagi-research-mcp --profile code

# Show help
uv run kagi-research-mcp --help
```

## Profile Options

The `--profile` argument adjusts tool names and descriptions for the target client. Each profile tailors the descriptions to explain how the MCP tools complement that client's built-in capabilities — for example, the `code` profile describes `WebFetchDirect` as returning full unsummarized text (vs Claude Code's summarizing `WebFetch`), while the `desktop` profile describes it as a local-fetch fallback for when Claude Desktop's server-proxied `web_fetch` gets rate-limited by the target site:

| Profile | Target | Tool Names |
|---------|--------|------------|
| `desktop` (default) | Claude Desktop | `kagi_search`, `kagi_summarize`, `web_fetch_js`, `web_fetch_direct`, `web_fetch_sections`, `semantic_scholar`, `arxiv` |
| `code` | Claude Code | `KagiSearch`, `KagiSummarize`, `WebFetchJS`, `WebFetchDirect`, `WebFetchSections`, `SemanticScholar`, `ArXiv` |

The `desktop` profile (snake_case) is the default as it aligns with MCP ecosystem conventions. Claude Code's PascalCase naming is the exception, not the norm.

## Tools

All tool names vary by profile (see [Profile Options](#profile-options)).

Tool Name          | Claude Code Tool Name | Description
-------------------|-----------------------|------------
kagi_search        | KagiSearch            | Search the web using Kagi.com's curated, SEO-resistant index
web_fetch_sections | WebFetchSections      | List section headings and anchor slugs for a web page (for targeted extraction)
web_fetch_direct   | WebFetchDirect        | Fetch a Markdown rendered version a HTML webpage (also returns raw content for common content types: JSON, XML, plain text)
web_fetch_js       | WebFetchJS            | Use Playwright to render a headless version of the website in Markdown (extracting documents from a JavaScript cage)
semantic_scholar   | SemanticScholar       | Search and retrieve academic paper data from Semantic Scholar (search, paper details, references, authors, body text snippets)
arxiv              | ArXiv                 | Search and retrieve academic papers from arXiv (search with field-prefix syntax, paper details, category browsing)
kagi_summarize     | KagiSummarize         | Summarize URLs or text (supports PDFs, YouTube, audio)

### fetch tool capabilities (common)

The fetch tools share the following features:

- **Markdown output with YAML frontmatter** - Returns structured output with title, source URL, and truncation hints. When content is truncated, frontmatter includes a table of contents so the caller can request specific sections.
- **Section extraction** - Use the `section` parameter with a heading name (or list of names) to extract specific sections. Supports disambiguation for duplicate heading names.
- **Fragment resolution** - URL fragments (e.g. `#section-name`) are resolved against the heading tree. Fuzzy matching handles cross-platform slug differences: case folding, underscore↔hyphen normalization (GFM vs Goldmark), and percent-encoded characters like `%27` (apostrophes).
- **Whitespace normalization** - Non-breaking spaces, HTML entities (`&nbsp;`), and exotic Unicode whitespace in headings and titles are normalized to plain ASCII spaces for reliable section matching.
- **arXiv fast path** - `arxiv.org/abs/` and `arxiv.org/pdf/` URLs are intercepted and served via the arXiv Atom API, returning structured metadata (authors with affiliations, categories, DOI, journal refs, version history). `/html/` URLs are deliberately excluded so they fall through to HTTP fetch for full paper text with BM25 slicing support. Frontmatter includes hints to the `/html/` URL and SemanticScholar cross-reference.
- **Semantic Scholar fast path** - `semanticscholar.org/paper/` URLs are intercepted and served via the S2 Graph API, bypassing CAPTCHA-blocked web pages. Returns structured paper data with YAML frontmatter.
- **MediaWiki fast path** - Wiki URLs (`/wiki/...`) are detected and fetched via the MediaWiki API with a [Wikimedia-compliant User-Agent](https://meta.wikimedia.org/wiki/User-Agent_policy), bypassing  HTTP entirely. Returns clean markdown with YAML frontmatter including site name and generator metadata. A single-entry page cache avoids redundant API calls when multiple tools access the same page.
- **Footnote extraction** (MediaWiki) - Inline footnotes appear as `[^N]` markers in the markdown output. The `footnotes` parameter retrieves specific numbered entries. Author-date shorthand (e.g. "Simpson 2003, p. 8") is automatically resolved against the article's bibliography via `#CITEREF` links.

### web_fetch_js Capabilities

Renders pages using a headless browser, enabling access to content that requires JavaScript execution:

- **JS-heavy sites** - SPAs, React/Vue/Angular apps, dynamically loaded content
- **Live app frameworks** - Automatic detection of Gradio and Streamlit apps with accelerated loading (avoids networkidle timeouts)
- **Embedded iframes** - Extracts content from iframes when main page is sparse (e.g., HuggingFace Spaces)
- **Interactive elements** - Returns annotated selectors for ReAct-style interaction chains

### web_fetch_direct Capabilities

Lightweight HTTP fetch without browser overhead:

- **HTML pages** - Converts to markdown with section support
- **JSON / XML / plain text** - Returns raw content with YAML frontmatter metadata
- **Footnote retrieval** - `footnotes=4` or `footnotes=[1,3,8]` returns specific numbered entries from MediaWiki pages, with bibliography resolution for author-date shorthand
- **BM25 keyword search** - `search="terms"` does BM25 keyword search over ~500-token slices of the page. Terms are matched independently and results are ranked by relevance (powered by [tantivy](https://github.com/quickwit-oss/tantivy-py)). Pages are chunked using [semantic-text-splitter](https://github.com/benbrandt/text-splitter)'s `MarkdownSplitter`, which respects heading and paragraph boundaries. Each matching slice is returned with a section ancestry breadcrumb (e.g. `Methodology > Approach A (2/3)`).
- **Slice retrieval** - `slices=[3, 4, 5]` retrieves specific slices by index from the cached page. Use this to fetch adjacent context after a search, or to page through a large document. The page cache is single-entry and auto-evicts when a new URL is fetched.

The search and slicing workflow mirrors the SemanticScholar `snippets` action — both use BM25 keyword matching over ~500-token chunks tagged by section.

## Setup

### Configuration

#### Claude Code

Add to your `.mcp.json`:

```json
{
  "mcpServers": {
    "kagi-research-mcp": {
      "command": "uv",
      "args": ["--directory", "/path/to/kagi-research-mcp", "run", "kagi-research-mcp", "--profile", "code"]
    }
  }
}
```

#### Claude Desktop (macOS)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "kagi-research-mcp": {
      "command": "uv",
      "args": ["--directory", "/path/to/kagi-research-mcp", "run", "kagi-research-mcp", "--profile", "desktop"]
    }
  }
}
```
### Kagi API Key (for search/summarize tools)

Set your Kagi API key via environment variable or config file:

```bash
# Option 1: Environment variable
export KAGI_API_KEY="your-api-key"

# Option 2: Config file
mkdir -p ~/.config/kagi
echo "your-api-key" > ~/.config/kagi/api_key
```

Get your API key at https://kagi.com/settings?p=api

### Semantic Scholar API Key (optional)

The SemanticScholar tool works without an API key but shares a global rate limit pool. For your own rate limit, get a free key and configure it:

```bash
# Option 1: Environment variable
export S2_API_KEY="your-api-key"

# Option 2: Config file
mkdir -p ~/.config/kagi
echo "your-api-key" > ~/.config/kagi/s2_api_key
```

Get your free API key at https://www.semanticscholar.org/product/api#api-key-form

### Browser Engine (for web_fetch_js)

The `web_fetch_js` tool requires a Playwright browser engine. Install one or more:

```bash
# WebKit (lightweight, preferred when available)
uv run playwright install webkit

# Chromium (broader compatibility, larger download)
uv run playwright install chromium

# Firefox (alternative option)
uv run playwright install firefox
```

**Browser selection logic:**
1. If `PLAYWRIGHT_BROWSER` env var is set, use that browser
2. If only one browser is installed, use it
3. If multiple browsers available, prefer the engine with the lightest footprint: webkit (smallest) > firefox > chromium (largest)

**Override example:**
```bash
# Force Chromium even if WebKit is available
export PLAYWRIGHT_BROWSER=chromium
```

The active browser is shown in tool output: `[Browser: WebKit | ...]`



## Development

### Running Tests

```bash
# Unit tests (mocked, no network)
uv run pytest

# Live integration tests (hits real endpoints)
uv run pytest -m live
```

## FAQ

> Is this project officially maintained by Kagi.com?

No, this is a third-party project.

> Is this project affiliated with Kagi.com?

Only in the sense that they let us use their name if we make it clear that this is a third-party project. The maintainer doesn't receive any form of monetary compensation, direct or indirect. (i.e. no API key kickbacks)

Other than that, we have a shared goal in making the web less enshittified. LLMs hallucinate more when they are forced to draw conclusions from their trained data, and often reach conclusions based on data is already months old. This MCP server is designed to help LLMs investigate the actual research texts and verify sources.

> Will there be support for other search engines?

Kagi is optimized against SEO pollution and a natural fit for research needs. If Kagi isn't your cup of tea, you are encouraged to use this MCP server alongside other servers that expose your preferred search engine(s).

> Do I need to pay for an API key?

**Kagi Tools:** _Yes._ We can't provide prices here because they are subject to change.
- https://help.kagi.com/kagi/api/summarizer.html
- https://help.kagi.com/kagi/api/search.html

**Semantic Scholar Tool:** No. The key is optional, and free: https://www.semanticscholar.org/product/api

**arXiv Tool:** No. The arXiv API is free and requires no authentication.

> Why can't I use Kagi's search API? I have money in my API wallet.

Kagi's search API is currently in closed beta and access is granted on an individual basis. The process is simple, send an e-mail and they will enable your use of the search API. https://help.kagi.com/kagi/api/search.html

> Why is the kagi_summarize tool refusing my request? I have money in my API wallet.

The MCP server automatically locks out the kagi_summarize tool if your balance dips below $1 USD. This is a safeguard against having your search functionality locked out by expensive kagi_summarize calls.

The flag is stored internally and persists until a kagi_search call successfully executes and observes that the balance has gone above $1 again. Restarting the MCP server will also clear the flag.

> My agent developed an addiction to kagi_summarize and drank my entire API balance in one sitting!

You probably shouldn't have auto-approved that tool. Sorry, we can't help.

> Why is the Semantic Scholar tool returning 429 errors about a global rate limit?

Because you are hitting S2's global rate limit. All anonymous API calls for S2 share the same rate limit pool, and the the calls made through this tool are no different.

You can request an API key from S2 [here](https://www.semanticscholar.org/product/api). There is no fee, but approvals are entirely at S2's own discretion.

> Why are arXiv API calls so slow?

The arXiv API requires a minimum 3-second interval between requests. This is enforced by the MCP server's rate limiter to comply with arXiv's [API terms of use](https://info.arxiv.org/help/api/tou.html). Parallel tool calls are serialized and the second caller sleeps for the remaining window.

> Why are batched tool calls against Semantic Scholar so slow?

The S2 API enforces a rate limit of 1s even when your API calls are authenticated. The MCP server queues requests for the SemanticScholar tool and internally throttles them to a 1.25s spacing in order to avoid unnecessary tool retries.

**Do not remove this throttling.** The 1s rate limit is upstream of you and this will make tool calls fail unnecessarily.

> What about Google Scholar?

Google Scholar does not provide an official API. Semantic Scholar has comparable coverage of documents that have not been paywalled.

> Your MCP server insulted the honor of my family, drained my Kagi API balance to $0, and developed a cult of personality when I connected it to OpenClaw.

We accept no liability, and there is no liability to be accepted. How your prompt stack spends your API balance isn't something we can help with.

Also, why would you connect a tool designed with almost no synthesis of research papers to a MCP server dedicated to research synthesis? 

## Credits

- Kagi.com for permission to use the Kagi name, and providing tools that were a natural fit for our needs.
- SemanticScholar.org for providing a much more accessible alternative to Google Scholar, and a fast turnaround on the API key for our internal testing.
- arXiv.org for providing a free, well-documented Atom API that made this integration straightforward.
- Wikipedia.org for allowing this tool to leverage the MediaWiki API at the easy cost of a user-agent header.
- The authors of the dependencies used by this MCP server. There are too many of you to list individually, but we appreciate your work greatly.
