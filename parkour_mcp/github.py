"""GitHub API integration for repository, issue, PR, and code lookup.

Provides a standalone GitHub tool with search, issue/PR viewing, file
fetching, and repo metadata — plus URL detection for the fast-path chain
in fetch_direct.py and fetch_js.py.

Uses httpx directly (no wrapper library) for consistency with the rest of
the codebase.  Authentication is optional: unauthenticated requests get
60 req/hr on the core API; a GITHUB_TOKEN bumps that to 5000/hr.
"""

import asyncio
import base64
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Optional
from urllib.parse import quote as _urlquote_raw

import httpx
from pydantic import Field

from .common import _API_USER_AGENT, _FETCH_HEADERS, RateLimiter, tool_name
from .markdown import (
    FMEntries,
    _append_frontmatter_entry,
    _apply_semantic_truncation,
    _build_frontmatter,
    _fence_content,
    _TRUST_ADVISORY,
)
from .scorecard import fetch_overall as _fetch_scorecard_overall

logger = logging.getLogger(__name__)


def _urlquote(s: str) -> str:
    """URL-encode a GitHub search query, preserving : and / for readability."""
    return _urlquote_raw(s, safe=":/")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_CONFIG_PATH = Path.home() / ".config" / "parkour" / "github_token"
_GITHUB_API_VERSION = "2022-11-28"

_NO_TOKEN_MSG = (
    "Rate limited. Unauthenticated GitHub API allows only 60 requests/hour.\n"
    "Set GITHUB_TOKEN env var or create ~/.config/parkour/github_token "
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


_github_token_cache: str | None = None


def _get_github_token() -> str:
    """Load GitHub token from env var, config file, or return empty string.

    Result is cached after the first call — the token does not change
    during a server session.
    """
    global _github_token_cache
    if _github_token_cache is not None:
        return _github_token_cache
    from .common import clean_env
    if key := clean_env("GITHUB_TOKEN"):
        _github_token_cache = key
        return key
    if GITHUB_CONFIG_PATH.exists():
        _github_token_cache = GITHUB_CONFIG_PATH.read_text().strip()
        return _github_token_cache
    _github_token_cache = ""
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
            # Try JSON parse; fall back to text for raw content endpoints
            # (application/vnd.github.raw+json contains "json" in the
            # content-type but the body is raw text, not JSON)
            try:
                return response.json()
            except Exception:
                return response.text
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
    kind: str  # "blob", "tree", "issue", "pull", "discussion", "repo", "gist", "wiki", "commit", "compare"
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
_WIKI_RE = re.compile(r"^wiki(?:/(.+))?$")
_COMMIT_RE = re.compile(r"^commit/([0-9a-f]{7,40})$", re.IGNORECASE)
_COMPARE_RE = re.compile(r"^compare/(.+)$")
_BLAME_RE = re.compile(r"^blame/([^/]+)/(.+)$")

# Gist URL
_GIST_URL_RE = re.compile(
    r"https?://gist\.github\.com/(?:[^/]+/)?([0-9a-f]+)",
    re.IGNORECASE,
)

# raw.githubusercontent.com — maps to blob kind
# Format: raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}
_RAW_GH_RE = re.compile(
    r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$",
    re.IGNORECASE,
)
_ORG_RE = re.compile(
    r"https?://github\.com/([a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?)/?$",
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
    # raw.githubusercontent.com → blob
    m = _RAW_GH_RE.match(url)
    if m:
        return GitHubUrlMatch(
            kind="blob", owner=m.group(1), repo=m.group(2),
            ref=m.group(3), path=m.group(4),
        )

    # Gist
    m = _GIST_URL_RE.match(url)
    if m:
        return GitHubUrlMatch(kind="gist", gist_id=m.group(1))

    # Org/user profile: github.com/{name} (single path segment, no repo)
    om = _ORG_RE.match(url)
    if om:
        name = om.group(1).lower()
        # Exclude GitHub system pages
        if name not in (
            "settings", "orgs", "marketplace", "explore",
            "topics", "trending", "collections", "sponsors",
            "features", "security", "enterprise", "pricing",
            "login", "signup", "join", "new", "about",
            "readme", "codespaces", "copilot", "issues",
            "pulls", "discussions", "notifications",
        ):
            return GitHubUrlMatch(kind="org", owner=om.group(1))

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

    # Wiki
    wm = _WIKI_RE.match(rest)
    if wm:
        return GitHubUrlMatch(
            kind="wiki", owner=owner, repo=repo,
            path=wm.group(1),  # page name or None for wiki root
        )

    # Commit
    cm = _COMMIT_RE.match(rest)
    if cm:
        return GitHubUrlMatch(
            kind="commit", owner=owner, repo=repo,
            ref=cm.group(1),
        )

    # Compare
    cpm = _COMPARE_RE.match(rest)
    if cpm:
        return GitHubUrlMatch(
            kind="compare", owner=owner, repo=repo,
            path=cpm.group(1),  # "base...head" spec
        )

    # Blame (detected so we can give a clean error)
    blm = _BLAME_RE.match(rest)
    if blm:
        return GitHubUrlMatch(
            kind="blame", owner=owner, repo=repo,
            ref=blm.group(1), path=blm.group(2),
        )

    # Releases — preserve sub-path for tag dispatch
    if rest == "releases" or rest.startswith("releases/"):
        sub = rest[len("releases/"):] if "/" in rest else None
        return GitHubUrlMatch(kind="releases", owner=owner, repo=repo, path=sub)

    # Paths that produce broken HTML if we fall through — detect and error cleanly
    for prefix in ("actions", "projects"):
        if rest == prefix or rest.startswith(prefix + "/"):
            return GitHubUrlMatch(kind=prefix, owner=owner, repo=repo)

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
                return source[child.start_byte:child.end_byte].decode(errors="replace")
        # Recurse one level for pointer_declarator → function_declarator → identifier
        for child in node.children:
            result = _extract_name_text(child, source)
            if result != "?":
                return result
    return source[node.start_byte:node.end_byte].decode(errors="replace")


def _extract_python_docstring(node, source: bytes) -> Optional[str]:
    """Extract first line of a Python docstring from a function/class body."""
    body = node.child_by_field_name("body")
    if not body:
        return None
    for child in body.children:
        if child.type == "expression_statement":
            for sc in child.children:
                if sc.type == "string":
                    raw = source[sc.start_byte:sc.end_byte].decode(errors="replace")
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
        text = source[prev.start_byte:prev.end_byte].decode(errors="replace").strip()
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

    # Iterative DFS to avoid stack overflow on deeply nested ASTs.
    # Stack entries: (node, definition_depth).
    stack: list[tuple] = [(tree.root_node, 0)]
    while stack:
        node, depth = stack.pop()

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
        # Reverse so left-to-right children are processed in order
        for child in reversed(node.children):
            stack.append((child, child_depth))

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


# Circuit-breaker threshold: any single line longer than this is treated as
# pathological and trips ``_plaintext_presplit``.  Real log/code lines are
# typically <1 KB; the longest legitimate single-line content we see in the
# wild is minified JavaScript bundles, which can approach but rarely exceed
# 1 MB.  Anything beyond is almost certainly adversarial or a data dump —
# dropping cache support for that file is preferable to letting
# MarkdownSplitter's char-level fallback spend multi-second CPU bursts on
# unstructured single-line content (see issue #6).
_MAX_PLAINTEXT_LINE_CHARS = 1_000_000

# Target size of each presplit chunk for plaintext content.  Matches the
# midpoint of ``MarkdownSplitter((1600, 2000))`` so BM25 index behavior is
# consistent across structured and unstructured cache entries.
_PLAINTEXT_CHUNK_CHARS = 1800


def _plaintext_presplit(
    source: str,
    chunk_chars: int = _PLAINTEXT_CHUNK_CHARS,
    max_line_chars: int = _MAX_PLAINTEXT_LINE_CHARS,
) -> Optional[list[tuple[int, str]]]:
    """Line-oriented presplit for plaintext blobs with no tree-sitter grammar.

    Groups consecutive lines into chunks up to ``chunk_chars`` in length,
    always splitting on line boundaries so BM25 slices stay readable.
    Returns (char_offset, chunk_text) tuples suitable for
    _PageCache.store(presplit=...).

    Circuit breaker: if any single line exceeds ``max_line_chars``, the
    content is treated as pathological (typical vector: a multi-MiB data
    dump emitted as one line) and the function returns ``None``.  Callers
    should then skip cache population entirely — MarkdownSplitter's
    char-level fallback on unbounded single-line content is the DoS path
    filed as issue #6, and returning None avoids routing into it.
    """
    chunks: list[tuple[int, str]] = []
    n = len(source)
    pos = 0
    chunk_start = 0
    chunk_buf: list[str] = []
    chunk_size = 0

    while pos < n:
        nl = source.find("\n", pos)
        line_end = n if nl == -1 else nl + 1  # include the newline
        line_len = line_end - pos

        if line_len > max_line_chars:
            return None  # circuit breaker — see docstring

        if chunk_size > 0 and chunk_size + line_len > chunk_chars:
            chunks.append((chunk_start, "".join(chunk_buf)))
            chunk_buf = []
            chunk_size = 0
            chunk_start = pos

        chunk_buf.append(source[pos:line_end])
        chunk_size += line_len
        pos = line_end

    if chunk_buf:
        chunks.append((chunk_start, "".join(chunk_buf)))

    return chunks


def _blob_presplit(source: str, ext: str) -> Optional[list[tuple[int, str]]]:
    """Presplit a GitHub blob for ``_page_cache.store(presplit=...)``.

    Tries AST-aware splitting via ``_sectionize_code`` first.  Falls back to
    ``_plaintext_presplit`` for files without a tree-sitter grammar (.txt,
    .log, .csv, etc.).  Returns ``None`` only when both fail, which happens
    for adversarial unstructured content — callers treat that as
    "skip cache, preserve formatted output" so the MarkdownSplitter
    char-level fallback is never invoked on such inputs.
    """
    presplit = _sectionize_code(source, ext)
    if presplit is not None:
        return presplit
    return _plaintext_presplit(source)


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
# Comment-boundary splitting for BM25 indexing
# ---------------------------------------------------------------------------
# Matches issue comment headings (### ic_...) and PR file/review headings
# (### filepath, #### rc_...).  Same approach as Reddit's _split_by_comments.
_GH_COMMENT_HEADING_RE = re.compile(r"^(#{2,6}) (?:ic_|rc_|\S+\.\S+)", re.MULTILINE)


def _split_github_comments(markdown: str) -> list[tuple[int, str]]:
    """Split GitHub issue/PR markdown at comment boundaries for BM25 indexing.

    The issue/PR body (everything before the first comment or file heading)
    becomes slice 0.  Each subsequent heading and its content becomes its
    own slice.  This produces one BM25-indexed slice per comment/file section,
    enabling ``search=`` and ``section=`` on cached GitHub content.

    Returns ``[(char_offset, chunk_text), ...]`` suitable for
    ``_PageCache.store(presplit=...)``.
    """
    splits = list(_GH_COMMENT_HEADING_RE.finditer(markdown))

    if not splits:
        return [(0, markdown)]

    chunks: list[tuple[int, str]] = []

    first_offset = splits[0].start()
    if first_offset > 0:
        chunks.append((0, markdown[:first_offset].rstrip()))

    for i, match in enumerate(splits):
        start = match.start()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(markdown)
        chunks.append((start, markdown[start:end].rstrip()))

    return chunks


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


_REACTION_EMOJIS = [
    ("+1", "👍"), ("-1", "👎"), ("laugh", "😄"), ("hooray", "🎉"),
    ("confused", "😕"), ("heart", "❤️"), ("rocket", "🚀"), ("eyes", "👀"),
]


def _fmt_reactions(reactions: dict) -> str:
    """Format reaction counts as emoji+count pairs."""
    parts = []
    for api_key, emoji in _REACTION_EMOJIS:
        if count := reactions.get(api_key, 0):
            parts.append(f"{emoji} {count}")
    return " ".join(parts)


def _fm_base(source: str, api: str = "GitHub") -> FMEntries:
    """Build common frontmatter entries.

    Returns ``FMEntries`` so multi-contributor keys downstream compose
    cleanly — any caller that later appends a ``hint`` or ``warning``
    stacks on top of the rate-limit warning we seed here.
    """
    entries = FMEntries({"source": source, "api": api})
    entries.append("warning", _rate_limit_warning())
    return entries


# ---------------------------------------------------------------------------
# Helpers: search qualifier extraction
# ---------------------------------------------------------------------------

# repo:owner/name — with or without quotes
_RE_REPO_QUAL = re.compile(r'repo:(?:"([^"]+)"|(\S+))')
# label:name — with or without quotes
_RE_LABEL_QUAL = re.compile(r'label:(?:"([^"]+)"|(\S+))')


async def _label_hint_for_empty_search(query: str) -> Optional[str]:
    """When an issue search returns 0 results and uses label: + repo:
    qualifiers, fetch the repo's labels and return a hint string listing
    them.  Returns None if the qualifiers are absent or the label fetch
    fails."""
    repo_m = _RE_REPO_QUAL.search(query)
    label_m = _RE_LABEL_QUAL.search(query)
    if not repo_m or not label_m:
        return None

    repo = repo_m.group(1) or repo_m.group(2)
    asked = label_m.group(1) or label_m.group(2)

    labels_result = await _github_request(
        "GET", f"/repos/{repo}/labels",
        params={"per_page": "100"},
    )
    if isinstance(labels_result, str) or not isinstance(labels_result, list):
        return None

    names = sorted(lb["name"] for lb in labels_result if "name" in lb)
    if not names:
        return None

    return (
        f"label:{asked} matched nothing — "
        f"{repo} uses: {', '.join(names)}"
    )


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
    assert isinstance(result, dict)

    items = result.get("items", [])
    total = result.get("total_count", 0)
    incomplete = result.get("incomplete_results", False)

    fm_entries = _fm_base(
        f"https://github.com/search?q={_urlquote(query)}&type=issues",
    )
    fm_entries["total_results"] = total
    fm_entries["showing"] = f"{len(items)} (page {page})"
    if incomplete:
        fm_entries.append("note", "Results may be incomplete (search timed out)")

    if not items:
        # When a label: qualifier produced zero results against a single
        # repo, fetch the repo's actual labels so the agent can retry
        # with a corrected name instead of guessing.
        fm_entries.append("hint", await _label_hint_for_empty_search(query))
        fm = _build_frontmatter(fm_entries)
        return fm + "\n\nNo results found."

    fm = _build_frontmatter(fm_entries)

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
    assert isinstance(result, dict)

    items = result.get("items", [])
    total = result.get("total_count", 0)

    fm_entries = _fm_base(f"https://github.com/search?q={_urlquote(query)}&type=code")
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
# Action: search_repos
# ---------------------------------------------------------------------------

async def _action_search_repos(
    query: str, limit: int, page: int,
) -> str:
    """Search repositories across GitHub."""
    result = await _github_request(
        "GET", "/search/repositories",
        params={"q": query, "per_page": str(min(limit, 100)), "page": str(page)},
    )
    if isinstance(result, str):
        return result
    assert isinstance(result, dict)

    items = result.get("items", [])
    total = result.get("total_count", 0)
    incomplete = result.get("incomplete_results", False)

    fm_entries = _fm_base(f"https://github.com/search?q={_urlquote(query)}&type=repositories")
    fm_entries["total_results"] = total
    fm_entries["showing"] = f"{len(items)} (page {page})"
    if incomplete:
        fm_entries.append("note", "Results may be incomplete (search timed out)")
    fm = _build_frontmatter(fm_entries)

    if not items:
        return fm + "\n\nNo results found."

    lines = []
    for item in items:
        full_name = item.get("full_name", "")
        desc = item.get("description") or ""
        stars = item.get("stargazers_count", 0)
        lang = item.get("language") or ""
        updated = _fmt_relative_time(item.get("updated_at", ""))
        topics = item.get("topics", [])
        license_info = item.get("license") or {}
        license_name = license_info.get("spdx_id") or ""

        meta_parts = []
        if stars:
            meta_parts.append(f"\u2605{stars:,}")
        if lang:
            meta_parts.append(lang)
        if license_name and license_name != "NOASSERTION":
            meta_parts.append(license_name)
        meta_parts.append(updated)
        meta = " \u00b7 ".join(meta_parts)

        lines.append(f"- **{full_name}** — {desc}")
        lines.append(f"  {meta}")
        if topics:
            lines.append(f"  Topics: {', '.join(topics[:8])}")

    body = "\n".join(lines)
    return fm + "\n\n" + _fence_content(body)


# ---------------------------------------------------------------------------
# Action: repo
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Repo metadata cache — shared across slow-churning .github/ file fetches
# ---------------------------------------------------------------------------
#
# Several repo-level configuration files (CITATION.cff,
# .github/ISSUE_TEMPLATE/ listings, .github/ISSUE_TEMPLATE/config.yml) are
# fetched per repo action and change rarely within a session. Routing them
# through a single LRU cache means: (a) the second `repo` action for the
# same owner/repo hits cache for all of them, (b) negative results (404s)
# are remembered so we don't re-pay the miss, and (c) concurrent fetches
# for the same key coalesce into one network call via the async lock.

_REPO_METADATA_CACHE_MAX = 64
_repo_metadata_cache: "OrderedDict[str, Any]" = OrderedDict()
_repo_metadata_cache_lock = asyncio.Lock()


def _reset_repo_metadata_cache() -> None:
    """Clear the repo metadata cache. Intended for test teardown."""
    _repo_metadata_cache.clear()


async def _cached_repo_fetch(
    cache_key: str,
    fetcher: Callable[[], Awaitable[Any]],
) -> Any:
    """Cache-through wrapper for repo metadata file fetches.

    Calls ``fetcher()`` on miss, stores the result (including ``None``
    for negative caching), and returns the cached value on hit. Async-safe:
    the lock is held across the fetch so concurrent callers for the same
    key coalesce into a single network request.
    """
    async with _repo_metadata_cache_lock:
        if cache_key in _repo_metadata_cache:
            _repo_metadata_cache.move_to_end(cache_key)
            return _repo_metadata_cache[cache_key]

        value = await fetcher()
        _repo_metadata_cache[cache_key] = value
        if len(_repo_metadata_cache) > _REPO_METADATA_CACHE_MAX:
            _repo_metadata_cache.popitem(last=False)
        return value


# ---------------------------------------------------------------------------
# CITATION.cff parsing and shelf integration
# ---------------------------------------------------------------------------

async def _fetch_citation_cff(
    owner: str, repo: str, default_branch: str,
) -> Optional[dict]:
    """Fetch and parse CITATION.cff from the repo root. Returns parsed YAML or None."""
    import yaml

    raw_url = (
        f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/CITATION.cff"
    )

    async def _do_fetch() -> Optional[dict]:
        headers = {"User-Agent": _API_USER_AGENT}
        token = _get_github_token()
        if token:
            headers["Authorization"] = f"token {token}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(raw_url, headers=headers)
            if resp.status_code != 200:
                return None
            return yaml.safe_load(resp.text)
        except Exception:
            return None

    return await _cached_repo_fetch(raw_url, _do_fetch)


# ---------------------------------------------------------------------------
# Issue template probe (.github/ISSUE_TEMPLATE/)
# ---------------------------------------------------------------------------

_ISSUE_TEMPLATE_DIR = ".github/ISSUE_TEMPLATE"


async def _fetch_issue_template_listing(
    owner: str, repo: str,
) -> Optional[list[dict]]:
    """List entries in ``.github/ISSUE_TEMPLATE/`` on the default branch.

    Returns the contents-API list (one dict per file) if the directory
    exists, or ``None`` if the directory is absent or the response is
    unexpected. Cached.
    """
    api_path = f"/repos/{owner}/{repo}/contents/{_ISSUE_TEMPLATE_DIR}"

    async def _do_fetch() -> Optional[list[dict]]:
        result = await _github_request("GET", api_path)
        if isinstance(result, list):
            return result
        return None

    return await _cached_repo_fetch(api_path, _do_fetch)


async def _fetch_issue_form_yaml(
    owner: str, repo: str, filename: str,
) -> Optional[dict]:
    """Fetch and parse an individual issue form YAML via contents API.

    Returns the parsed header (name, description, title, labels,
    assignees, ...) as a dict, with the ``body`` field dropped — body
    entries are the form's field definitions, too verbose for advisory
    output. Returns ``None`` on any failure. Cached.
    """
    import yaml

    api_path = (
        f"/repos/{owner}/{repo}/contents/{_ISSUE_TEMPLATE_DIR}/{filename}"
    )

    async def _do_fetch() -> Optional[dict]:
        result = await _github_request("GET", api_path)
        if not isinstance(result, dict):
            return None
        encoded = result.get("content")
        if not isinstance(encoded, str):
            return None
        try:
            text = base64.b64decode(encoded).decode("utf-8")
            parsed = yaml.safe_load(text)
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        # Drop the `body` array — it's verbose field definitions, not
        # something we want in the advisory body.
        parsed.pop("body", None)
        return parsed

    return await _cached_repo_fetch(api_path, _do_fetch)


async def _fetch_issue_template_config_yml(
    owner: str, repo: str,
) -> Optional[dict]:
    """Fetch and parse ``.github/ISSUE_TEMPLATE/config.yml`` via contents API.

    Uses the repo's default branch implicitly (contents API resolves it
    server-side), so we never need to know the default branch ourselves.
    Returns the parsed YAML dict, or ``None`` on any failure (404, parse
    error, network error). Cached.
    """
    import yaml

    api_path = f"/repos/{owner}/{repo}/contents/{_ISSUE_TEMPLATE_DIR}/config.yml"

    async def _do_fetch() -> Optional[dict]:
        result = await _github_request("GET", api_path)
        if not isinstance(result, dict):
            return None
        encoded = result.get("content")
        if not isinstance(encoded, str):
            return None
        try:
            text = base64.b64decode(encoded).decode("utf-8")
            parsed = yaml.safe_load(text)
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    return await _cached_repo_fetch(api_path, _do_fetch)


async def _probe_issue_templates(
    owner: str, repo: str,
) -> Optional[dict]:
    """Probe a repo's ``.github/ISSUE_TEMPLATE/`` configuration.

    Returns a structured dict describing custom issue forms, markdown
    templates, and routing configuration (contact links, blank-issues
    toggle), or ``None`` if the repo has nothing worth advising about.

    The returned ``contact_links`` value is raw contributor-supplied data
    and MUST NOT be placed in frontmatter. The body formatter surfaces it
    inside the fenced content zone where the datamarking defense applies.
    """
    listing = await _fetch_issue_template_listing(owner, repo)
    if listing is None:
        return None

    forms: list[str] = []
    markdown_templates: list[str] = []
    has_config = False
    for entry in listing:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "file":
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        if name == "config.yml" or name == "config.yaml":
            has_config = True
            continue
        lower = name.lower()
        if lower.endswith((".yml", ".yaml")):
            forms.append(name)
        elif lower.endswith(".md"):
            markdown_templates.append(name)

    # Fetch config.yml and all form YAMLs concurrently. Each form YAML
    # costs one contents-API request but all are cached, so repeated
    # calls in-session are free.
    fetch_coros: list = []
    fetch_coros.append(
        _fetch_issue_template_config_yml(owner, repo) if has_config else _noop_none()
    )
    for form_name in forms:
        fetch_coros.append(_fetch_issue_form_yaml(owner, repo, form_name))

    results = await asyncio.gather(*fetch_coros)
    config = results[0]
    form_details_raw = results[1:]

    blank_issues_enabled: Optional[bool] = None
    contact_links: Optional[list[dict]] = None
    if isinstance(config, dict):
        raw_flag = config.get("blank_issues_enabled")
        if isinstance(raw_flag, bool):
            blank_issues_enabled = raw_flag
        raw_links = config.get("contact_links")
        if isinstance(raw_links, list):
            contact_links = [cl for cl in raw_links if isinstance(cl, dict)]

    # Build forms_detail: {filename: parsed_header_dict_or_None}.
    # None for forms that failed to parse — the formatter degrades
    # gracefully to filename-only for those.
    forms_detail: dict[str, Optional[dict]] = {}
    for form_name, detail in zip(forms, form_details_raw):
        forms_detail[form_name] = detail if isinstance(detail, dict) else None

    if (
        not forms
        and not markdown_templates
        and blank_issues_enabled is not False
        and not contact_links
    ):
        return None

    return {
        "forms": forms,
        "forms_detail": forms_detail,
        "markdown_templates": markdown_templates,
        "blank_issues_enabled": blank_issues_enabled,
        "contact_links": contact_links,
    }


async def _noop_none() -> None:
    """Awaitable that returns None. Used as a no-op slot in gather()."""
    return None


def _build_issue_template_hint(owner: str, repo: str) -> str:
    """Short steering hint pointing at the ``issue_templates`` action.

    Fired from any repo-scoped action (``repo``, ``issue``,
    ``pull_request``) when ``.github/ISSUE_TEMPLATE/`` exists. Designed
    to be cheap (tens of tokens) — the full advisory lives in the
    dedicated action.
    """
    return (
        f"Custom issue submission flow detected at {owner}/{repo}. "
        f"Use {tool_name('github')} issue_templates action with "
        f"'{owner}/{repo}' for forms, contact links, and filing guidance "
        f"before opening an issue via API."
    )


async def _maybe_issue_template_hint(
    owner: str, repo: str,
) -> Optional[str]:
    """Return the steering hint if the repo has a custom submission flow.

    Wraps the cached directory-listing fetch. Returns ``None`` when the
    repo has no ``.github/ISSUE_TEMPLATE/`` directory (or it is empty).
    Safe to call from multiple actions — cache coalesces the fetch.
    """
    listing = await _fetch_issue_template_listing(owner, repo)
    if not listing:
        return None
    return _build_issue_template_hint(owner, repo)


def _build_issue_template_note(
    probe: Optional[dict], owner: str, repo: str,
) -> Optional[str]:
    """Compose the frontmatter ``note:`` advisory from structural signals.

    Uses counts, boolean flags, and a server-built chooser URL only —
    never contributor-supplied strings (names, URLs, "about" text). Those
    live inside the fenced body section.
    """
    if probe is None:
        return None

    parts: list[str] = []

    form_count = len(probe.get("forms") or [])
    if form_count:
        noun = "custom issue form" if form_count == 1 else "custom issue forms"
        parts.append(f"{form_count} {noun}")

    md_count = len(probe.get("markdown_templates") or [])
    if md_count:
        noun = "markdown template" if md_count == 1 else "markdown templates"
        parts.append(f"{md_count} {noun}")

    if probe.get("blank_issues_enabled") is False:
        parts.append("blank issues disabled")

    contact_links = probe.get("contact_links") or []
    link_count = len(contact_links)
    if link_count:
        noun = "contact link" if link_count == 1 else "contact links"
        parts.append(f"{link_count} {noun} configured")

    if not parts:
        return None

    chooser_url = f"https://github.com/{owner}/{repo}/issues/new/choose"
    summary = "; ".join(parts)
    return (
        f"Issue submissions are structured ({summary}). "
        f"Prefer {chooser_url} over direct API filings."
    )


def _format_issue_submission_section(probe: Optional[dict]) -> Optional[str]:
    """Render the fenced-body ``## Issue Submission`` section.

    Contributor-supplied strings (form filenames, contact link names,
    URLs, and ``about`` text) are rendered here rather than in
    frontmatter. They arrive inside the ``_fence_content`` zone via the
    existing ``parts`` pipeline, inheriting the per-line trust-marking
    datamarking defense.
    """
    if probe is None:
        return None

    forms = probe.get("forms") or []
    forms_detail = probe.get("forms_detail") or {}
    markdown_templates = probe.get("markdown_templates") or []
    contact_links = probe.get("contact_links") or []
    blank_issues_enabled = probe.get("blank_issues_enabled")

    if (
        not forms
        and not markdown_templates
        and not contact_links
        and blank_issues_enabled is not False
    ):
        return None

    lines: list[str] = ["## Issue Submission"]

    if blank_issues_enabled is False:
        lines.append("Blank issues are disabled; maintainers expect a template.")

    if forms:
        lines.append("")
        lines.append("**Custom issue forms:**")
        for name in forms:
            detail = forms_detail.get(name)
            if not isinstance(detail, dict):
                # Malformed or missing form YAML — filename only.
                lines.append(f"- `{name}`")
                continue

            form_title = detail.get("name") or name
            description = detail.get("description") or ""
            title_prefix = detail.get("title") or ""
            labels = detail.get("labels") or []
            assignees = detail.get("assignees") or []

            header = f"- **{form_title}** (`{name}`)"
            if description:
                header += f" — {description}"
            lines.append(header)
            if title_prefix:
                lines.append(f"  Title prefix: `{title_prefix}`")
            if isinstance(labels, list) and labels:
                label_str = ", ".join(str(lb) for lb in labels)
                lines.append(f"  Labels: {label_str}")
            if isinstance(assignees, list) and assignees:
                assignee_str = ", ".join(str(a) for a in assignees)
                lines.append(f"  Assignees: {assignee_str}")

    if markdown_templates:
        lines.append("")
        lines.append("**Markdown templates:**")
        for name in markdown_templates:
            lines.append(f"- {name}")

    if contact_links:
        lines.append("")
        lines.append("**Contact links:**")
        for link in contact_links:
            name = link.get("name") or "(unnamed)"
            url = link.get("url") or ""
            about = link.get("about") or ""
            bits = [f"- **{name}**"]
            if url:
                bits.append(f"— {url}")
            if about:
                bits.append(f"— {about}")
            lines.append(" ".join(bits))

    return "\n".join(lines)


def _parse_citation_cff(cff: dict) -> tuple[Optional[str], str, list[str], Optional[int]]:
    """Extract DOI, title, authors, and year from a CITATION.cff dict.

    Prefers ``preferred-citation`` when present (it references the
    associated paper rather than the software itself). Falls back to
    top-level fields.

    Returns (doi, title, authors_list, year).
    """
    # Prefer the preferred-citation block (references the paper)
    source = cff.get("preferred-citation") or cff

    doi = source.get("doi") or cff.get("doi")
    title = source.get("title") or cff.get("title") or "Untitled"

    # Authors: list of dicts with family-names/given-names
    raw_authors = source.get("authors") or cff.get("authors") or []
    authors = []
    for a in raw_authors:
        family = a.get("family-names", "")
        given = a.get("given-names", "")
        if family and given:
            authors.append(f"{family}, {given}")
        elif family:
            authors.append(family)
        elif a.get("name"):
            authors.append(a["name"])

    # Year from date-released or year field
    year = None
    date_released = source.get("date-released") or cff.get("date-released")
    if date_released:
        try:
            year = int(str(date_released)[:4])
        except (ValueError, TypeError):
            pass
    if year is None:
        raw_year = source.get("year") or cff.get("year")
        if raw_year:
            try:
                year = int(raw_year)
            except (ValueError, TypeError):
                pass

    return doi, title, authors, year


async def _track_repo_on_shelf(
    owner: str,
    repo: str,
    full_name: str,
    description: str,
    repo_data: dict,
    citation_cff: Optional[dict],
) -> Optional[str]:
    """Track a GitHub repo on the research shelf and return the status line.

    If CITATION.cff is present, extracts DOI + metadata from it.
    Otherwise, tracks the repo with a synthetic github: identifier.
    Returns the compact shelf status line for the frontmatter ``shelf:``
    field, or None on any error.
    """
    from .shelf import _track_on_shelf, CitationRecord

    if citation_cff:
        doi, title, authors, year = _parse_citation_cff(citation_cff)
        if not doi:
            # CFF exists but has no DOI — use synthetic key
            doi = f"github:{full_name}"
        result = await _track_on_shelf(CitationRecord(
            doi=doi,
            title=title,
            authors=authors,
            year=year,
            venue=f"GitHub ({full_name})",
            source_tool="github",
        ))
        return result.status_line

    # No CITATION.cff — track with synthetic identifier and repo metadata
    license_info = repo_data.get("license") or {}
    license_name = license_info.get("spdx_id")
    created = repo_data.get("created_at", "")
    year = None
    if len(created) >= 4:
        try:
            year = int(created[:4])
        except (ValueError, TypeError):
            pass

    result = await _track_on_shelf(CitationRecord(
        doi=f"github:{full_name}",
        title=f"{full_name}: {description}" if description else full_name,
        year=year,
        venue="GitHub" + (f" | {license_name}" if license_name else ""),
        source_tool="github",
    ))
    return result.status_line


async def _action_repo(query: str) -> str:
    """Fetch repository metadata and README."""
    parsed = _parse_owner_repo(query)
    if isinstance(parsed, str):
        return parsed
    owner, repo = parsed

    result = await _github_request("GET", f"/repos/{owner}/{repo}")
    if isinstance(result, str):
        return result
    assert isinstance(result, dict)

    name = result["full_name"]
    desc = result.get("description") or "No description"
    stars = result.get("stargazers_count", 0)
    forks = result.get("forks_count", 0)
    lang = result.get("language") or "—"
    license_info = result.get("license") or {}
    license_name = license_info.get("spdx_id") or "—"
    topics = result.get("topics") or []
    open_issues = result.get("open_issues_count", 0)

    default_branch = result.get("default_branch", "main")

    fm_entries = _fm_base(f"https://github.com/{name}")

    parts = [
        f"**{desc}**\n",
        f"Stars: {stars:,} | Forks: {forks:,} | Open issues: {open_issues:,}",
        f"Language: {lang} | License: {license_name}",
    ]
    if topics:
        parts.append(f"Topics: {', '.join(topics)}")

    # Fetch README (as JSON for path metadata), CITATION.cff, the
    # issue-template steering hint, and the OpenSSF Scorecard rating
    # concurrently. The template hint is a cheap cached directory-existence
    # check; the rich payload lives in the dedicated ``issue_templates``
    # action. The Scorecard call degrades silently; many repos are not
    # scanned and the enrichment is strictly additive.
    readme_result, citation_cff, template_hint, scorecard_score = await asyncio.gather(
        _github_request("GET", f"/repos/{owner}/{repo}/readme"),
        _fetch_citation_cff(owner, repo, default_branch),
        _maybe_issue_template_hint(owner, repo),
        _fetch_scorecard_overall(owner, repo),
    )

    readme_text = None
    readme_path = "README.md"  # fallback
    if isinstance(readme_result, dict):
        readme_path = readme_result.get("path", readme_path)
        content = readme_result.get("content", "")
        if content:
            try:
                readme_text = base64.b64decode(content).decode("utf-8")
            except Exception:
                pass
    elif isinstance(readme_result, str) and not readme_result.startswith("Error"):
        # Raw text response (shouldn't happen with default accept, but handle it)
        readme_text = readme_result

    if readme_text:
        # Truncate README to ~2000 tokens — repos are an entry point, not a full read
        truncated, trunc_hint = _apply_semantic_truncation(readme_text, 2000)
        parts.append(f"\n## README\n\n{truncated}")
        if trunc_hint:
            readme_url = f"https://github.com/{owner}/{repo}/blob/{default_branch}/{readme_path}"
            fm_entries.append(
                "hint",
                f"README truncated. Use GitHub file action with "
                f"'{owner}/{repo}/{readme_path}' for full content, "
                f"or {tool_name('web_fetch_direct')}('{readme_url}', section=...) for specific sections.",
            )
    fm_entries.append("hint", template_hint)

    if scorecard_score is not None:
        fm_entries["openssf_scorecard"] = f"{scorecard_score:g}/10"
        fm_entries.append(
            "see_also",
            f"{tool_name('packages')}(action=project, "
            f"query=github.com/{owner}/{repo}) for OpenSSF Scorecard "
            "per-check breakdown",
        )

    fm_entries["shelf"] = await _track_repo_on_shelf(
        owner, repo, name, desc, result, citation_cff,
    )

    fm = _build_frontmatter(fm_entries)
    body = "\n".join(parts)
    return fm + "\n\n" + _fence_content(body, title=name)


# ---------------------------------------------------------------------------
# Action: issue_templates
# ---------------------------------------------------------------------------

async def _action_issue_templates(query: str) -> str:
    """Fetch issue submission configuration for a repository.

    Returns the full advisory — custom forms, markdown templates,
    contact-link routing, and ``blank_issues_enabled`` — so an agent
    preparing to file an issue can pick the right entry point and
    avoid raw API filings against repos that prefer structured
    submissions.
    """
    parsed = _parse_owner_repo(query)
    if isinstance(parsed, str):
        return parsed
    owner, repo = parsed

    probe = await _probe_issue_templates(owner, repo)
    if probe is None:
        return (
            f"No custom issue submission flow configured for {owner}/{repo}. "
            f"The repo does not have a .github/ISSUE_TEMPLATE/ directory, "
            f"so blank issues are allowed. File via the GitHub web UI "
            f"or API at https://github.com/{owner}/{repo}/issues."
        )

    chooser_url = f"https://github.com/{owner}/{repo}/issues/new/choose"
    fm_entries = _fm_base(chooser_url)
    fm_entries.append("note", _build_issue_template_note(probe, owner, repo))
    fm_entries["trust"] = _TRUST_ADVISORY

    body = _format_issue_submission_section(probe) or ""
    fm = _build_frontmatter(fm_entries)
    return fm + "\n\n" + _fence_content(body, title=f"{owner}/{repo}")


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

async def _build_issue_markdown(
    owner: str, repo: str, number: int, limit: int, page: int,
) -> tuple[str, str, str, FMEntries] | str:
    """Fetch issue + comments and build raw markdown.

    Returns (title, raw_markdown, state, extra_fm_entries) on success,
    or an error string on failure.
    """
    result = await _github_request(
        "GET", f"/repos/{owner}/{repo}/issues/{number}",
    )
    if isinstance(result, str):
        return result
    assert isinstance(result, dict)

    title = result["title"]
    state = result["state"]
    author = result["user"]["login"]
    body = result.get("body") or ""
    created = result.get("created_at", "")
    labels = _fmt_labels(result.get("labels", []))
    comment_count = result.get("comments", 0)
    reactions = result.get("reactions", {})
    association = result.get("author_association", "")

    extra_fm = FMEntries({"type": "issue", "state": state})
    if comment_count > limit:
        extra_fm.append("hint", f"Showing {limit} of {comment_count} comments. Use page= for more.")

    parts = []
    meta = f"**{owner}/{repo}#{number}** | {state} | {comment_count} comments"
    parts.append(meta)

    assoc_str = f" ({association})" if association and association != "NONE" else ""
    parts.append(f"**@{author}**{assoc_str} — {_fmt_relative_time(created)}")
    if labels:
        parts.append(f"Labels: {labels}")

    reaction_str = _fmt_reactions(reactions)
    if reaction_str:
        parts.append(reaction_str)

    parts.append("")
    if body:
        parts.append(body)

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

                cr_str = _fmt_reactions(creactions)
                if cr_str:
                    parts.append(cr_str)

                parts.append("")
                parts.append(cbody)
                parts.append("")

    return title, "\n".join(parts), state, extra_fm


async def _action_issue(
    query: str, limit: int, page: int,
) -> str:
    """Fetch an issue with comments."""
    parsed = _parse_owner_repo_number(query)
    if isinstance(parsed, str):
        return parsed
    owner, repo, number = parsed

    built, template_hint = await asyncio.gather(
        _build_issue_markdown(owner, repo, number, limit, page),
        _maybe_issue_template_hint(owner, repo),
    )
    if isinstance(built, str):
        return built
    title, raw_md, state, extra_fm = built

    fm_entries = _fm_base(f"https://github.com/{owner}/{repo}/issues/{number}")
    fm_entries.update(extra_fm)
    fm_entries["trust"] = _TRUST_ADVISORY

    content, trunc_hint = _apply_semantic_truncation(raw_md, 5000)
    if trunc_hint:
        fm_entries["truncated"] = trunc_hint
        _append_frontmatter_entry(
            fm_entries, "hint",
            f"Use page= to load more comments (page {page + 1})",
        )
    _append_frontmatter_entry(fm_entries, "hint", template_hint)
    fm = _build_frontmatter(fm_entries)
    return fm + "\n\n" + _fence_content(content, title=title)


# ---------------------------------------------------------------------------
# Action: pull_request
# ---------------------------------------------------------------------------

async def _build_pr_markdown(
    owner: str, repo: str, number: int, limit: int, page: int,
) -> tuple[str, str, str, FMEntries] | str:
    """Fetch PR + review comments and build raw markdown.

    Returns (title, raw_markdown, display_state, extra_fm_entries) on success,
    or an error string on failure.
    """
    result = await _github_request(
        "GET", f"/repos/{owner}/{repo}/pulls/{number}",
    )
    if isinstance(result, str):
        return result
    assert isinstance(result, dict)

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

    extra_fm = FMEntries({"type": "pull_request", "state": display_state})

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

    parts.append("\n## Diff stat\n")
    parts.append(f"{changed_files} files changed, +{additions}, -{deletions}")

    # Fetch review comments and issue comments concurrently when both exist
    review_coro = (
        _github_request(
            "GET", f"/repos/{owner}/{repo}/pulls/{number}/comments",
            params={"per_page": str(min(limit, 100)), "page": str(page)},
        ) if review_comment_count > 0 else None
    )
    issue_coro = (
        _github_request(
            "GET", f"/repos/{owner}/{repo}/issues/{number}/comments",
            params={"per_page": str(min(limit, 100)), "page": str(page)},
        ) if comment_count > 0 else None
    )
    if review_coro and issue_coro:
        review_comments, comments = await asyncio.gather(review_coro, issue_coro)
    elif review_coro:
        review_comments = await review_coro
        comments = []
    elif issue_coro:
        review_comments = []
        comments = await issue_coro
    else:
        review_comments = []
        comments = []

    if isinstance(review_comments, list) and review_comments:
        by_file: dict[str, list[dict]] = {}
        for rc in review_comments:
            path = rc.get("path", "unknown")
            by_file.setdefault(path, []).append(rc)

        parts.append("\n## Review comments\n")
        for filepath, file_comments in by_file.items():
            parts.append(f"### {filepath}\n")
            for rc in file_comments:
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

                if diff_hunk and not in_reply:
                    hunk_lines = diff_hunk.strip().split("\n")
                    display_lines = hunk_lines[-6:] if len(hunk_lines) > 6 else hunk_lines
                    parts.append("```diff")
                    parts.extend(display_lines)
                    parts.append("```")

                parts.append("")
                parts.append(rcbody)
                parts.append("")

    if isinstance(comments, list) and comments:
        parts.append("\n## Comments\n")
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

    return title, "\n".join(parts), display_state, extra_fm


async def _action_pull_request(
    query: str, limit: int, page: int,
) -> str:
    """Fetch a pull request with diff stats and review comments."""
    parsed = _parse_owner_repo_number(query)
    if isinstance(parsed, str):
        return parsed
    owner, repo, number = parsed

    built, template_hint = await asyncio.gather(
        _build_pr_markdown(owner, repo, number, limit, page),
        _maybe_issue_template_hint(owner, repo),
    )
    if isinstance(built, str):
        return built
    title, raw_md, display_state, extra_fm = built

    fm_entries = _fm_base(f"https://github.com/{owner}/{repo}/pull/{number}")
    fm_entries.update(extra_fm)
    fm_entries["trust"] = _TRUST_ADVISORY

    content, trunc_hint = _apply_semantic_truncation(raw_md, 5000)
    if trunc_hint:
        fm_entries["truncated"] = trunc_hint
        _append_frontmatter_entry(
            fm_entries, "hint",
            f"Use page= to load more comments (page {page + 1})",
        )
    _append_frontmatter_entry(fm_entries, "hint", template_hint)
    fm = _build_frontmatter(fm_entries)
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

    # Fetch the file and the OpenSSF Scorecard in parallel.  The scorecard
    # is cached per-process, so subsequent files in the same repo pay no
    # extra latency.  When the code the agent is consuming comes from a
    # low-scored repo, surfacing that in frontmatter gives the caller a
    # chance to weigh trust before using the content.
    async def _fetch_raw() -> httpx.Response | str:
        try:
            async with httpx.AsyncClient(
                timeout=30.0, follow_redirects=True,
            ) as client:
                return await client.get(raw_url, headers=headers)
        except httpx.TimeoutException:
            return f"Error: Request timed out for {raw_url}"
        except httpx.RequestError as e:
            return f"Error: Request failed - {type(e).__name__}"

    response, scorecard_score = await asyncio.gather(
        _fetch_raw(),
        _fetch_scorecard_overall(owner, repo),
    )
    if isinstance(response, str):
        return response

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
    from .common import _LANGUAGE_MAP
    lang = _LANGUAGE_MAP.get(ext, "")

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
    if scorecard_score is not None:
        fm_entries["openssf_scorecard"] = f"{scorecard_score:g}/10"
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
    "search_issues", "search_code", "search_repos", "repo", "tree",
    "issue", "pull_request", "file", "issue_templates",
)


async def github(
    action: Annotated[str, Field(
        description=(
            "The operation to perform. "
            "search_issues: search issues/PRs by query (supports GitHub qualifiers like repo:, is:, label:). "
            "search_repos: search repositories by query (supports qualifiers like topic:, stars:, language:, forks:). "
            "search_code: search code across GitHub (supports qualifiers like repo:, language:, path:). "
            "issue: get issue details + comments by owner/repo#number. "
            "pull_request: get PR details + review comments + diff stat by owner/repo#number. "
            "file: get file content from a repo (use ref= for branch/tag). "
            "repo: get repo metadata + README. "
            "tree: get directory listing. "
            "issue_templates: list issue forms, markdown templates, and contact-link routing for a repository — use before filing a new issue."
        ),
    )],
    query: Annotated[str, Field(
        description=(
            "For search_issues/search_repos/search_code: search query with optional GitHub qualifiers. "
            "For issue/pull_request: 'owner/repo#number' (e.g. 'facebook/react#1234'). "
            "For file/tree: 'owner/repo/path' (e.g. 'facebook/react/packages/react/src/React.js'). "
            "For repo and issue_templates: 'owner/repo' (e.g. 'facebook/react')."
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
    if action == "search_repos":
        return await _action_search_repos(query, limit, page)
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
    if action == "issue_templates":
        return await _action_issue_templates(query)

    return f"Error: Action '{action}' not implemented."
