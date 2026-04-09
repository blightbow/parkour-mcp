# Parkour MCP

Parkour is a content exploration tool that enables a LLM to surface and explore high signal, unsummarized web content. It makes extensive use of clean APIs and Markdown conversion to enable targeted content extraction and knowledge synthesis. While primarily designed for Claude Code and Claude Desktop, it should be adaptable to most agentic toolchain needs.

API integrations:
- Kagi Search
- Kagi Summarize
- Semantic Scholar
- arXiv
- IETF
- deps.dev (library package lookups)
- GitHub
- MediaWiki
- Reddit (old.reddit.com JSON API)

## Why Parkour?
What sets Parkour apart from the standard approaches are three principles:

### Tool calls should participate in steering the LLM.

We design our tool outputs with the LLM in mind. The LLM is our user, and if our user has a good experience the humans behind them have an even better experience.

The standout feature of Parkour is a frontmatter tool envelope that intelligently advises the LLM and steers its decisionmaking. This is a fancy way of saying "our tool payloads are prefaced with instructional YAML frontmatter". It's a technique that is simple on its face but deceptively powerful.

- Frontmatter leverages the LLM's existing document training to prime the next tool decision. Envelope fields are relevant to the activity at hand, and are positioned for actionability.
- The key:value pairs of YAML are self-documenting for both humans and LLMs, giving us a free out of band channel that doesn't require a new MCP standard.
- The tool outputs proactively steer the LLM toward sources of high signal and away from dead ends. This adds a small amount of tool latency for the background web calls we perform, but every unnecessary tool call we avoid pays dividends on that investment.
- We maintain a design document to ensure that the frontmatter envelope is used in a consistent fashion across tools. You can read more about it [here](https://github.com/blightbow/parkour-mcp/blob/main/docs/frontmatter-standard.md).

Parkour also intercepts requests for content from websites with robust first-party APIs.

| Claude: Please provide some examples here. Thanks!

### Enable LLMs to be slightly more responsible with citations.

LLMs lack training to be responsible scholars. They would be better at tracking citations than humans _if_ they were instructed to do so, but most instructions for compacting context aren't designed to preserve these at all -- to say nothing about gathering those citations as they work.

While we can't do anything about the training, we _can_ make sure the MCP server passively accumulates citations for actively browsed Github projects, research papers, and IETF publications. We can't force the LLM to do anything **with** those citations, but we do give it a little reminder nudge in the tool payload every time one of those citations are accumulated. This increases the odds that the LLM has access to that information when it's time to write documentation, which will reduce the odds of it being forgotten or hallucinated. Is the solution perfect? No, but we think it's a step in the right direction. Researchers will also find it legitimately useful. We're very open to feature suggestions on how this can be improved for academics.

We also do some errand running that the average user won't think of doing, let alone a LLM.

- We don't let the LLM hit the GUIs of Github repos, hard stop. If a LLM asks for a file from the repo, it gets the raw without the extra tool call.
- Background DOI lookups. When's the last time someone who wasn't an academic clicked on your Github repo's CITATION.cff? 
- **Retraction** lookups. Frontmatter tells the LLM up front that the knowledge well is poisoned before it drinks deeply.
- Does ArXiv have a HTML version of a paper? We tell the LLM it's missing before it burns a tool call on the 404, and point it toward the snippets tool. If the HTML version exists, the LLM is told up front where to look for it.

### Don't enshittify the web more than necessary.

Modern LLM solutions have converged on agentic toolchains that pair cheaper text analysis LLMs (Haiku) with larger models that excel at reasoning (Opus), but sometimes the finer details get lost in this process. In a worst case scenario, sometimes these details get hallucinated during the summarization process...**including the attributed authors of research papers and software**. Considering that the very frontier of LLM capabilities live and die by the quality of research papers, this is unacceptable to us.

The best way to minimize the damage of LLM enshittification is to make it easy for their pilots to do the right thing. By providing a tool that synthesizes better data while also making a best effort to steer the LLM toward being a good netizen, we reduce the "litter" left in the wake of irresponsible LLM use. The caveat is that the quality of outputs _must_ create the incentive to use the tool on their own merit, otherwise this MCP server would simply be yet another doomed recycling initiative.

There is no magic wand for making LLMs go away, so let's build LLM toolkits that make things better for more than just the venture capitalists.

### Don't summarize when you can enrich.

Why is LLM summarization so popular?

- Security: **unsummarized** LLM content creates a broader prompt injection surface.
- Brevity: Fewer tokens are used, which in turn improves the LLM's attention focus on the problem you are solving.
- Cost: Fewer tokens in context mean fewer tokens that you're billed for. Poorly chosen walls of text are cost amplifiers that sit in context until it scrolls out or gets compacted.
- Laziness: The traditional problem with "AI". The technology moves too rapidly to be concerned with the long term effects. Today's "good enough" doesn't concern itself with what's good for the world ten years from now.

Our counterpoint:

- The attack surface for prompt injection can be responsibly mitigated, and summarizers aren't entirely exempt from being prompt injection surfaces.
- Hallucination is much more likely to present itself when the details in context are vague, which is an artifact of both first-tier summarization _and_ recursive LLM summarization. Hallucinations pollute the web at best, and at worst force corrective action.
- Corrective action is a hidden cost, both in terms of human labor and the energy labor of LLMs being instructed to redo the work.
- Search results are already very polluted with SEO optimized AI slop, a non-zero percentage of which will make it into the next round of training for frontier LLM models.

QED:
  - Summarization enshittifies the web through hallucination and model collapse.
  - LLMs summarizing LLMs are the path to madness. Frontier model providers avoid this at the training layer, but _they aren't solving it for the agentic tool calls powered by those models_.

The token problem is largely solved by enabling the LLM to take a more procedural approach to content surfing. The web_fetch_sections tool extracts a table of contents style outline of section labels, giving the model an immediate understanding of the webpage structure and its relevance. Rather than summarizing the page or blindly fetching it all at once, the LLM can now make an educated decision about what to extract. It can also decide early on that the page isn't useful without having to pay the price of a summary, helping us to edge close to net-zero with our output tokens compared to traditional approaches.

| Claude: Insert an example here. Thanks!

We would be remiss to ignore the prompt injection surface that we are broadening with our approach. To safeguard against it, we employ a content fencing technique similar to what is recommended in Microsoft's Spotlight paper. We take this a step further by adding a `trust` hint in the tool envelope that instructs the LLM not to trust the fenced content. We defend against truncation by labeling the fence as untrusted content at both the entrance and the exit.

| Claude: Insert an example here. Thanks!

It's not perfect, but it's the best technique that exists at the moment. As more advanced techniques emerge we will continue to update our approach.


## Claude: This is where most of the examples used to be. Consider the examples you've already provided above, then insert the rest here. Thanks!

## Usage

```bash
# Default (desktop profile, snake_case naming)
uv run parkour-mcp

# Claude Code profile (PascalCase naming)
uv run parkour-mcp --profile code

# Show help
uv run parkour-mcp --help
```

## Profile Options

The `--profile` argument adjusts tool names and descriptions for the target client. Each profile tailors the descriptions to explain how the MCP tools complement that client's built-in capabilities — for example, both profiles describe `WebFetchExact` as fetching through the user's device instead of proxying through Anthropic's servers, using precise content extraction and clean first-party APIs instead of summarization. The `code` profile emphasizes extracting specific details that summarization would discard, while the `desktop` profile notes it as a fallback when `web_fetch` is rejected with PERMISSIONS_ERROR:

| Profile | Target | Tool Names |
|---------|--------|------------|
| `desktop` (default) | Claude Desktop | `kagi_search`, `kagi_summarize`, `web_fetch_js`, `web_fetch_exact`, `web_fetch_sections`, `semantic_scholar`, `arxiv`, `github`, `ietf`, `packages` |
| `code` | Claude Code | `KagiSearch`, `KagiSummarize`, `WebFetchJS`, `WebFetchExact`, `WebFetchSections`, `SemanticScholar`, `ArXiv`, `GitHub`, `IETF`, `Packages` |

The `desktop` profile (snake_case) is the default as it aligns with MCP ecosystem conventions. Claude Code's PascalCase naming is the exception, not the norm.

## Tools

All tool names vary by profile (see [Profile Options](#profile-options)).

Tool Name          | Claude Code Tool Name | Description
-------------------|-----------------------|------------
kagi_search        | KagiSearch            | Search the web using Kagi.com's curated, SEO-resistant index
web_fetch_sections | WebFetchSections      | List section headings and anchor slugs for a web page (for targeted extraction)
web_fetch_exact    | WebFetchExact         | Fetch a Markdown rendered version of a HTML webpage (also returns raw content for common content types: JSON, XML, plain text)
web_fetch_js       | WebFetchJS            | Use Playwright to render a headless version of the website in Markdown (extracting documents from a JavaScript cage)
semantic_scholar   | SemanticScholar       | Search and retrieve academic paper data from Semantic Scholar (search, paper details, references, authors, body text snippets)
arxiv              | ArXiv                 | Search and retrieve academic papers from arXiv (search with field-prefix syntax, paper details, category browsing)
github             | GitHub                | Search and retrieve code, issues, pull requests, commits, and comparisons from GitHub (7 actions: search_issues, search_code, repo, tree, issue, pull_request, file)
ietf               | IETF                  | Search and retrieve IETF RFCs and Internet-Drafts (4 actions: rfc, search, draft, subseries)
packages           | Packages              | Inspect software packages across 7 language ecosystems via deps.dev (5 actions: package, version, dependencies, project, advisory)
kagi_summarize     | KagiSummarize         | Summarize URLs or text (supports PDFs, YouTube, audio)

For detailed capabilities, worked examples, and integration-specific behavior, see the [Guide](docs/guide.md).

## Setup

### Configuration

#### Claude Code

Install globally via CLI:
```
claude mcp add parkour-mcp -- uv --directory /path/to/parkour-mcp run parkour-mcp --profile code
```

Or add it directly to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "parkour-mcp": {
      "command": "uv",
      "args": ["--directory", "/path/to/parkour-mcp", "run", "parkour-mcp", "--profile", "code"]
    }
  }
}
```

#### Claude Desktop (macOS)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "parkour-mcp": {
      "command": "uv",
      "args": ["--directory", "/path/to/parkour-mcp", "run", "parkour-mcp", "--profile", "desktop"]
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
mkdir -p ~/.config/parkour
echo "your-api-key" > ~/.config/parkour/kagi_api_key
```

Get your API key at https://kagi.com/settings?p=api

### Semantic Scholar API Key (optional)

The SemanticScholar tool works without an API key but shares a global rate limit pool. For your own rate limit, get a free key and configure it:

```bash
# Option 1: Environment variable
export S2_API_KEY="your-api-key"

# Option 2: Config file
mkdir -p ~/.config/parkour
echo "your-api-key" > ~/.config/parkour/s2_api_key
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

### GitHub Token (optional, for GitHub tool)

The GitHub tool works without authentication but shares a global 60 req/hr rate limit. For 5,000 req/hr with your own limit, configure a personal access token:

```bash
# Option 1: Environment variable
export GITHUB_TOKEN="ghp_your-token-here"

# Option 2: Config file
mkdir -p ~/.config/parkour
echo "ghp_your-token-here" > ~/.config/parkour/github_token
```

No special scopes are needed for public repos. For private repos, create a [fine-grained PAT](https://github.com/settings/tokens?type=beta) with `Contents: read` permission on the target repos.

### Tree-sitter Grammars (optional, for code definition trees)

The GitHub tool uses [tree-sitter](https://tree-sitter.github.io/) grammars for AST-aware code splitting and definition extraction when viewing source files. With a grammar installed, `web_fetch_sections` on a GitHub source file returns the code definition tree (classes, functions, methods with line ranges and docstrings), and BM25 search splits at function/class boundaries instead of fixed-size chunks. Without a grammar, the tool falls back to line-based splitting gracefully — everything still works, just with less precise boundaries.

Install all included grammars via the `grammars` optional dependency group:

```bash
uv sync --extra grammars
```

To persist grammars across `uv sync` when running as an MCP server, add `--extra grammars` to your MCP configuration:

```json
{
  "mcpServers": {
    "parkour-mcp": {
      "command": "uv",
      "args": ["--directory", "/path/to/parkour-mcp", "run", "--extra", "grammars", "parkour-mcp", "--profile", "code"]
    }
  }
}
```

**What each grammar enables:**

| Grammar | Extensions | Definition extraction |
|---------|-----------|----------------------|
| `tree-sitter-python` | `.py` | functions, classes, methods + docstrings |
| `tree-sitter-javascript` | `.js`, `.jsx` | functions, classes, methods + JSDoc comments |
| `tree-sitter-typescript` | `.ts`, `.tsx` | functions, classes, interfaces + JSDoc comments |
| `tree-sitter-go` | `.go` | functions, methods, structs, interfaces + preceding comments |
| `tree-sitter-rust` | `.rs` | functions, structs, enums, traits, impls + doc comments |
| `tree-sitter-c` | `.c`, `.h` | functions, structs, enums, typedefs + preceding comments |
| `tree-sitter-cpp` | `.cpp`, `.hpp`, `.cc` | functions, classes, structs, namespaces + preceding comments |
| `tree-sitter-java` | `.java` | classes, interfaces, methods + Javadoc comments |
| `tree-sitter-kotlin` | `.kt` | functions, classes + preceding comments |
| `tree-sitter-scala` | `.scala` | functions, classes, objects, traits + preceding comments |

Adding support for a new language requires a registry entry in `github.py` (`_EXT_TO_GRAMMAR` and `_DEFINITION_TYPES`) plus the corresponding `tree-sitter-{language}` package in the `grammars` extra. Grammars that are installed but not in the registry are ignored; grammars in the registry but not installed fall back gracefully to line-based splitting.

### SSRF Protection

By default, the fetch tools block requests to private, loopback, reserved, and link-local IP addresses (both IPv4 and IPv6). This prevents the MCP server from being used to probe internal networks or cloud metadata endpoints (e.g. `169.254.169.254`).

To allow fetching from local network resources (e.g. internal documentation servers):

```bash
export MCP_ALLOW_PRIVATE_IPS=1
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

Other than that, we have a shared goal in making the web less enshittified. LLMs hallucinate more when they are forced to draw conclusions from their trained data, and often reach conclusions based on data that is already months old. This MCP server is designed to help LLMs investigate the actual research texts and verify sources.

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

Because you are hitting S2's global rate limit. All anonymous API calls for S2 share the same rate limit pool, and the calls made through this tool are no different.

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
