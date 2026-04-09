# Privacy Policy

**parkour-mcp** is an MCP server that runs locally on your device. It does not
collect, store, or transmit any personal data beyond what is described below.

## Third-party services

When you use parkour-mcp's tools, your queries are sent to the following
third-party APIs over HTTPS:

| Service | What is sent | When |
|---|---|---|
| **Kagi** (kagi.com) | Search queries, URLs for summarization | KagiSearch, KagiSummarize tools |
| **Semantic Scholar** (semanticscholar.org) | Search queries, paper IDs | SemanticScholar tool |
| **arXiv** (arxiv.org) | Search queries, paper IDs | ArXiv tool |
| **GitHub** (api.github.com) | Search queries, repo/issue/PR identifiers | GitHub tool |
| **IETF** (rfc-editor.org, datatracker.ietf.org) | RFC numbers, search queries | IETF tool |
| **deps.dev** (deps.dev) | Package names, versions | Packages tool |
| **Wikipedia/MediaWiki** (various) | Page titles, API queries | WebFetchExact (fast path) |
| **Reddit** (old.reddit.com) | Thread URLs | WebFetchExact (fast path) |
| **CrossRef** (api.crossref.org) | DOIs | DOI resolution |
| **Arbitrary web URLs** | The URL you provide | WebFetchExact, WebFetchJS, WebFetchSections |

Each service's own privacy policy governs how they handle your requests.

## API keys

API keys you provide (Kagi, GitHub, Semantic Scholar) are:

- Stored locally by Claude Desktop using your operating system's secure storage
- Sent only to their respective services (Kagi key to Kagi, GitHub token to
  GitHub, etc.)
- Never sent to Anthropic or any other third party

## Contact email

If you provide a contact email (`MCP_CONTACT_EMAIL`), it is included in the
`User-Agent` header sent to API endpoints. This enables better rate limits
with services like CrossRef that offer a "polite pool" for identified clients.

## Data retention

- **No server-side storage.** parkour-mcp does not operate a backend service.
- **No telemetry or analytics.** No usage data is collected or transmitted.
- **Session-scoped only.** The research shelf (citation tracker) is held in
  memory and does not persist beyond the MCP session. When Claude Desktop
  restarts, the shelf is empty.

## Network access

parkour-mcp makes outbound HTTPS requests to the services listed above.
Requests to private, loopback, and link-local IP addresses are blocked by
default (SSRF protection). This can be overridden by setting
`MCP_ALLOW_PRIVATE_IPS=1` in your environment.

## Changes

This policy may be updated as new tools or integrations are added. Changes
will be reflected in this file in the project repository.
