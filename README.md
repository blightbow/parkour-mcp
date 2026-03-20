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

### Shared Features

Both fetch tools share these capabilities:

- **MediaWiki fast path** - Wiki URLs (`/wiki/...`) are detected and fetched via the MediaWiki API with a [Wikimedia-compliant User-Agent](https://meta.wikimedia.org/wiki/User-Agent_policy), bypassing the browser or HTTP entirely. Returns clean markdown with YAML frontmatter including site name and generator metadata. A single-entry page cache avoids redundant API calls when multiple tools access the same page.
- **Section extraction** - Use the `section` parameter with a heading name (or list of names) to extract specific sections. Supports disambiguation for duplicate heading names.
- **Fragment resolution** - URL fragments (e.g. `#section-name`) are resolved against the heading tree. Fuzzy matching handles cross-platform slug differences: case folding, underscore↔hyphen normalization (GFM vs Goldmark), and percent-encoded characters like `%27` (apostrophes).
- **Citation extraction** (MediaWiki) - Inline citations appear as `[^N]` footnote markers in the markdown output. Use the `citation` parameter to retrieve specific numbered references. Author-date shorthand (e.g. "Simpson 2003, p. 8") is automatically resolved against the article's bibliography via `#CITEREF` links.
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
- **Citation retrieval** - `citation=4` or `citation=[1,3,8]` returns specific numbered references from MediaWiki pages, with bibliography resolution for author-date shorthand

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
| `desktop` (default) | Claude Desktop | `kagi_search`, `kagi_summarize`, `web_fetch_js`, `web_fetch_direct`, `web_fetch_sections` |
| `code` | Claude Code | `KagiSearch`, `KagiSummarize`, `WebFetchJS`, `WebFetchDirect`, `WebFetchSections` |

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
