# Kagi Research MCP

A research synthesis pipeline for MCP. Enables agents to perform targeted content extraction from websites and research papers. Integrates with the APIs for Kagi Search, Kagi Summarize, Semantic Scholar, and MediaWiki. It is primarily designed for Claude Code and Claude Desktop, but should be adaptable to most needs.

Note: This project is a third-party tool unaffiliated with Kagi.com. Usage of their name has been generously allowed with this attribution.

## Goal

Make it easier for LLM's to perform online research. Anthropic's tooling approach leans heavily toward auto-summarization. Our approach is targeted extraction of webpage sections:

- kagi_search to locate the content
- web_fetch_sections to generate a table of contents for a page (headings and #IDs)
- web_fetch_direct returns the requested sections (or the entire document without `sections=`)
  - web_fetch_js is an alternative to web_fetch_direct that deals with JS gated content.
  - MediaWiki citations are preserved inline as markdown footnotes (`[^1]`, `[^2]`, etc.)
  - MediaWiki citations can then be extracted with a follow-up fetch call (`footnotes=` parameter)
- semantic_scholar can be directly search papers indexed by SemanticScholar.org or fetch abstracts
- kagi_summarize is the option of last resort: summarize the content 

For Claude Code and Claude Desktop, the fetch tools bypass Anthropic's HTTP proxy. This avoids rate limiting issues or IP blocks associated with their IP space.


## Tools

All tool names vary by profile (see [Profile Options](#profile-options)).

Tool Name          | Claude Code Tool Name | Description
-------------------|-----------------------|------------
kagi_search        | KagiSearch            | Search the web using Kagi.com's curated, SEO-resistant index
web_fetch_sections | WebFetchSections      | List section headings and anchor slugs for a web page (for targeted extraction)
web_fetch_direct   | WebFetchDirect        | Fetch a Markdown rendered version a HTML webpage (also returns raw content for common content types: JSON, XML, plain text)
web_fetch_js       | WebFetchJS            | Use Playwright to render a headless version of the website in Markdown (extracting documents from a JavaScript cage)
semantic_scholar   | SemanticScholar       | Search and retrieve academic paper data from Semantic Scholar (search, paper details, references, authors, body text snippets)
kagi_summarize     | KagiSummarize         | Summarize URLs or text (supports PDFs, YouTube, audio)

### fetch tool capabilities (common)

The fetch tools share the following features:

- **Markdown output with YAML frontmatter** - Returns structured output with title, source URL, and truncation hints. When content is truncated, frontmatter includes a table of contents so the caller can request specific sections.
- **Section extraction** - Use the `section` parameter with a heading name (or list of names) to extract specific sections. Supports disambiguation for duplicate heading names.
- **Fragment resolution** - URL fragments (e.g. `#section-name`) are resolved against the heading tree. Fuzzy matching handles cross-platform slug differences: case folding, underscore↔hyphen normalization (GFM vs Goldmark), and percent-encoded characters like `%27` (apostrophes).
- **Whitespace normalization** - Non-breaking spaces, HTML entities (`&nbsp;`), and exotic Unicode whitespace in headings and titles are normalized to plain ASCII spaces for reliable section matching.
- **Semantic Scholar fast path** - `semanticscholar.org/paper/` URLs are intercepted and served via the S2 Graph API, bypassing CAPTCHA-blocked web pages. Returns structured paper data with YAML frontmatter.
- **MediaWiki fast path** - Wiki URLs (`/wiki/...`) are detected and fetched via the MediaWiki API with a [Wikimedia-compliant User-Agent](https://meta.wikimedia.org/wiki/User-Agent_policy), bypassing  HTTP entirely. Returns clean markdown with YAML frontmatter including site name and generator metadata. A single-entry page cache avoids redundant API calls when multiple tools access the same page.
- **Footnote extraction** (MediaWiki) - Inline footnotes appear as `[^N]` markers in the markdown output. The `footnotes` parameter retrieves specific numbered entries. Author-date shorthand (e.g. "Simpson 2003, p. 8") is automatically resolved against the article's bibliography via `#CITEREF` links.

### web_fetch_js Capabilities

Renders pages using a headless browser, enabling access to content that requires JavaScript execution:

- **JS-heavy sites** - SPAs, React/Vue/Angular apps, dynamically loaded content
- **Live app frameworks** - Automatic detection of Gradio and Streamlit apps with accelerated loading (avoids networkidle timeouts)
- **Embedded iframes** - Extracts content from iframes when main page is sparse (e.g., HuggingFace Spaces)
- **Interactive elements** - Returns annotated selectors for ReAct-style interaction chains

**ReAct interaction example:**
```python
# First call: fetch page, observe interactive elements
result = web_fetch_js(url="https://example.com/app")

# Follow-up: interact with discovered elements
result = web_fetch_js(
    url="https://example.com/app",
    actions=[
        {"action": "fill", "selector": "input[name=query]", "value": "search term"},
        {"action": "click", "selector": "button#submit"}
    ]
)
```

### web_fetch_direct Capabilities

Lightweight HTTP fetch without browser overhead:

- **HTML pages** - Converts to markdown with section support
- **JSON / XML / plain text** - Returns raw content with YAML frontmatter metadata
- **Footnote retrieval** - `footnotes=4` or `footnotes=[1,3,8]` returns specific numbered entries from MediaWiki pages, with bibliography resolution for author-date shorthand

### Sample Output

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
    - Crawler and bot UA strings (#crawler-and-bot-ua-strings)
    - Specifications (#specifications)
    - See also (#see-also)
---
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
    - Specifications
    - See also
---

# User-Agent header

The HTTP **User-Agent** request header is a characteristic string
that lets servers and network peers identify the application,
operating system, vendor, and/or version of the requesting user agent.
...
```

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

**Wikipedia full page** — when truncated, frontmatter includes a section table of contents for follow-up requests:

```
>>> web_fetch_direct("https://en.wikipedia.org/wiki/42_(number)", max_tokens=300)
---
title: 42 (number)
source: https://en.wikipedia.org/wiki/42_(number)
site: Wikipedia
truncated: Full page is 27.4 KB (~7,019 tokens), showing first ~300 tokens. ...
sections:
  - Mathematics
  - Wisdom literature, religion, and philosophy
  - Popular culture
    - The Hitchhiker's Guide to the Galaxy
    - Jackie Robinson
    - Japan
  - References
  - External links
---

For other uses, see 42.

Natural number
...
```

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

**Footnote retrieval** — follow up with specific `[^N]` entries:

```
>>> web_fetch_direct("https://en.wikipedia.org/wiki/42_(number)", footnotes=[14, 15])
---
title: 42 (number)
source: https://en.wikipedia.org/wiki/42_(number)
footnotes_only: True
---

[^14]: ["Mathematical Fiction: Hitchhiker's Guide to the Galaxy"](http://kasmana.people.cofc.edu/MATHFICT/mfview.php?callnumber=mf458)
[^15]: ["17 amazing Google Easter eggs"](https://www.cbsnews.com/pictures/17-amazing-google-easter-eggs/2/)
```

**Semantic Scholar paper lookup** — structured paper data via API:

```
>>> semantic_scholar(action="paper", query="204e3073870fae3d05bcbc2f6a8e263d9b72e776")

# Attention is All you Need

**Authors:** Ashish Vaswani, Noam Shazeer, Niki Parmar, Jakob Uszkoreit, ...

**Year:** 2017
**Venue:** Neural Information Processing Systems
**Published:** 2017-06-12

**Citations:** 169,982 (4,542 influential) | **References:** 41

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

**Semantic Scholar URL interception** — S2 URLs are automatically handled by fetch tools:

```
>>> web_fetch_direct("https://www.semanticscholar.org/paper/Attention-Is-All-You-Need-Vaswani-Shazeer/204e3073870fae3d05bcbc2f6a8e263d9b72e776")
---
title: Attention is All you Need
source: https://www.semanticscholar.org/paper/204e3073870fae3d05bcbc2f6a8e263d9b72e776
api: Semantic Scholar
---

# Attention is All you Need
...
```

## Setup

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

## Configuration

### Claude Code

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

### Claude Desktop (macOS)

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

## Profile Options

The `--profile` argument adjusts tool names and descriptions for the target client. Each profile tailors the descriptions to explain how the MCP tools complement that client's built-in capabilities — for example, the `code` profile describes `WebFetchDirect` as returning full unsummarized text (vs Claude Code's summarizing `WebFetch`), while the `desktop` profile describes it as a local-fetch fallback for when Claude Desktop's server-proxied `web_fetch` gets rate-limited by the target site:

| Profile | Target | Tool Names |
|---------|--------|------------|
| `desktop` (default) | Claude Desktop | `kagi_search`, `kagi_summarize`, `web_fetch_js`, `web_fetch_direct`, `web_fetch_sections`, `semantic_scholar` |
| `code` | Claude Code | `KagiSearch`, `KagiSummarize`, `WebFetchJS`, `WebFetchDirect`, `WebFetchSections`, `SemanticScholar` |

The `desktop` profile (snake_case) is the default as it aligns with MCP ecosystem conventions. Claude Code's PascalCase naming is the exception, not the norm.

## Usage

```bash
# Default (desktop profile, snake_case naming)
uv run kagi-research-mcp

# Claude Code profile (PascalCase naming)
uv run kagi-research-mcp --profile code

# Show help
uv run kagi-research-mcp --help
```

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

> Why are batched tool calls against Semantic Scholar so slow?

The S2 API enforces a rate limit of 1s even when your API calls are authenticated. The MCP server queues requests for the SemanticScholar tool and internally throttles them to a 1.25s spacing in order to avoid unnecessary tool retries.

**Do not remove this timeout.** The 1s rate limit is upstream of you and this will make tool calls fail unnecessarily.

> What about Google Scholar?

Google Scholar does not provide an official API and has comparable coverage of documents that have not been paywalled.

> Your MCP server insulted the honor of my family, drained my Kagi API balance to $0, and developed a cult of personality when I connected it to OpenClaw.

We accept no liability, and there is no liability to be accepted. How your prompt stack spends your API balance isn't something we can help with.

Also, why would you connect a tool designed with almost no synthesis of research papers to a MCP server dedicated to research synthesis? 
