"""Pure URL and identifier classification for the fast-path chain.

This module is the single home for *stateless* URL detection: given a string,
decide which API-backed source (if any) owns it and extract the identifier or
components a handler needs.  Everything here depends only on the standard
library (``re`` and ``urllib.parse``), so it can be imported anywhere without
pulling a source module's heavy transport dependencies (httpx, curl_cffi,
tree-sitter, yt-dlp).  That cheapness is the point: the fast-path dispatchers
(`fetch_direct`, `_pipeline`) import these at module top instead of lazily, and
sibling tools (e.g. Kagi search) can steer callers toward a fast path by
recognising its URLs without loading the fetcher.

Deliberately NOT here:

- **GitHub** detection lives in `github.py`: `_detect_github_url` consults
  `_get_github_token()` to auth-gate discussion URLs, so it is not stateless.
- **MediaWiki** and **Discourse** detect at fetch time (an API probe and a
  response-header check respectively), not from the URL string, so they have
  no pure predicate to host here.

HuggingFace *is* here, and the contrast with GitHub is deliberate rather than
accidental.  A Hub repo's gated / private / nonexistent status is a property of
the API *response*, never of the URL string, and the Hub returns an identical
401 for all three when unauthenticated — so there is no HF URL shape whose
classification could consult a token even if it wanted to.  Detection stays
pure; the token only ever affects the fetch.

Fetch-ready URL *rewriting* tied to a specific endpoint stays with its handler
(e.g. reddit's swap to ``oauth.reddit.com`` and ``.json`` suffixing); only the
``old.reddit.com`` normalisation that detection itself performs lives here.
"""

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------
# Matches arxiv.org/{abs,pdf}/<id> and export.arxiv.org variants.
# Excludes /html/ — arXiv's HTML endpoint serves full rendered papers;
# intercepting it would discard full text in favor of metadata-only.
ARXIV_URL_RE = re.compile(
    r'https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)',
    re.IGNORECASE,
)


def _detect_arxiv_url(url: str) -> str | None:
    """Extract a bare arXiv ID from an arXiv URL, or None.

    Matches /abs/ and /pdf/ paths. Does NOT match /html/ — those should
    fall through to HTTP fetch for full paper text with BM25 slicing.
    """
    m = ARXIV_URL_RE.search(url)
    return m.group(1) if m else None


# Matches /html/ paths (full paper text — not intercepted by the fast path)
_ARXIV_HTML_RE = re.compile(
    r'https?://(?:export\.)?arxiv\.org/html/(\d{4}\.\d{4,5}(?:v\d+)?)',
    re.IGNORECASE,
)


def _detect_arxiv_html_url(url: str) -> str | None:
    """Extract arXiv ID from an /html/ URL, or None."""
    m = _ARXIV_HTML_RE.search(url)
    return m.group(1) if m else None


_VERSION_SUFFIX_RE = re.compile(r'v\d+$')


def _strip_version(arxiv_id: str) -> str:
    """Strip the version suffix from an arXiv ID for DOI synthesis.

    DataCite registers one DOI per paper, always versionless:
    ``10.48550/arXiv.2501.16496`` (not ``v1``).  The Atom API always
    returns versioned IDs (e.g. ``2501.16496v1``), so this helper is
    needed whenever constructing DOIs from API-returned IDs.

    The versioned ID should still be used for arXiv URLs (abs, pdf, html)
    and display — arXiv recommends citing with the specific version.
    """
    return _VERSION_SUFFIX_RE.sub('', arxiv_id)


# ---------------------------------------------------------------------------
# DOI
# ---------------------------------------------------------------------------
DOI_URL_RE = re.compile(
    r'https?://(?:dx\.)?doi\.org/(10\.\S+)',
    re.IGNORECASE,
)


def _detect_doi_url(url: str) -> str | None:
    """Extract a bare DOI from a doi.org URL, or None."""
    m = DOI_URL_RE.search(url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------
# Matches semanticscholar.org/paper/ URLs, captures 40-char hex paper ID
S2_URL_RE = re.compile(
    r'https?://(?:www\.)?semanticscholar\.org/paper/(?:[^/]+/)?([0-9a-f]{40})',
    re.IGNORECASE,
)


def _detect_s2_url(url: str) -> str | None:
    """Extract a 40-char hex paper ID from a Semantic Scholar URL, or None."""
    m = S2_URL_RE.search(url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# IETF (RFC Editor + Datatracker)
# ---------------------------------------------------------------------------
# Matches rfc-editor.org/rfc/rfc{N} only when the URL targets the canonical
# metadata endpoint: bare path (with optional trailing slash) or `.json` suffix.
# Body-text suffixes (.html/.txt/.xml/.pdf) intentionally fall through to the
# generic HTTP+markdown pipeline so callers can use section= and search=
# against the rendered text.  Does NOT match /info/ pages (used for subseries
# resolution).
_RFC_EDITOR_RE = re.compile(
    r'https?://www\.rfc-editor\.org/rfc/rfc(\d+)(?:\.json)?(?=[/?#]|$)',
    re.IGNORECASE,
)

# Matches datatracker.ietf.org/doc/{rfc{N}|draft-*}
_DATATRACKER_RE = re.compile(
    r'https?://datatracker\.ietf\.org/doc/(rfc(\d+)|draft-[\w.-]+)/?',
    re.IGNORECASE,
)


def _detect_ietf_url(url: str) -> dict | None:
    """Detect an IETF RFC or Internet-Draft URL.

    Returns ``{"type": "rfc", "number": int}`` for RFC URLs,
    ``{"type": "draft", "name": str}`` for I-D URLs, or None.
    """
    m = _RFC_EDITOR_RE.search(url)
    if m:
        return {"type": "rfc", "number": int(m.group(1))}

    m = _DATATRACKER_RE.search(url)
    if m:
        if m.group(2):
            # rfc{N}
            return {"type": "rfc", "number": int(m.group(2))}
        # draft-*
        return {"type": "draft", "name": m.group(1)}

    return None


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------
_REDDIT_URL_RE = re.compile(
    r"https?://(?:(?:www|old|new|np)\.)?reddit\.com/",
    re.IGNORECASE,
)

_REDD_IT_RE = re.compile(
    r"https?://redd\.it/(\w+)",
    re.IGNORECASE,
)

# Matches a search endpoint, global (/search/) or subreddit-scoped
# (/r/SUB/search/). Anchored to the end so it does not match a post whose
# slug merely contains the word "search".
_SEARCH_RE = re.compile(r"/search/?$", re.IGNORECASE)

# Query params preserved on a Reddit search URL. q is the payload; the rest
# tune or paginate the search. include_over_18 is the NSFW lever (the userless
# token filters adult content out of search by default; =1 includes it). A
# curated allowlist (not blind passthrough) because a stray param can break the
# endpoint: `category=<anything>` 500s it. Reddit's full documented set is:
# after, before, category, count, include_facets, limit, q, restrict_sr, show,
# sort, sr_detail, t, type. We drop `category` (500-prone) and
# `sr_detail`/`include_facets` (response shaping we do not render). Everything
# outside this set (tracking junk like utm_/ref/share_id) is dropped so it
# never reaches the API.
_SEARCH_QUERY_KEYS = (
    "q", "sort", "t", "restrict_sr", "type",
    "after", "before", "limit", "count", "show", "include_over_18",
)


def is_reddit_url(url: str) -> bool:
    """True if *url* is a link the Reddit fast path can serve.

    Detection only — covers the ``reddit.com`` host variants and ``redd.it``
    short links, with no normalisation or rewriting.  Shares the two module
    regexes with `_detect_reddit_url` so the predicate and the rewriter can
    never disagree on what counts as a Reddit URL.  Used by sibling tools
    (e.g. Kagi search) to steer callers toward this fast path rather than a
    direct fetch Reddit would 403.
    """
    return bool(_REDD_IT_RE.match(url) or _REDDIT_URL_RE.match(url))


def _detect_reddit_url(url: str) -> str | None:
    """Return normalised old.reddit.com URL if *url* is a Reddit link, else None.

    Rewrites the host to old.reddit.com and strips a caller-appended ``.json``
    suffix (the fetch function appends its own; a doubled ``.json`` makes
    Reddit answer 400).  For listings and permalinks only ``sort`` is
    preserved; for ``/search`` URLs the search-relevant params (``q`` and
    friends) are preserved, since ``q`` is the payload, not tracking noise.
    Does NOT append ``.json`` — the fetch function does that.  For ``redd.it``
    short links the original URL is returned (redirect resolved during fetch).
    """
    # Short links
    if _REDD_IT_RE.match(url):
        return url

    if not _REDDIT_URL_RE.match(url):
        return None

    parsed = urlparse(url)

    # Rewrite host
    netloc = "old.reddit.com"

    # Strip a caller-appended `.json` so the fetcher does not double it,
    # then ensure a trailing slash.
    path = parsed.path
    path = path.removesuffix(".json")
    if not path.endswith("/"):
        path += "/"

    # Preserve query params: search URLs carry their payload in the query
    # (q is mandatory), so keep the search-relevant set; everything else
    # keeps only ?sort=.
    qs = parse_qs(parsed.query)
    keep: dict[str, list[str]] = {}
    if _SEARCH_RE.search(path):
        for k in _SEARCH_QUERY_KEYS:
            if k in qs:
                keep[k] = qs[k]
        # Default to NOT filtering NSFW, to match the rest of the toolkit
        # (Kagi, the generic fetch) which never editorialize on adult
        # content. Search is the only Reddit path that filters NSFW by
        # default; direct subreddit/thread fetches already surface it. An
        # explicit ?include_over_18=0 from the caller is honored.
        keep.setdefault("include_over_18", ["1"])
    elif "sort" in qs:
        keep["sort"] = qs["sort"]
    query = urlencode(keep, doseq=True)

    return urlunparse(("https", netloc, path, "", query, ""))


class RedditPageType(Enum):
    COMMENT_THREAD = "comment_thread"
    SUBREDDIT = "subreddit"
    USER = "user"
    SHORT_LINK = "short_link"
    SEARCH = "search"


# Matches a comment thread with or without the /r/SUB/ prefix: redd.it short
# links redirect to the subreddit-less /comments/{id} canonical form, which
# Reddit (and oauth.reddit.com) resolve to the full thread.
_COMMENT_RE = re.compile(r"/(?:r/[^/]+/)?comments/\w+", re.IGNORECASE)
_USER_RE = re.compile(r"/(?:u|user)/[^/]+", re.IGNORECASE)

# Comment permalink: /r/SUB/comments/POSTID/slug/COMMENTID/ — points at
# a specific comment within a post.  Reddit's .json endpoint serves a
# context-scoped subtree for these URLs (the comment + its replies, not
# the full thread), which silently truncates most of the conversation.
# Canonical Reddit permalinks always include the slug; we require it to
# disambiguate from a slug-less whole-post URL /r/SUB/comments/POSTID/.
_PERMALINK_RE = re.compile(
    r"^/r/([^/]+)/comments/(\w+)/([^/]+)/(\w+)/?$",
    re.IGNORECASE,
)


def _extract_comment_permalink(url: str) -> tuple[str, str] | None:
    """Decompose a comment-permalink URL into (stripped_url, comment_id).

    Returns ``None`` for whole-post URLs, subreddit listings, user
    pages, or any other URL shape — callers treat ``None`` as "not a
    permalink, handle normally."

    ``stripped_url`` points at the containing post (with any comment-ID
    suffix removed); ``comment_id`` is the identifier of the targeted
    comment.  Callers can use it as a ``section=`` filter on the
    full-thread fetch: the Reddit renderer emits ``### {id}`` /
    ``#### {id}`` headings for each comment, so the section filter
    resolves naturally against the cached markdown.
    """
    parsed = urlparse(url)
    m = _PERMALINK_RE.match(parsed.path)
    if not m:
        return None
    sub, post, slug, comment_id = m.groups()
    stripped_path = f"/r/{sub}/comments/{post}/{slug}/"
    stripped = urlunparse((
        parsed.scheme, parsed.netloc, stripped_path,
        "", parsed.query, "",
    ))
    return stripped, comment_id


def _classify_reddit_url(url: str) -> RedditPageType:
    """Classify a Reddit URL by page type."""
    if _REDD_IT_RE.match(url):
        return RedditPageType.SHORT_LINK

    parsed = urlparse(url)
    path = parsed.path

    if _COMMENT_RE.search(path):
        return RedditPageType.COMMENT_THREAD
    if _SEARCH_RE.search(path):
        return RedditPageType.SEARCH
    if _USER_RE.search(path):
        return RedditPageType.USER
    return RedditPageType.SUBREDDIT


# ---------------------------------------------------------------------------
# HuggingFace Hub
# ---------------------------------------------------------------------------

@dataclass
class HFUrlMatch:
    """Parsed components of a HuggingFace Hub URL.

    *kind* is one of ``model``, ``tree``, ``file``, ``commit``, or ``org``.
    ``dataset`` and ``space`` are recognised only so the caller can decline
    them explicitly — v1 handles models, and silently treating a dataset URL
    as a model repo would produce a confidently wrong answer.
    """
    kind: str
    repo: str = ""          # "<org>/<name>" for repo kinds; "" for org
    org: str = ""
    rev: str = "main"
    path: str | None = None
    sha: str | None = None


# Reserved first path segments that are Hub features, not orgs.  A URL like
# /models?search=… or /login must never parse as an org named "models".
_HF_RESERVED = frozenset({
    "datasets", "spaces", "models", "docs", "blog", "learn", "papers",
    "collections", "posts", "login", "join", "settings", "pricing",
    "notifications", "new", "organizations", "chat", "tasks", "inference",
    "api", "search", "welcome", "enterprise", "changelog", "terms-of-service",
    "privacy", "content-guidelines", "code-of-conduct", "brand",
})

_HF_HOST_RE = re.compile(
    r"^https?://(?:www\.)?huggingface\.co(/.*)?$", re.IGNORECASE,
)

# Repo sub-paths, applied to the remainder after "<org>/<name>/".
_HF_TREE_RE = re.compile(r"^tree/([^/]+)(?:/(.*))?$")
_HF_BLOB_RE = re.compile(r"^(?:blob|resolve|raw)/([^/]+)/(.+)$")
_HF_COMMIT_RE = re.compile(r"^commit/([0-9a-f]{7,40})$", re.IGNORECASE)

_HF_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def is_hf_commit_sha(rev: str) -> bool:
    """True when *rev* is a full 40-hex commit SHA.

    Callers use this to decide cacheability: a commit-pinned revision is
    immutable, so its metadata can be cached indefinitely, while ``main``
    and other branch refs move under you.
    """
    return bool(_HF_SHA_RE.match(rev))


def _detect_hf_url(url: str) -> HFUrlMatch | None:
    """Parse a HuggingFace Hub URL into its components, or return None.

    Pure string parsing — see the module docstring for why this is stateless
    where the GitHub equivalent is not.

    ``/resolve/`` (CDN raw) and ``/raw/`` (LFS pointer) collapse into the same
    ``file`` kind as ``/blob/``: they differ in what bytes the *browser* gets,
    but the tool answers all three from the same repo-file handler, and for
    weight files it answers without transferring the payload at all.
    """
    host = _HF_HOST_RE.match(url.strip())
    if not host:
        return None

    parsed = urlparse(url.strip())
    segments = [s for s in parsed.path.split("/") if s]

    if not segments:
        return None

    head = segments[0].lower()

    # Non-model repo types: recognised, then declined by the caller.
    if head in ("datasets", "spaces"):
        if len(segments) >= 3:
            repo = f"{segments[1]}/{segments[2]}"
        elif len(segments) == 2:
            repo = segments[1]
        else:
            return None
        return HFUrlMatch(
            kind="dataset" if head == "datasets" else "space",
            repo=repo,
            org=segments[1],
        )

    if head in _HF_RESERVED:
        return None

    # Bare /<org> — an org or user profile.
    if len(segments) == 1:
        return HFUrlMatch(kind="org", org=segments[0])

    org, name = segments[0], segments[1]
    repo = f"{org}/{name}"
    rest = "/".join(segments[2:])

    if not rest:
        return HFUrlMatch(kind="model", repo=repo, org=org)

    if m := _HF_COMMIT_RE.match(rest):
        return HFUrlMatch(kind="commit", repo=repo, org=org, sha=m.group(1))

    if m := _HF_TREE_RE.match(rest):
        return HFUrlMatch(
            kind="tree", repo=repo, org=org,
            rev=m.group(1), path=m.group(2) or "",
        )

    if m := _HF_BLOB_RE.match(rest):
        return HFUrlMatch(
            kind="file", repo=repo, org=org,
            rev=m.group(1), path=m.group(2),
        )

    # Some other repo tab (discussions, settings, …) — the repo itself is
    # still the useful answer, so fall back to the model beta rather than
    # dropping to a generic scrape of a JS-rendered page.
    return HFUrlMatch(kind="model", repo=repo, org=org)
