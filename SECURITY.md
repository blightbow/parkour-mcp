# Security Policy

## Reporting a vulnerability

Report security vulnerabilities through GitHub's private vulnerability
reporting:

**<https://github.com/blightbow/parkour-mcp/security/advisories/new>**

Please do not open a public issue for a security report. Public issues are
the right place for everything else, including hardening suggestions that
do not describe an exploitable defect.

### What to expect

| Stage | Target |
|---|---|
| Acknowledgement | 7 days |
| Initial assessment | 7 days |
| Fix or documented mitigation | 3 days |

parkour-mcp is maintained by one person. These are good-faith targets
barring real life emergencies. Agentic coding with review passes keep the
post-acknowledgment windows tight, but human oversight is still needed for
triage, implementation, and adversarial agent reviewing.

Coordinated disclosure is preferred. Where a fix ships, the advisory is
published with credit to the reporter unless anonymity is requested.

### What helps

- The tool and action involved, and the arguments passed to it.
- A minimal reproduction. A short script beats a description.
- What an attacker gains. This matters more than severity scoring.
- Version (`parkour-mcp --version`) and platform.

## Supported versions

Only the latest released version receives fixes. There are no maintained
release branches, and patches are not backported.

## Threat model

parkour-mcp fetches content from the open web and returns it to an LLM.
Reports are most useful when framed against how that is actually used.

### In scope

- **Server-side request forgery.** Any path that reaches a network
  destination the caller should not be able to select, including via
  redirects, DNS behavior, or URL parsing differences between the
  validation layer and the transport.
- **Local resource disclosure.** Any path that reads a local file, an
  environment variable, or a credential and returns it in tool output.
- **Content-fence escape.** Fetched content is wrapped in fence markers
  with per-line prefixes so an LLM can distinguish it from trusted
  metadata. Content that escapes the fence, or reaches the frontmatter
  block, is a vulnerability.
- **Credential handling.** Leakage of API tokens read from the
  environment or from `~/.config/parkour/`.
- **Supply chain.** Issues in the published PyPI distribution, the `.mcpb`
  bundle, or the release workflow.

### Out of scope

- **Prompt injection contained inside the fence.** parkour marks untrusted
  content; it does not sanitize it. Fetched pages will contain text trying
  to steer the model, by design, because that is what the open web
  contains. The fence is a provenance signal for the calling agent, not a
  guarantee that the agent will honor it. A payload that stays inside the
  fence and is correctly marked is working as intended. A payload that
  escapes the fence, forges the `source` field, or otherwise reaches the
  trusted frontmatter zone is in scope.
- **Fetching a private address with `MCP_ALLOW_PRIVATE_IPS=1` set.** That
  variable exists to opt into local network crawling and is documented as
  such.
- **Rate limits, quotas, or terms-of-service questions** for the upstream
  APIs parkour talks to.
- **Vulnerabilities in an upstream API itself.** Report those to the
  operator. If parkour amplifies one, that part is in scope.

## Testing guidance

Test against your own infrastructure. Do not use parkour to probe third
parties, and do not exercise reports against the live upstream APIs in a
way that would burn a shared quota or trip abuse detection.

The test suite runs fully offline against mocked endpoints
(`uv run pytest`), and live tests are opt-in (`uv run pytest -m live`).
Building a reproduction on the offline path is usually possible and is
preferred.
