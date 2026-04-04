"""GitHub API integration for repository, issue, PR, and code lookup.

Provides a standalone GitHub tool with search, issue/PR viewing, file
fetching, and repo metadata — plus URL detection for the fast-path chain
in fetch_direct.py and fetch_js.py.

Uses httpx directly (no wrapper library) for consistency with the rest of
the codebase.  Authentication is optional: unauthenticated requests get
60 req/hr on the core API; a GITHUB_TOKEN bumps that to 5000/hr.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Optional

import httpx
from pydantic import Field

from .common import _API_USER_AGENT, _FETCH_HEADERS, RateLimiter
from .markdown import _build_frontmatter, _fence_content, _TRUST_ADVISORY

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_CONFIG_PATH = Path.home() / ".config" / "kagi" / "github_token"
_GITHUB_API_VERSION = "2022-11-28"

_NO_TOKEN_MSG = (
    "Rate limited. Unauthenticated GitHub API allows only 60 requests/hour.\n"
    "Set GITHUB_TOKEN env var or create ~/.config/kagi/github_token "
    "with a personal access token.\n"
    "No special scopes needed for public repos. "
    "See: https://github.com/settings/tokens"
)

# ---------------------------------------------------------------------------
# Rate limiter — 1 request per second baseline politeness
# ---------------------------------------------------------------------------

_github_limiter = RateLimiter(1.0)

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def _get_github_token() -> str:
    """Load GitHub token from env var, config file, or return empty string."""
    if key := os.environ.get("GITHUB_TOKEN"):
        return key
    if GITHUB_CONFIG_PATH.exists():
        return GITHUB_CONFIG_PATH.read_text().strip()
    return ""


def _github_headers(accept: str = "application/vnd.github.v3+json") -> dict:
    """Build request headers with optional auth and API version pinning."""
    headers = {
        "User-Agent": _API_USER_AGENT,
        "Accept": accept,
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
    }
    token = _get_github_token()
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


# ---------------------------------------------------------------------------
# Rate limit tracking (per-resource: "core" vs "search")
# ---------------------------------------------------------------------------

@dataclass
class _GitHubRateLimit:
    """Parsed rate limit state from GitHub response headers."""
    limit: int = 0
    remaining: int = 0
    reset_epoch: float = 0.0
    resource: str = "core"

    @classmethod
    def from_headers(cls, headers: httpx.Headers) -> Optional["_GitHubRateLimit"]:
        """Parse X-RateLimit-* headers, or None if absent."""
        try:
            return cls(
                limit=int(headers["x-ratelimit-limit"]),
                remaining=int(headers["x-ratelimit-remaining"]),
                reset_epoch=float(headers["x-ratelimit-reset"]),
                resource=headers.get("x-ratelimit-resource", "core"),
            )
        except (KeyError, ValueError):
            return None


# Per-resource rate limit state (updated after each request)
_rate_limits: dict[str, _GitHubRateLimit] = {}


# ---------------------------------------------------------------------------
# Core HTTP request
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_RETRY_BACKOFF = 1.0  # seconds; constant backoff (gh CLI pattern)

# Link header pagination regex
_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')


def _next_page_url(link_header: Optional[str]) -> Optional[str]:
    """Extract the 'next' URL from a Link response header."""
    if not link_header:
        return None
    for match in _LINK_RE.finditer(link_header):
        if match.group(2) == "next":
            return match.group(1)
    return None


async def _github_request(
    method: str,
    path: str,
    params: Optional[dict] = None,
    accept: str = "application/vnd.github.v3+json",
) -> dict | list | str:
    """Core HTTP call to the GitHub API.

    Returns parsed JSON (dict or list) on success, or an error string on
    failure.  Retries on 5xx with constant backoff (max 3 attempts).
    Tracks rate limit state per-resource from response headers.
    """
    url = f"https://api.github.com{path}" if path.startswith("/") else path

    for attempt in range(_MAX_RETRIES + 1):
        await _github_limiter.wait()

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(
                    method, url,
                    headers=_github_headers(accept),
                    params=params,
                )
        except httpx.TimeoutException:
            return "Error: GitHub API request timed out."
        except httpx.RequestError as e:
            return f"Error: GitHub API request failed - {type(e).__name__}"

        # Track rate limit state
        rl = _GitHubRateLimit.from_headers(response.headers)
        if rl:
            _rate_limits[rl.resource] = rl

        # Success
        if response.status_code in (200, 201):
            return response.json()
        if response.status_code == 204:
            return {}

        # 404
        if response.status_code == 404:
            return "Error: Not found on GitHub."

        # 403 — rate limit or auth/scope issue
        if response.status_code == 403:
            if rl and rl.remaining == 0:
                if _get_github_token():
                    return f"Error: Rate limited. Resets at epoch {int(rl.reset_epoch)}."
                return f"Error: {_NO_TOKEN_MSG}"
            # Scope issue — check accepted vs actual scopes
            needed = response.headers.get("x-accepted-oauth-scopes", "")
            has = response.headers.get("x-oauth-scopes", "")
            if needed:
                return (
                    f"Error: HTTP 403 — this endpoint requires the "
                    f"'{needed}' scope. Your token has: '{has or 'none'}'."
                )
            return "Error: HTTP 403 Forbidden."

        # 422 — validation error (bad search query, etc.)
        if response.status_code == 422:
            try:
                body = response.json()
                errors = body.get("errors", [])
                if errors:
                    msg = errors[0].get("message", body.get("message", ""))
                    return f"Error: Invalid request — {msg}"
                return f"Error: Invalid request — {body.get('message', 'Unprocessable Entity')}"
            except Exception:
                return "Error: HTTP 422 Unprocessable Entity."

        # 5xx — retry
        if response.status_code >= 500:
            if attempt < _MAX_RETRIES:
                logger.info(
                    "GitHub %d on %s, retry %d after %.1fs",
                    response.status_code, path, attempt + 1, _RETRY_BACKOFF,
                )
                await asyncio.sleep(_RETRY_BACKOFF)
                continue
            return f"Error: GitHub API returned HTTP {response.status_code}."

        # Other 4xx
        return f"Error: GitHub API returned HTTP {response.status_code}."

    return "Error: GitHub API request failed."


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

@dataclass
class GitHubUrlMatch:
    """Parsed components of a GitHub URL."""
    kind: str  # "blob", "tree", "issue", "pull", "discussion", "repo", "gist"
    owner: str = ""
    repo: str = ""
    number: Optional[int] = None
    ref: Optional[str] = None
    path: Optional[str] = None
    gist_id: Optional[str] = None


# Main github.com URL — captures owner, repo, and remaining path
_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/(.*))?$",
    re.IGNORECASE,
)

# Sub-path patterns (applied after main pattern matches)
_BLOB_RE = re.compile(r"^blob/([^/]+)/(.+)$")
_TREE_RE = re.compile(r"^tree/([^/]+)/(.+)$")
_ISSUE_RE = re.compile(r"^issues/(\d+)(?:/.*)?$")
_PULL_RE = re.compile(r"^pull/(\d+)(?:/.*)?$")
_DISCUSSION_RE = re.compile(r"^discussions/(\d+)(?:/.*)?$")

# Gist URL
_GIST_URL_RE = re.compile(
    r"https?://gist\.github\.com/(?:[^/]+/)?([0-9a-f]+)",
    re.IGNORECASE,
)


def _detect_github_url(url: str) -> Optional[GitHubUrlMatch]:
    """Parse a GitHub URL into its components, or return None.

    Supports github.com repo URLs (blob, tree, issues, pull, discussions,
    repo root) and gist.github.com URLs.

    Discussion detection is gated on authentication — returns None for
    discussion URLs when no GITHUB_TOKEN is configured, allowing them to
    fall through to generic HTTP fetch rather than silently failing.
    """
    # Gist
    m = _GIST_URL_RE.match(url)
    if m:
        return GitHubUrlMatch(kind="gist", gist_id=m.group(1))

    # Main github.com
    m = _GITHUB_URL_RE.match(url)
    if not m:
        return None

    owner, repo, rest = m.group(1), m.group(2), m.group(3) or ""

    # Skip non-repo paths (e.g. github.com/settings, github.com/orgs/...)
    if owner in ("settings", "orgs", "marketplace", "explore",
                 "topics", "trending", "collections", "sponsors",
                 "features", "security", "enterprise", "pricing",
                 "login", "signup", "join", "new"):
        return None

    if not rest:
        return GitHubUrlMatch(kind="repo", owner=owner, repo=repo)

    # Blob (source file)
    bm = _BLOB_RE.match(rest)
    if bm:
        return GitHubUrlMatch(
            kind="blob", owner=owner, repo=repo,
            ref=bm.group(1), path=bm.group(2),
        )

    # Tree (directory)
    tm = _TREE_RE.match(rest)
    if tm:
        return GitHubUrlMatch(
            kind="tree", owner=owner, repo=repo,
            ref=tm.group(1), path=tm.group(2),
        )

    # Issue
    im = _ISSUE_RE.match(rest)
    if im:
        return GitHubUrlMatch(
            kind="issue", owner=owner, repo=repo,
            number=int(im.group(1)),
        )

    # Pull request
    pm = _PULL_RE.match(rest)
    if pm:
        return GitHubUrlMatch(
            kind="pull", owner=owner, repo=repo,
            number=int(pm.group(1)),
        )

    # Discussion (auth-gated)
    dm = _DISCUSSION_RE.match(rest)
    if dm and _get_github_token():
        return GitHubUrlMatch(
            kind="discussion", owner=owner, repo=repo,
            number=int(dm.group(1)),
        )

    return None


# ---------------------------------------------------------------------------
# Rate limit warning for frontmatter
# ---------------------------------------------------------------------------

def _rate_limit_warning() -> Optional[str]:
    """Return a warning string if rate limit is low and unauthenticated."""
    if _get_github_token():
        return None
    core = _rate_limits.get("core")
    if core and core.remaining < 10:
        return (
            f"GitHub API rate limit low ({core.remaining}/{core.limit} remaining). "
            "Set GITHUB_TOKEN for 5000 req/hr."
        )
    return None


# ---------------------------------------------------------------------------
# Source code sectionization (tree-sitter + CodeSplitter)
# ---------------------------------------------------------------------------

# Extension → (tree-sitter module name, language function name)
# Grammar packages are optional deps — missing grammars fall back gracefully.
_EXT_TO_GRAMMAR: dict[str, tuple[str, str]] = {
    ".py": ("tree_sitter_python", "language"),
    ".js": ("tree_sitter_javascript", "language"),
    ".jsx": ("tree_sitter_javascript", "language"),
    ".ts": ("tree_sitter_typescript", "language_typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".go": ("tree_sitter_go", "language"),
    ".rs": ("tree_sitter_rust", "language"),
    ".java": ("tree_sitter_java", "language"),
    ".c": ("tree_sitter_c", "language"),
    ".h": ("tree_sitter_c", "language"),
    ".cpp": ("tree_sitter_cpp", "language"),
    ".hpp": ("tree_sitter_cpp", "language"),
    ".cc": ("tree_sitter_cpp", "language"),
    ".kt": ("tree_sitter_kotlin", "language"),
    ".scala": ("tree_sitter_scala", "language"),
}

# Node types that represent definitions, per tree-sitter grammar.
# Each entry: grammar module → list of (node_type, name_field) tuples.
_DEFINITION_TYPES: dict[str, list[tuple[str, str]]] = {
    "tree_sitter_python": [
        ("function_definition", "name"),
        ("class_definition", "name"),
    ],
    "tree_sitter_javascript": [
        ("function_declaration", "name"),
        ("class_declaration", "name"),
        ("method_definition", "name"),
        ("lexical_declaration", "name"),  # const/let
    ],
    "tree_sitter_typescript": [
        ("function_declaration", "name"),
        ("class_declaration", "name"),
        ("method_definition", "name"),
        ("lexical_declaration", "name"),
        ("interface_declaration", "name"),
        ("type_alias_declaration", "name"),
    ],
    "tree_sitter_go": [
        ("function_declaration", "name"),
        ("method_declaration", "name"),
        ("type_declaration", "name"),
    ],
    "tree_sitter_rust": [
        ("function_item", "name"),
        ("struct_item", "name"),
        ("enum_item", "name"),
        ("trait_item", "name"),
        ("impl_item", "type"),
    ],
    "tree_sitter_java": [
        ("class_declaration", "name"),
        ("interface_declaration", "name"),
        ("method_declaration", "name"),
        ("enum_declaration", "name"),
    ],
    "tree_sitter_c": [
        ("function_definition", "declarator"),
        ("struct_specifier", "name"),
        ("enum_specifier", "name"),
    ],
    "tree_sitter_cpp": [
        ("function_definition", "declarator"),
        ("class_specifier", "name"),
        ("struct_specifier", "name"),
        ("enum_specifier", "name"),
    ],
    "tree_sitter_kotlin": [
        ("function_declaration", "name"),
        ("class_declaration", "name"),
        ("object_declaration", "name"),
    ],
    "tree_sitter_scala": [
        ("function_definition", "name"),
        ("class_definition", "name"),
        ("object_definition", "name"),
        ("trait_definition", "name"),
    ],
}

# Python docstring: first expression_statement > string in body
# JSDoc/Javadoc: comment node immediately preceding the definition
_DOC_COMMENT_GRAMMARS = {
    "tree_sitter_python",  # uses body-first-string pattern
}
_PRECEDING_COMMENT_GRAMMARS = {
    "tree_sitter_javascript", "tree_sitter_typescript",
    "tree_sitter_java", "tree_sitter_go",
    "tree_sitter_rust", "tree_sitter_c", "tree_sitter_cpp",
    "tree_sitter_kotlin", "tree_sitter_scala",
}


@dataclass
class CodeDefinition:
    """A function, class, or type definition extracted from source code."""
    kind: str       # "function", "class", "struct", etc.
    name: str
    start_line: int
    end_line: int
    depth: int      # nesting level (0 = top-level)
    docstring: Optional[str] = None  # first line only


def _get_code_splitter(ext: str):
    """Return a CodeSplitter for the given file extension, or None.

    Lazily imports the tree-sitter grammar package. Returns None if the
    grammar is not installed.
    """
    from semantic_text_splitter import CodeSplitter
    import importlib

    grammar_info = _EXT_TO_GRAMMAR.get(ext)
    if not grammar_info:
        return None

    module_name, func_name = grammar_info
    try:
        mod = importlib.import_module(module_name)
        lang_fn = getattr(mod, func_name)
        return CodeSplitter(lang_fn(), (100, 1000))
    except (ImportError, AttributeError):
        logger.debug("tree-sitter grammar %s not available", module_name)
        return None


def _extract_name_text(node, source: bytes) -> str:
    """Extract the name text from a definition node, handling nested declarators."""
    if node is None:
        return "?"
    # C/C++ declarators can be nested: function_declarator → identifier
    if node.type in ("function_declarator", "pointer_declarator"):
        for child in node.children:
            if child.type == "identifier":
                return source[child.start_byte:child.end_byte].decode()
        # Recurse one level for pointer_declarator → function_declarator → identifier
        for child in node.children:
            result = _extract_name_text(child, source)
            if result != "?":
                return result
    return source[node.start_byte:node.end_byte].decode()


def _extract_python_docstring(node, source: bytes) -> Optional[str]:
    """Extract first line of a Python docstring from a function/class body."""
    body = node.child_by_field_name("body")
    if not body:
        return None
    for child in body.children:
        if child.type == "expression_statement":
            for sc in child.children:
                if sc.type == "string":
                    raw = source[sc.start_byte:sc.end_byte].decode()
                    # Strip triple quotes and whitespace
                    content = raw.strip("\"'").strip()
                    return content.split("\n")[0].strip() if content else None
            break  # only check first statement
    return None


def _extract_preceding_comment(node, source: bytes) -> Optional[str]:
    """Extract first line of a doc comment preceding a definition node."""
    # Walk backward through siblings to find a comment
    prev = node.prev_sibling
    if prev is None:
        return None

    if prev.type == "comment":
        text = source[prev.start_byte:prev.end_byte].decode().strip()
        # Strip comment markers: //, /*, */, /**, ///, //!
        for prefix in ("/**", "///", "//!", "/*", "//"):
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        text = text.rstrip("*/").strip()
        return text.split("\n")[0].strip() if text else None

    return None


def extract_code_definitions(
    source: str, ext: str,
) -> list[CodeDefinition]:
    """Extract function/class definitions with docstrings from source code.

    Uses tree-sitter for AST parsing. Returns an empty list if the grammar
    for the given file extension is not installed.
    """
    import importlib

    grammar_info = _EXT_TO_GRAMMAR.get(ext)
    if not grammar_info:
        return []

    module_name, func_name = grammar_info
    try:
        import tree_sitter
        mod = importlib.import_module(module_name)
        lang_fn = getattr(mod, func_name)
        lang = tree_sitter.Language(lang_fn())
        parser = tree_sitter.Parser(lang)
    except (ImportError, AttributeError):
        return []

    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)

    def_types = _DEFINITION_TYPES.get(module_name, [])
    type_map = {node_type: name_field for node_type, name_field in def_types}
    uses_body_docstring = module_name in _DOC_COMMENT_GRAMMARS
    uses_preceding_comment = module_name in _PRECEDING_COMMENT_GRAMMARS

    results: list[CodeDefinition] = []

    def walk(node, depth: int = 0):
        if node.type in type_map:
            name_field = type_map[node.type]
            name_node = node.child_by_field_name(name_field)
            name = _extract_name_text(name_node, source_bytes)

            # Kind: strip _definition, _declaration, _item, _specifier suffixes
            kind = node.type
            for suffix in ("_definition", "_declaration", "_item", "_specifier"):
                kind = kind.replace(suffix, "")

            # Docstring extraction
            docstring = None
            if uses_body_docstring:
                docstring = _extract_python_docstring(node, source_bytes)
            elif uses_preceding_comment:
                docstring = _extract_preceding_comment(node, source_bytes)

            results.append(CodeDefinition(
                kind=kind,
                name=name,
                start_line=node.start_point.row + 1,
                end_line=node.end_point.row + 1,
                depth=depth,
                docstring=docstring,
            ))

        child_depth = depth + (1 if node.type in type_map else 0)
        for child in node.children:
            walk(child, child_depth)

    walk(tree.root_node)
    return results


def _sectionize_code(source: str, ext: str) -> Optional[list[tuple[int, str]]]:
    """Split source code at AST boundaries for presplit cache storage.

    Returns (char_offset, chunk_text) tuples suitable for
    _PageCache.store(presplit=...), or None if the grammar is unavailable.
    """
    splitter = _get_code_splitter(ext)
    if splitter is None:
        return None

    try:
        return splitter.chunk_indices(source)
    except Exception:
        logger.debug("CodeSplitter failed for extension %s", ext, exc_info=True)
        return None


def format_code_sections(defs: list[CodeDefinition]) -> str:
    """Format extracted definitions as a section listing for web_fetch_sections."""
    if not defs:
        return ""
    lines = []
    for d in defs:
        indent = "  " * d.depth
        doc = f" — {d.docstring}" if d.docstring else ""
        lines.append(f"{indent}- {d.kind} {d.name} (L{d.start_line}-{d.end_line}){doc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Query parsing helpers
# ---------------------------------------------------------------------------

def _parse_owner_repo_number(query: str) -> tuple[str, str, int] | str:
    """Parse 'owner/repo#number' → (owner, repo, number) or error string."""
    m = re.match(r"^([^/]+)/([^#]+)#(\d+)$", query.strip())
    if not m:
        return f"Error: Expected 'owner/repo#number', got '{query}'."
    return m.group(1), m.group(2), int(m.group(3))


def _parse_owner_repo(query: str) -> tuple[str, str] | str:
    """Parse 'owner/repo' → (owner, repo) or error string."""
    parts = query.strip().strip("/").split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return f"Error: Expected 'owner/repo', got '{query}'."
    return parts[0], parts[1]


def _parse_owner_repo_path(query: str) -> tuple[str, str, str] | str:
    """Parse 'owner/repo/path/to/file' → (owner, repo, path) or error string."""
    parts = query.strip().strip("/").split("/", 2)
    if len(parts) < 3 or not parts[0] or not parts[1] or not parts[2]:
        return f"Error: Expected 'owner/repo/path', got '{query}'."
    return parts[0], parts[1], parts[2]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_relative_time(iso_date: str) -> str:
    """Format an ISO 8601 timestamp as a relative time string."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            m = seconds // 60
            return f"{m}m ago"
        if seconds < 86400:
            h = seconds // 3600
            return f"{h}h ago"
        days = seconds // 86400
        if days < 30:
            return f"{days}d ago"
        if days < 365:
            return f"{days // 30}mo ago"
        return f"{days // 365}y ago"
    except Exception:
        return iso_date


def _fmt_labels(labels: list[dict]) -> str:
    """Format label list as comma-separated names."""
    return ", ".join(lb["name"] for lb in labels) if labels else ""


def _fm_base(source: str, api: str = "GitHub") -> dict:
    """Build common frontmatter entries."""
    entries: dict = {"source": source, "api": api}
    warning = _rate_limit_warning()
    if warning:
        entries["warning"] = warning
    return entries


# ---------------------------------------------------------------------------
# Action: search_issues
# ---------------------------------------------------------------------------

async def _action_search_issues(
    query: str, limit: int, page: int,
) -> str:
    """Search issues and pull requests across GitHub."""
    result = await _github_request(
        "GET", "/search/issues",
        params={"q": query, "per_page": str(min(limit, 100)), "page": str(page)},
    )
    if isinstance(result, str):
        return result

    items = result.get("items", [])
    total = result.get("total_count", 0)
    incomplete = result.get("incomplete_results", False)

    fm_entries = _fm_base(f"https://github.com/search?q={query}&type=issues")
    fm_entries["total_results"] = total
    fm_entries["showing"] = f"{len(items)} (page {page})"
    if incomplete:
        fm_entries["note"] = "Results may be incomplete (search timed out)"
    fm = _build_frontmatter(fm_entries)

    if not items:
        return fm + "\n\nNo results found."

    lines = []
    for item in items:
        num = item["number"]
        title = item["title"]
        state = item["state"]
        repo_name = item.get("repository_url", "").rsplit("/", 2)[-2:]
        repo_str = "/".join(repo_name) if len(repo_name) == 2 else ""
        labels = _fmt_labels(item.get("labels", []))
        updated = _fmt_relative_time(item.get("updated_at", ""))
        kind = "PR" if "pull_request" in item else "Issue"
        label_str = f" [{labels}]" if labels else ""

        lines.append(f"- **{repo_str}#{num}** ({kind}, {state}) {title}{label_str} — {updated}")

    body = "\n".join(lines)
    return fm + "\n\n" + _fence_content(body)


# ---------------------------------------------------------------------------
# Action: search_code
# ---------------------------------------------------------------------------

async def _action_search_code(
    query: str, limit: int, page: int,
) -> str:
    """Search code across GitHub repositories."""
    result = await _github_request(
        "GET", "/search/code",
        params={"q": query, "per_page": str(min(limit, 100)), "page": str(page)},
        accept="application/vnd.github.text-match+json",
    )
    if isinstance(result, str):
        return result

    items = result.get("items", [])
    total = result.get("total_count", 0)

    fm_entries = _fm_base(f"https://github.com/search?q={query}&type=code")
    fm_entries["total_results"] = total
    fm_entries["showing"] = f"{len(items)} (page {page})"
    fm = _build_frontmatter(fm_entries)

    if not items:
        return fm + "\n\nNo results found."

    lines = []
    for item in items:
        repo = item.get("repository", {}).get("full_name", "")
        path = item.get("path", "")
        # Extract text match fragments if available
        matches = item.get("text_matches", [])
        fragments = []
        for tm in matches[:3]:
            frag = tm.get("fragment", "").strip().replace("\n", " ")
            if frag:
                fragments.append(frag[:120])

        lines.append(f"**{repo}** `{path}`")
        for frag in fragments:
            lines.append(f"> {frag}")
        lines.append("")

    body = "\n".join(lines).rstrip()
    return fm + "\n\n" + _fence_content(body)


# ---------------------------------------------------------------------------
# Action: repo
# ---------------------------------------------------------------------------

async def _action_repo(query: str) -> str:
    """Fetch repository metadata and README."""
    parsed = _parse_owner_repo(query)
    if isinstance(parsed, str):
        return parsed
    owner, repo = parsed

    result = await _github_request("GET", f"/repos/{owner}/{repo}")
    if isinstance(result, str):
        return result

    name = result["full_name"]
    desc = result.get("description") or "No description"
    stars = result.get("stargazers_count", 0)
    forks = result.get("forks_count", 0)
    lang = result.get("language") or "—"
    license_info = result.get("license") or {}
    license_name = license_info.get("spdx_id") or "—"
    topics = result.get("topics") or []
    open_issues = result.get("open_issues_count", 0)

    fm_entries = _fm_base(f"https://github.com/{name}")
    fm = _build_frontmatter(fm_entries)

    parts = [
        f"# {name}\n",
        f"**{desc}**\n",
        f"Stars: {stars:,} | Forks: {forks:,} | Open issues: {open_issues:,}",
        f"Language: {lang} | License: {license_name}",
    ]
    if topics:
        parts.append(f"Topics: {', '.join(topics)}")

    # Fetch README
    readme_result = await _github_request(
        "GET", f"/repos/{owner}/{repo}/readme",
        accept="application/vnd.github.raw+json",
    )
    if isinstance(readme_result, str) and not readme_result.startswith("Error"):
        parts.append(f"\n## README\n\n{readme_result}")
    elif isinstance(readme_result, dict):
        # API returned JSON instead of raw — decode content
        import base64
        content = readme_result.get("content", "")
        if content:
            try:
                decoded = base64.b64decode(content).decode("utf-8")
                parts.append(f"\n## README\n\n{decoded}")
            except Exception:
                pass

    body = "\n".join(parts)
    return fm + "\n\n" + _fence_content(body, title=name)


# ---------------------------------------------------------------------------
# Action: tree
# ---------------------------------------------------------------------------

async def _action_tree(
    query: str, ref: Optional[str],
) -> str:
    """Fetch directory listing from a repository."""
    parsed = _parse_owner_repo_path(query)
    if isinstance(parsed, str):
        # Might be owner/repo (root directory)
        parsed2 = _parse_owner_repo(query)
        if isinstance(parsed2, str):
            return parsed
        owner, repo = parsed2
        path = ""
    else:
        owner, repo, path = parsed

    params = {}
    if ref:
        params["ref"] = ref

    api_path = f"/repos/{owner}/{repo}/contents/{path}" if path else f"/repos/{owner}/{repo}/contents"
    result = await _github_request("GET", api_path, params=params)
    if isinstance(result, str):
        return result
    if not isinstance(result, list):
        return "Error: Expected directory listing but got a file. Use the 'file' action instead."

    source = f"https://github.com/{owner}/{repo}"
    if path:
        source += f"/tree/{ref or 'HEAD'}/{path}"
    fm_entries = _fm_base(source)
    fm_entries["entries"] = len(result)
    fm = _build_frontmatter(fm_entries)

    lines = []
    # Sort: directories first, then files
    dirs = sorted([e for e in result if e["type"] == "dir"], key=lambda e: e["name"])
    files = sorted([e for e in result if e["type"] != "dir"], key=lambda e: e["name"])
    for entry in dirs:
        lines.append(f"  dir  {entry['name']}/")
    for entry in files:
        size = entry.get("size", 0)
        if size >= 1024 * 1024:
            size_str = f"{size / (1024 * 1024):.1f}MB"
        elif size >= 1024:
            size_str = f"{size / 1024:.1f}KB"
        else:
            size_str = f"{size}B"
        lines.append(f"  file {entry['name']} ({size_str})")

    title = f"{owner}/{repo}/{path}" if path else f"{owner}/{repo}"
    body = "\n".join(lines)
    return fm + "\n\n" + _fence_content(body, title=title)


# ---------------------------------------------------------------------------
# Action: issue
# ---------------------------------------------------------------------------

async def _action_issue(
    query: str, limit: int, page: int,
) -> str:
    """Fetch an issue with comments."""
    parsed = _parse_owner_repo_number(query)
    if isinstance(parsed, str):
        return parsed
    owner, repo, number = parsed

    # Fetch issue
    result = await _github_request(
        "GET", f"/repos/{owner}/{repo}/issues/{number}",
    )
    if isinstance(result, str):
        return result

    title = result["title"]
    state = result["state"]
    author = result["user"]["login"]
    body = result.get("body") or ""
    created = result.get("created_at", "")
    labels = _fmt_labels(result.get("labels", []))
    comment_count = result.get("comments", 0)
    reactions = result.get("reactions", {})
    association = result.get("author_association", "")

    fm_entries = _fm_base(f"https://github.com/{owner}/{repo}/issues/{number}")
    fm_entries["type"] = "issue"
    fm_entries["state"] = state
    fm_entries["trust"] = _TRUST_ADVISORY
    if comment_count > limit:
        fm_entries["hint"] = f"Showing {limit} of {comment_count} comments. Use page= for more."
    fm = _build_frontmatter(fm_entries)

    # Build issue body
    parts = []
    meta = f"**{owner}/{repo}#{number}** | {state} | {comment_count} comments"
    parts.append(meta)

    assoc_str = f" ({association})" if association and association != "NONE" else ""
    parts.append(f"**@{author}**{assoc_str} — {_fmt_relative_time(created)}")
    if labels:
        parts.append(f"Labels: {labels}")

    # Reaction summary
    reaction_parts = []
    for emoji, key in [
        ("+1", "👍"), ("-1", "👎"), ("laugh", "😄"), ("hooray", "🎉"),
        ("confused", "😕"), ("heart", "❤️"), ("rocket", "🚀"), ("eyes", "👀"),
    ]:
        count = reactions.get(emoji, 0)
        if count:
            reaction_parts.append(f"{key} {count}")
    if reaction_parts:
        parts.append(" ".join(reaction_parts))

    parts.append("")
    if body:
        parts.append(body)

    # Fetch comments
    if comment_count > 0:
        parts.append("\n## Comments\n")
        comments = await _github_request(
            "GET", f"/repos/{owner}/{repo}/issues/{number}/comments",
            params={"per_page": str(min(limit, 100)), "page": str(page)},
        )
        if isinstance(comments, list):
            for c in comments:
                cid = c["id"]
                cauthor = c["user"]["login"]
                cassoc = c.get("author_association", "")
                cbody = c.get("body") or ""
                ccreated = c.get("created_at", "")
                creactions = c.get("reactions", {})

                assoc_tag = f" ({cassoc})" if cassoc and cassoc != "NONE" else ""
                parts.append(f"### ic_{cid}\n")
                parts.append(f"**@{cauthor}**{assoc_tag} — {_fmt_relative_time(ccreated)}")

                # Comment reactions
                cr_parts = []
                for emoji, key in [
                    ("+1", "👍"), ("-1", "👎"), ("laugh", "😄"),
                    ("heart", "❤️"), ("rocket", "🚀"), ("eyes", "👀"),
                ]:
                    count = creactions.get(emoji, 0)
                    if count:
                        cr_parts.append(f"{key} {count}")
                if cr_parts:
                    parts.append(" ".join(cr_parts))

                parts.append("")
                parts.append(cbody)
                parts.append("")

    content = "\n".join(parts)
    return fm + "\n\n" + _fence_content(content, title=title)


# ---------------------------------------------------------------------------
# Action: pull_request
# ---------------------------------------------------------------------------

async def _action_pull_request(
    query: str, limit: int, page: int,
) -> str:
    """Fetch a pull request with diff stats and review comments."""
    parsed = _parse_owner_repo_number(query)
    if isinstance(parsed, str):
        return parsed
    owner, repo, number = parsed

    # Fetch PR
    result = await _github_request(
        "GET", f"/repos/{owner}/{repo}/pulls/{number}",
    )
    if isinstance(result, str):
        return result

    title = result["title"]
    state = result["state"]
    merged = result.get("merged", False)
    author = result["user"]["login"]
    body = result.get("body") or ""
    created = result.get("created_at", "")
    additions = result.get("additions", 0)
    deletions = result.get("deletions", 0)
    changed_files = result.get("changed_files", 0)
    base = result.get("base", {}).get("ref", "")
    head = result.get("head", {}).get("ref", "")
    comment_count = result.get("comments", 0)
    review_comment_count = result.get("review_comments", 0)
    labels = _fmt_labels(result.get("labels", []))
    association = result.get("author_association", "")

    display_state = "merged" if merged else state

    fm_entries = _fm_base(f"https://github.com/{owner}/{repo}/pull/{number}")
    fm_entries["type"] = "pull_request"
    fm_entries["state"] = display_state
    fm_entries["trust"] = _TRUST_ADVISORY
    fm = _build_frontmatter(fm_entries)

    parts = []
    meta = f"**{owner}/{repo}#{number}** | {display_state} | {head} → {base}"
    parts.append(meta)

    assoc_str = f" ({association})" if association and association != "NONE" else ""
    parts.append(f"**@{author}**{assoc_str} — {_fmt_relative_time(created)}")
    if labels:
        parts.append(f"Labels: {labels}")

    parts.append("")
    if body:
        parts.append(body)

    # Diff stat
    parts.append("\n## Diff stat\n")
    parts.append(f"{changed_files} files changed, +{additions}, -{deletions}")

    # Review comments (grouped by file)
    if review_comment_count > 0:
        review_comments = await _github_request(
            "GET", f"/repos/{owner}/{repo}/pulls/{number}/comments",
            params={"per_page": str(min(limit, 100)), "page": str(page)},
        )
        if isinstance(review_comments, list) and review_comments:
            # Group by file path
            by_file: dict[str, list[dict]] = {}
            for rc in review_comments:
                path = rc.get("path", "unknown")
                by_file.setdefault(path, []).append(rc)

            parts.append("\n## Review comments\n")
            for filepath, comments in by_file.items():
                parts.append(f"### {filepath}\n")
                for rc in comments:
                    rcid = rc["id"]
                    rcauthor = rc["user"]["login"]
                    rcbody = rc.get("body") or ""
                    rccreated = rc.get("created_at", "")
                    rcassoc = rc.get("author_association", "")
                    diff_hunk = rc.get("diff_hunk") or ""
                    line = rc.get("line") or rc.get("original_line")
                    in_reply = rc.get("in_reply_to_id")

                    reply_tag = f" (reply to rc_{in_reply})" if in_reply else ""
                    assoc_tag = f" ({rcassoc})" if rcassoc and rcassoc != "NONE" else ""
                    line_tag = f" L{line}" if line else ""

                    parts.append(f"#### rc_{rcid}{reply_tag}\n")
                    parts.append(f"**@{rcauthor}**{assoc_tag}{line_tag} — {_fmt_relative_time(rccreated)}")

                    # Include diff hunk context (trimmed)
                    if diff_hunk and not in_reply:
                        hunk_lines = diff_hunk.strip().split("\n")
                        # Show last few lines of context
                        display_lines = hunk_lines[-6:] if len(hunk_lines) > 6 else hunk_lines
                        parts.append("```diff")
                        parts.extend(display_lines)
                        parts.append("```")

                    parts.append("")
                    parts.append(rcbody)
                    parts.append("")

    # Regular comments
    if comment_count > 0:
        parts.append("\n## Comments\n")
        comments = await _github_request(
            "GET", f"/repos/{owner}/{repo}/issues/{number}/comments",
            params={"per_page": str(min(limit, 100)), "page": str(page)},
        )
        if isinstance(comments, list):
            for c in comments:
                cid = c["id"]
                cauthor = c["user"]["login"]
                cbody = c.get("body") or ""
                ccreated = c.get("created_at", "")
                cassoc = c.get("author_association", "")

                assoc_tag = f" ({cassoc})" if cassoc and cassoc != "NONE" else ""
                parts.append(f"### ic_{cid}\n")
                parts.append(f"**@{cauthor}**{assoc_tag} — {_fmt_relative_time(ccreated)}")
                parts.append("")
                parts.append(cbody)
                parts.append("")

    content = "\n".join(parts)
    return fm + "\n\n" + _fence_content(content, title=title)


# ---------------------------------------------------------------------------
# Action: file
# ---------------------------------------------------------------------------

async def _action_file(
    query: str, ref: Optional[str], max_tokens: int = 5000,
) -> str:
    """Fetch raw file content from a repository."""
    parsed = _parse_owner_repo_path(query)
    if isinstance(parsed, str):
        return parsed
    owner, repo, path = parsed

    # Fetch via raw.githubusercontent.com (no API rate limit, no base64)
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref or 'HEAD'}/{path}"
    headers = dict(_FETCH_HEADERS)
    token = _get_github_token()
    if token:
        headers["Authorization"] = f"token {token}"

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(raw_url, headers=headers)
    except httpx.TimeoutException:
        return f"Error: Request timed out for {raw_url}"
    except httpx.RequestError as e:
        return f"Error: Request failed - {type(e).__name__}"

    if response.status_code == 404:
        if not token:
            return "Error: File not found. If this is a private repo, set GITHUB_TOKEN."
        return "Error: File not found."
    if response.status_code != 200:
        return f"Error: HTTP {response.status_code} for {raw_url}"

    content = response.text

    # Binary detection
    if "\x00" in content[:8192]:
        return f"Error: Binary file ({path}). Use the GitHub web UI to view this file."

    # Detect language from extension for code fencing
    ext = Path(path).suffix.lower() if "." in path else ""
    lang_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".jsx": "javascript", ".tsx": "typescript",
        ".go": "go", ".rs": "rust", ".rb": "ruby",
        ".java": "java", ".kt": "kotlin", ".scala": "scala",
        ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp",
        ".sh": "bash", ".bash": "bash", ".zsh": "zsh",
        ".yaml": "yaml", ".yml": "yaml", ".json": "json",
        ".toml": "toml", ".xml": "xml", ".html": "html", ".css": "css",
        ".md": "markdown", ".sql": "sql", ".r": "r",
        ".swift": "swift", ".m": "objectivec",
    }
    lang = lang_map.get(ext, "")

    # Truncate if needed
    char_budget = max_tokens * 4
    truncated = False
    if len(content) > char_budget:
        content = content[:char_budget]
        truncated = True

    source = f"https://github.com/{owner}/{repo}/blob/{ref or 'HEAD'}/{path}"
    fm_entries = _fm_base(source, api="GitHub (raw)")
    if lang:
        fm_entries["language"] = lang
    if truncated:
        fm_entries["truncated"] = f"Content truncated to ~{max_tokens} tokens"
    fm = _build_frontmatter(fm_entries)

    # Add line numbers
    lines = content.split("\n")
    width = len(str(len(lines)))
    numbered = "\n".join(f"{i + 1:>{width}} | {line}" for i, line in enumerate(lines))

    fenced_code = f"```{lang}\n{numbered}\n```"
    return fm + "\n\n" + _fence_content(fenced_code, title=path)


# ---------------------------------------------------------------------------
# Main tool dispatch
# ---------------------------------------------------------------------------

_VALID_ACTIONS = (
    "search_issues", "search_code", "repo", "tree",
    "issue", "pull_request", "file",
)


async def github(
    action: Annotated[str, Field(
        description=(
            "The operation to perform. "
            "search_issues: search issues/PRs by query (supports GitHub qualifiers like repo:, is:, label:). "
            "search_code: search code across GitHub (supports qualifiers like repo:, language:, path:). "
            "issue: get issue details + comments by owner/repo#number. "
            "pull_request: get PR details + review comments + diff stat by owner/repo#number. "
            "file: get file content from a repo (use ref= for branch/tag). "
            "repo: get repo metadata + README. "
            "tree: get directory listing."
        ),
    )],
    query: Annotated[str, Field(
        description=(
            "For search_issues/search_code: search query with optional GitHub qualifiers. "
            "For issue/pull_request: 'owner/repo#number' (e.g. 'facebook/react#1234'). "
            "For file/tree: 'owner/repo/path' (e.g. 'facebook/react/packages/react/src/React.js'). "
            "For repo: 'owner/repo' (e.g. 'facebook/react')."
        ),
    )],
    ref: Annotated[Optional[str], Field(
        description="Git ref (branch, tag, or commit SHA) for file/tree actions. Defaults to the repo's default branch.",
    )] = None,
    limit: Annotated[int, Field(
        description="Maximum results to return (default 10, max 100).",
    )] = 10,
    page: Annotated[int, Field(
        description="Page number for pagination (1-indexed).",
    )] = 1,
) -> str:
    """Search and retrieve code, issues, and pull requests from GitHub."""
    action = action.strip().lower()

    if action not in _VALID_ACTIONS:
        return (
            f"Error: Unknown action '{action}'. "
            f"Valid actions: {', '.join(_VALID_ACTIONS)}"
        )

    # Auto-detect URLs in query for issue/PR actions
    if action in ("issue", "pull_request"):
        match = _detect_github_url(query.strip())
        if match and match.kind in ("issue", "pull") and match.number:
            query = f"{match.owner}/{match.repo}#{match.number}"

    if action == "search_issues":
        return await _action_search_issues(query, limit, page)
    if action == "search_code":
        return await _action_search_code(query, limit, page)
    if action == "repo":
        return await _action_repo(query)
    if action == "tree":
        return await _action_tree(query, ref)
    if action == "issue":
        return await _action_issue(query, limit, page)
    if action == "pull_request":
        return await _action_pull_request(query, limit, page)
    if action == "file":
        return await _action_file(query, ref)

    return f"Error: Action '{action}' not implemented."
