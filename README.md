# Claude Web Tools

MCP server providing web browsing and content extraction tools for Claude.

## Tools

All tool names vary by profile (see [Profile Options](#profile-options)).

### Kagi Integration
- **KagiSearch** / **kagi_search** - Search the web using Kagi's curated, SEO-resistant index
- **KagiSummarize** / **kagi_summarize** - Summarize URLs or text (supports PDFs, YouTube, audio)

### Browser Tools
- **WebFetchJS** / **web_fetch_js** - Fetch JavaScript-rendered web content with full browser emulation

### Direct Fetch
- **WebFetchDirect** / **web_fetch_direct** - Fetch raw content without JavaScript rendering (HTML, JSON, XML, plain text)
- **WebFetchSections** / **web_fetch_sections** - List section headings and anchor slugs for a web page

### Academic Papers
- **SemanticScholar** / **semantic_scholar** - Search and retrieve academic paper data from Semantic Scholar (search, paper details, references, authors)

### Shared Features

Both fetch tools share these capabilities:

- **MediaWiki fast path** - Wiki URLs (`/wiki/...`) are detected and fetched via the MediaWiki API with a [Wikimedia-compliant User-Agent](https://meta.wikimedia.org/wiki/User-Agent_policy), bypassing the browser or HTTP entirely. Returns clean markdown with YAML frontmatter including site name and generator metadata. A single-entry page cache avoids redundant API calls when multiple tools access the same page.
- **Semantic Scholar fast path** - `semanticscholar.org/paper/` URLs are intercepted and served via the S2 Graph API, bypassing CAPTCHA-blocked web pages. Returns structured paper data with YAML frontmatter.
- **Section extraction** - Use the `section` parameter with a heading name (or list of names) to extract specific sections. Supports disambiguation for duplicate heading names.
- **Fragment resolution** - URL fragments (e.g. `#section-name`) are resolved against the heading tree. Fuzzy matching handles cross-platform slug differences: case folding, underscore↔hyphen normalization (GFM vs Goldmark), and percent-encoded characters like `%27` (apostrophes).
- **Footnote extraction** (MediaWiki) - Inline footnotes appear as `[^N]` markers in the markdown output. Use the `footnotes` parameter to retrieve specific numbered entries. Author-date shorthand (e.g. "Simpson 2003, p. 8") is automatically resolved against the article's bibliography via `#CITEREF` links.
- **Markdown output with YAML frontmatter** - Returns structured output with title, source URL, and truncation hints. When content is truncated, frontmatter includes a table of contents so the caller can request specific sections.
- **Whitespace normalization** - Non-breaking spaces, HTML entities (`&nbsp;`), and exotic Unicode whitespace in headings and titles are normalized to plain ASCII spaces for reliable section matching.

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
    "claude-web-tools": {
      "command": "uv",
      "args": ["--directory", "/path/to/claude-web-tools", "run", "claude-web-tools", "--profile", "code"]
    }
  }
}
```

### Claude Desktop (macOS)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "claude-web-tools": {
      "command": "uv",
      "args": ["--directory", "/path/to/claude-web-tools", "run", "claude-web-tools", "--profile", "desktop"]
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
uv run claude-web-tools

# Claude Code profile (PascalCase naming)
uv run claude-web-tools --profile code

# Show help
uv run claude-web-tools --help
```

## Development

### Running Tests

```bash
# Unit tests (mocked, no network)
uv run pytest

# Live integration tests (hits real endpoints)
uv run pytest -m live
```
