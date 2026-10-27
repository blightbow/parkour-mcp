"""Reddit fast path — fetches Reddit pages through Reddit's own APIs.

Reddit serves no structured data to anonymous clients: the www and
old.reddit `.json` endpoints answer with a 403 "network security" block on
every TLS profile. Access goes through a *userless* OAuth token instead,
mirroring redlib (redlib-org/redlib, src/oauth.rs + src/client.rs): mint a
logged-out token via Reddit's own mobile-app grant, fall back to the
generic-web grant, and refresh it shortly before expiry. There is no user
account, no API key, and no app registration: the token is anonymous and
carries only a logged-out id (`loid`).

Two hosts serve that token. Listings, search, and user pages come from
oauth.reddit.com in the JSON API's Listing shape. Comment threads are refused
there, so the post comes from oauth.reddit.com's `/by_id` and the comments
from the GraphQL gateway behind Reddit's Android app; `_fetch_thread_json`
reassembles them into the Listing shape, so every formatter below reads one
shape whichever host answered.

The client IDs are Reddit's OWN first-party app credentials, not a third
party's, so we borrow Reddit's identity (a browser-equivalent reader) rather
than impersonating an indie app whose developer would become collateral when
Reddit revokes a scraper-associated id.

Supports comment threads, subreddit listings, search, and user pages.
"""

import asyncio
import base64
import contextlib
import logging
import re
import secrets
import string
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from wreq import Client, Emulation
from wreq import exceptions as wreq_exc
from wreq.redirect import Policy

from .common import RateLimiter
from .detection import RedditPageType, _classify_reddit_url, _detect_reddit_url

logger = logging.getLogger(__name__)

# Reddit began blocking httpx's TLS fingerprint in April 2026 while still
# serving browsers: same headers via curl get 200, via httpx get 403. Safari
# profiles pass; Chrome profiles are JA3-blocked. The token mint and the
# oauth.reddit.com calls both go through this emulation.
#
# Reddit is one of the two places this codebase presents a browser identity
# rather than identifying honestly (see the header-selection policy in
# `common.py`): it withdrew access to content it does not exclusively license.
_EMULATION = Emulation.Safari26_4

# wreq's exception types are flat: every one subclasses ``Exception`` directly,
# with no ``RequestException``-style base to catch.  They are enumerated so a
# network failure becomes an error string instead of propagating out of the
# tool, and so the list is a visible thing to revisit rather than a bare
# ``except Exception``.  Collapse this if wreq ever grows a common base.
_WREQ_FETCH_ERRORS = (
    wreq_exc.BodyError,
    wreq_exc.ConnectionError,
    wreq_exc.ConnectionResetError,
    wreq_exc.DecodingError,
    wreq_exc.ProxyConnectionError,
    wreq_exc.RedirectError,
    wreq_exc.RequestError,
    wreq_exc.StatusError,
    wreq_exc.TlsError,
)

# ---------------------------------------------------------------------------
# Rate limiter — 2s between API requests
# ---------------------------------------------------------------------------

_reddit_limiter = RateLimiter(
    2.0,
    name="Reddit",
    policy="2 s between requests on a userless OAuth token, well under "
           "Reddit's ceiling of roughly 100 requests per minute",
)

# ---------------------------------------------------------------------------
# OAuth — userless "installed_client" access tokens
# ---------------------------------------------------------------------------
#
# Two-tier token acquisition mirroring redlib's Oauth::new: the mobile grant
# first (Reddit-for-Android client id against the logged-out loid endpoint),
# then the generic-web grant as a fallback. redlib retries the mobile grant
# up to 5 times 5s apart before falling back and exits after 10 total
# failures; we collapse that to one attempt per tier because we run inside an
# interactive tool call, not a server boot loop, so a fast graceful failure
# beats a 25s+ stall (the caller surfaces the error string).

# Reddit's official Reddit-for-Android OAuth client id
# (redlib oauth.rs#REDDIT_ANDROID_OAUTH_CLIENT_ID).
_ANDROID_OAUTH_CLIENT_ID = "ohXpoqrZYub1kg"
# Reddit's logged-out web client id (redlib GenericWebAuth, base64-decoded).
_WEB_OAUTH_CLIENT_ID = "3XfBJWliHvqACnXrfIYlLw"

_OAUTH_API_HOST = "oauth.reddit.com"
_MOBILE_TOKEN_URL = "https://www.reddit.com/auth/v2/oauth/access-token/loid"  # noqa: S105  # not a secret: token-mint endpoint URL
_WEB_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"  # noqa: S105  # not a secret: token-mint endpoint URL
_INSTALLED_CLIENT_GRANT = "https://oauth.reddit.com/grants/installed_client"

# Refresh this many seconds before stated expiry so a content fetch never
# blocks on a mint (redlib token_daemon: sleeps expires_in - 120).
_TOKEN_REFRESH_MARGIN = 120.0

# Recent Reddit-for-Android build strings, used to randomise the spoofed
# mobile User-Agent (subset of redlib oauth_resources.rs#ANDROID_APP_VERSION_LIST).
_ANDROID_APP_VERSIONS = (
    "Version 2024.47.0/Build 2029755",
    "Version 2024.46.0/Build 2012731",
    "Version 2024.45.0/Build 2001943",
    "Version 2024.44.0/Build 1988458",
    "Version 2024.43.0/Build 1972250",
    "Version 2024.42.0/Build 1952440",
    "Version 2024.41.1/Build 1947805",
    "Version 2024.40.0/Build 1928580",
    "Version 2024.39.0/Build 1916713",
    "Version 2024.38.0/Build 1902791",
)

_DEVICE_ID_ALPHABET = string.ascii_letters + string.digits

# Module-level token cache. Session-scoped: resets when the server restarts,
# like the page/wiki caches. Guarded by _oauth_lock so concurrent first
# fetches mint only once.
_oauth_lock = asyncio.Lock()
_oauth_token: str | None = None
# Device UA plus the x-reddit-loid / x-reddit-session identifiers Reddit
# issued, replayed on every content request so it looks like one device.
_oauth_token_headers: dict[str, str] = {}
_oauth_expires_at: float = 0.0
_oauth_backend: str | None = None  # "mobile" | "web"
_refresh_task: Optional["asyncio.Task"] = None


def _android_device_headers() -> dict[str, str]:
    """Build a spoofed Reddit-for-Android device header set.

    Mirrors redlib oauth.rs ``Device::android``: a random recent app build, a
    random Android major version, and a per-device UUID used for both vendor
    and device id.  The ``User-Agent`` is carried inside the returned dict and
    replayed on subsequent content requests.
    """
    device_id = str(uuid.uuid4())
    app_version = secrets.choice(_ANDROID_APP_VERSIONS)
    android_version = secrets.randbelow(6) + 9  # 9..14 inclusive
    return {
        "User-Agent": f"Reddit/{app_version}/Android {android_version}",
        "x-reddit-retry": "algo=no-retries",
        "x-reddit-compression": "1",
        "client-vendor-id": device_id,
        "X-Reddit-Device-Id": device_id,
    }


def _header_str(value) -> str:
    """Decode a wreq header value to ``str``.

    wreq returns header values as ``bytes``; these are replayed into later
    request headers, which must be text. Latin-1 is total and is the encoding
    RFC 9110 ascribes to field values, so no real header fails to decode.
    """
    if isinstance(value, (bytes, bytearray)):
        return value.decode("latin-1")
    return "" if value is None else str(value)


async def _mint_mobile_token() -> tuple[str, float, dict[str, str]] | None:
    """Mint a userless token via the logged-out Android-app grant.

    Mirrors redlib ``MobileSpoofAuth``: HTTP Basic with the Android client id
    and empty secret against the loid endpoint.  Returns
    ``(token, expires_at_monotonic, replay_headers)`` or ``None`` on failure.
    """
    device_headers = _android_device_headers()
    auth = base64.standard_b64encode(f"{_ANDROID_OAUTH_CLIENT_ID}:".encode()).decode()
    req_headers = {
        **device_headers,
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    try:
        async with Client(emulation=_EMULATION) as client:
            resp = await client.post(
                _MOBILE_TOKEN_URL,
                headers=req_headers,
                json={"scopes": ["*", "email", "pii"]},
                timeout=timedelta(seconds=15),
            )
            if resp.status.as_int() != 200:
                logger.debug("Reddit mobile token mint: HTTP %s", resp.status.as_int())
                return None
            payload = await resp.json()
            token = payload.get("access_token")
            expires_in = payload.get("expires_in")
            if not token or not expires_in:
                return None
            replay = dict(device_headers)
            for h in ("x-reddit-loid", "x-reddit-session"):
                val = _header_str(resp.headers.get(h))
                if val:
                    replay[h] = val
            return token, time.monotonic() + float(expires_in), replay
    except Exception:
        logger.debug("Reddit mobile token mint error", exc_info=True)
        return None


async def _mint_web_token() -> tuple[str, float, dict[str, str]] | None:
    """Fallback mint via the generic-web installed_client grant.

    Mirrors redlib ``GenericWebAuth``: Basic auth with the web client id and a
    random device id, form-encoded ``installed_client`` grant.
    """
    device_id = "".join(secrets.choice(_DEVICE_ID_ALPHABET) for _ in range(20))
    auth = base64.standard_b64encode(f"{_WEB_OAUTH_CLIENT_ID}:".encode()).decode()
    req_headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
    }
    body = {"grant_type": _INSTALLED_CLIENT_GRANT, "device_id": device_id}
    try:
        async with Client(emulation=_EMULATION) as client:
            resp = await client.post(
                _WEB_TOKEN_URL, headers=req_headers, data=body, timeout=timedelta(seconds=15),
            )
            if resp.status.as_int() != 200:
                logger.debug("Reddit web token mint: HTTP %s", resp.status.as_int())
                return None
            payload = await resp.json()
            token = payload.get("access_token")
            expires_in = payload.get("expires_in")
            if not token or not expires_in:
                return None
            replay = {"Origin": "https://www.reddit.com"}
            for h in ("x-reddit-loid", "x-reddit-session"):
                val = _header_str(resp.headers.get(h))
                if val:
                    replay[h] = val
            return token, time.monotonic() + float(expires_in), replay
    except Exception:
        logger.debug("Reddit web token mint error", exc_info=True)
        return None


async def _authenticate() -> bool:
    """Acquire a userless token: mobile grant first, web fallback.

    Sets the module token state on success and returns True.  On total
    failure leaves any existing token untouched and returns False.
    """
    global _oauth_token, _oauth_token_headers, _oauth_expires_at, _oauth_backend
    for backend, mint in (("mobile", _mint_mobile_token), ("web", _mint_web_token)):
        result = await mint()
        if result is not None:
            _oauth_token, _oauth_expires_at, _oauth_token_headers = result
            _oauth_backend = backend
            logger.info("Reddit OAuth token acquired via %s grant", backend)
            return True
    logger.warning("Reddit OAuth token acquisition failed (mobile + web grants)")
    return False


def _token_valid() -> bool:
    """True if a token exists and is not within the refresh margin of expiry."""
    return bool(_oauth_token) and time.monotonic() < _oauth_expires_at - _TOKEN_REFRESH_MARGIN


def _ensure_refresh_daemon() -> None:
    """Start the background refresh task once, if an event loop is running."""
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _refresh_task = loop.create_task(_token_daemon())


async def _token_daemon() -> None:
    """Refresh the token ~2 min before expiry (redlib token_daemon).

    Runs for the life of the server once started.  On a refresh failure it
    backs off briefly and retries rather than exiting; content fetches also
    refresh reactively on 401, so a transient daemon miss is recoverable.
    """
    while True:
        delay = _oauth_expires_at - _TOKEN_REFRESH_MARGIN - time.monotonic()
        await asyncio.sleep(max(delay, 5.0))
        async with _oauth_lock:
            ok = await _authenticate()
        if not ok:
            await asyncio.sleep(30.0)


async def _ensure_token() -> str | None:
    """Return a valid bearer token, minting/refreshing under lock if needed.

    Starts the refresh daemon after the first successful mint so later
    fetches never pay mint latency.  Returns ``None`` if acquisition fails.
    """
    if _token_valid():
        return _oauth_token
    async with _oauth_lock:
        if _token_valid():  # another coroutine may have minted while we waited
            return _oauth_token
        if not await _authenticate():
            return None
        _ensure_refresh_daemon()
        return _oauth_token


async def _force_refresh_token() -> None:
    """Re-mint the token now (reactive refresh on a 401)."""
    async with _oauth_lock:
        await _authenticate()

# ---------------------------------------------------------------------------
# Detection (imported from detection.py)
# ---------------------------------------------------------------------------
# URL detection, page-type classification, and the old.reddit.com
# normalisation live in detection.py (pure, stdlib-only).  This module imports
# back the helpers its fetch flow uses directly (_detect_reddit_url,
# _classify_reddit_url, RedditPageType) at the top of the file; is_reddit_url
# and _extract_comment_permalink are consumed only by other modules.

# Maximum comment-tree depth rendered in a thread (a fetch/format concern, not
# detection, so it stays here).
_MAX_COMMENT_DEPTH = 6


# ---------------------------------------------------------------------------
# JSON fetch
# ---------------------------------------------------------------------------

async def _resolve_redd_it(url: str) -> str | None:
    """Follow a redd.it short link redirect to get the canonical URL.

    Attaches the bearer token and device headers when available (redlib
    resolves share links with the OAuth client too); falls back to an
    unauthenticated HEAD if no token could be minted.
    """
    token = await _ensure_token()
    headers = (
        {"Authorization": f"Bearer {token}", **_oauth_token_headers}
        if token else {}
    )
    try:
        await _reddit_limiter.wait()
        async with Client(
            emulation=_EMULATION, redirect=Policy.limited(10),
        ) as client:
            resp = await client.head(
                url, headers=headers, timeout=timedelta(seconds=10),
            )
            final = str(resp.url)
            return _detect_reddit_url(final)
    except Exception:
        logger.debug("redd.it redirect failed for %s", url, exc_info=True)
        return None


def _check_reddit_json_error(data: list | dict) -> list | dict | str:
    """Map Reddit's in-band JSON error envelopes to error strings.

    Mirrors redlib client.rs ``json()``: a suspended user, the quarantined /
    gated / private / banned subreddit reasons, and the bare Unauthorized
    envelope.  Normal listing/thread responses (lists, or dicts whose ``data``
    holds a ``children`` body) carry no ``error`` field and pass through.
    """
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict) and inner.get("is_suspended"):
            return "Error: This Reddit account is suspended."
        err = data.get("error")
        if isinstance(err, int):
            reason = data.get("reason")
            mapping = {
                "quarantined": "Error: This subreddit is quarantined.",
                "gated": "Error: This subreddit is gated (age or community restricted).",
                "private": "Error: This subreddit is private.",
                "banned": "Error: This subreddit is banned.",
            }
            if reason in mapping:
                return mapping[reason]
            if data.get("message") == "Unauthorized":
                return "Error: Reddit returned Unauthorized."
            return f"Error: Reddit API error {err}: {data.get('message', '')}".rstrip()
    return data


async def _fetch_reddit_json(url: str) -> list | dict | str:
    """Fetch a Reddit URL's JSON via the authenticated oauth.reddit.com API.

    Rewrites the host to oauth.reddit.com, appends ``.json`` and
    ``raw_json=1``, and attaches the userless bearer token plus the replayed
    device headers.  Returns parsed JSON (list or dict) on success, or an
    error string.  Retries once on a 401 after forcing a token refresh.
    """
    token = await _ensure_token()
    if not token:
        return (
            "Error: Could not authenticate to Reddit. The unauthenticated "
            ".json endpoints were retired 2026-05-29; the userless OAuth token "
            "mint (oauth.reddit.com) failed, so Reddit may have changed the "
            "logged-out token flow."
        )

    if _classify_reddit_url(url) == RedditPageType.COMMENT_THREAD:
        return await _fetch_thread_json(url)

    parsed = urlparse(url)
    path = parsed.path.rstrip("/") + "/.json"
    qs = parse_qs(parsed.query)
    qs["raw_json"] = ["1"]
    json_url = urlunparse((
        "https", _OAUTH_API_HOST, path, "", urlencode(qs, doseq=True), "",
    ))

    return await _reddit_api_call(json_url, retry_on_401=True)


async def _reddit_api_call(
    json_url: str, *, json_body: dict | None = None, retry_on_401: bool,
) -> list | dict | str:
    """Call a Reddit JSON endpoint with the current bearer token.

    GETs ``json_url``, or POSTs ``json_body`` to it when one is given (the
    GraphQL gateway).  Returns parsed JSON or an error string.
    """
    headers = {
        "Authorization": f"Bearer {_oauth_token}",
        "Accept": "application/json",
        **_oauth_token_headers,
    }
    await _reddit_limiter.wait()
    try:
        async with Client(
            emulation=_EMULATION, redirect=Policy.limited(10),
        ) as client:
            if json_body is None:
                resp = await client.get(
                    json_url, headers=headers, timeout=timedelta(seconds=30),
                )
            else:
                resp = await client.post(
                    json_url, headers=headers, json=json_body,
                    timeout=timedelta(seconds=30),
                )
            status = resp.status.as_int()
            if status == 401 and retry_on_401:
                await _force_refresh_token()
                if not _oauth_token:
                    return "Error: Reddit OAuth token expired and refresh failed."
                return await _reddit_api_call(
                    json_url, json_body=json_body, retry_on_401=False,
                )
            if status == 429:
                return "Error: Reddit rate limit exceeded. Try again later."
            if status == 403:
                # A private or quarantined subreddit answers 403 with a JSON
                # envelope that names the reason; Reddit's edge block answers
                # 403 with an HTML "blocked by network security" page.
                ctype = _header_str(resp.headers.get("content-type"))
                if "json" in ctype:
                    mapped = _check_reddit_json_error(await resp.json())
                    if isinstance(mapped, str):
                        return mapped
                return (
                    f"Error: HTTP 403 for {json_url} — Reddit refused this "
                    "endpoint to a logged-out client."
                )
            # Status is read directly rather than via raise_for_status: wreq's
            # StatusError carries no ``response``, so the code would have to be
            # recovered from the message string to report it.
            if status >= 400:
                return f"Error: HTTP {status} for {json_url}"
            data = await resp.json()
    except wreq_exc.TimeoutError:
        return f"Error: Request timed out for {json_url}"
    except _WREQ_FETCH_ERRORS as exc:
        return f"Error: Failed to fetch {json_url} — {type(exc).__name__}"
    except ValueError:
        return f"Error: Invalid JSON response from {json_url}"

    return _check_reddit_json_error(data)


# ---------------------------------------------------------------------------
# Comment threads — post from /by_id, comments from GraphQL
# ---------------------------------------------------------------------------
#
# oauth.reddit.com refuses every comment-thread route to a logged-out token
# (/r/SUB/comments/..., /comments/..., /api/morechildren) with an HTML 403
# "blocked by network security" page, whatever the headers or TLS profile.
# Listings, /by_id and /api/info still answer.  The GraphQL gateway behind
# Reddit's Android app serves the comment forest to the same token, so a
# thread takes two requests, reassembled into the two-Listing shape
# ``[post_listing, comment_listing]`` the formatters below consume.

_GQL_URL = "https://gql-fed.reddit.com/"
# Persisted-operation hash of the Android app's PostComments query.  The
# gateway accepts only persisted operations (ad-hoc query text draws a
# misleading NOT_AUTHENTICATED), so this is pinned to an app build; an unknown
# hash draws "persisted query not found".  The hash is read from the Android
# app's Apollo classes; Amqx/reddit-gql (src/operations.ts) tracks current ones.
_POST_COMMENTS_HASH = "314f29c2494fd78c7844ef2c6b09acb4bef5b37ab50b4ddc01691d6d5c8433f8"
# Comments per request.  Further top-level pages and deep reply chains come
# back as cursor placeholders, which the formatters skip as they skipped the
# JSON API's "more" stubs.
_GQL_COMMENT_COUNT = 200

_THREAD_POST_ID_RE = re.compile(r"/comments/(\w+)", re.IGNORECASE)

# The JSON API's ``sort=`` values, mapped to PostComments' sortType enum.
_GQL_SORT_TYPES = {
    "confidence": "CONFIDENCE",
    "best": "CONFIDENCE",
    "top": "TOP",
    "new": "NEW",
    "controversial": "CONTROVERSIAL",
    "old": "OLD",
    "qa": "QA",
}


def _api_label(url: str) -> str:
    """Frontmatter ``api`` value naming the hosts that serve ``url``.

    A redd.it short link always names a post, so it counts as a thread.
    """
    if _classify_reddit_url(url) in (RedditPageType.COMMENT_THREAD, RedditPageType.SHORT_LINK):
        return f"Reddit ({_OAUTH_API_HOST} + {urlparse(_GQL_URL).hostname})"
    return f"Reddit ({_OAUTH_API_HOST})"


async def _fetch_thread_json(url: str) -> list | str:
    """Fetch a comment thread as ``[post_listing, comment_listing]``.

    A comment permalink yields its whole thread; callers narrow it to the
    comment with a section filter.  Returns an error string on failure.
    """
    match = _THREAD_POST_ID_RE.search(urlparse(url).path)
    if not match:
        return f"Error: No post id in Reddit URL {url}"
    post_id = match.group(1)

    post = await _reddit_api_call(
        f"https://{_OAUTH_API_HOST}/by_id/t3_{post_id}/.json?raw_json=1",
        retry_on_401=True,
    )
    if isinstance(post, str):
        return post
    if not isinstance(post, dict) or not post.get("data", {}).get("children"):
        return f"Error: Reddit post {post_id} not found."

    sort = parse_qs(urlparse(url).query).get("sort", ["confidence"])[0].lower()
    body = {
        "operationName": "PostComments",
        "variables": {
            "id": f"t3_{post_id}",
            "sortType": _GQL_SORT_TYPES.get(sort, "CONFIDENCE"),
            "count": _GQL_COMMENT_COUNT,
            "maxDepth": _MAX_COMMENT_DEPTH,
            "includeConsistencyToken": False,
            "includePremiumAvatarTreatment": False,
        },
        "extensions": {
            "persistedQuery": {"version": 1, "sha256Hash": _POST_COMMENTS_HASH},
        },
    }
    response = await _reddit_api_call(_GQL_URL, json_body=body, retry_on_401=True)
    if isinstance(response, str):
        return response
    comments = _gql_comment_listing(response)
    if isinstance(comments, str):
        return comments
    return [post, comments]


def _gql_comment_listing(response: list | dict) -> dict | str:
    """Convert a PostComments response to the JSON API's comment Listing.

    The forest arrives flat, in depth-first order, each entry carrying its
    ``depth``; nesting is rebuilt from depth rather than ``parentId`` because
    a deleted comment's node carries no id to hang its replies on.
    """
    if not isinstance(response, dict):
        return "Error: Reddit returned an unexpected comments response."
    errors = response.get("errors")
    if errors:
        message = "; ".join(str(e.get("message", e)) for e in errors if e)
        if "persisted query not found" in message.lower():
            return (
                "Error: Reddit retired the comments query this tool relies on "
                "(persisted query not found); the PostComments hash in "
                "reddit.py needs updating."
            )
        return f"Error: Reddit comments API error: {message}"
    post_info = (response.get("data") or {}).get("postInfoById")
    if not post_info:
        return "Error: Reddit returned no comments for this post."
    trees = (post_info.get("commentForest") or {}).get("trees") or []

    top: list[dict] = []
    # Open ancestors as (depth, comment, its replies); a comment closes when
    # the next entry is at its depth or shallower.
    stack: list[tuple[int, dict, list[dict]]] = []
    for tree in trees:
        node = tree.get("node")
        if not node:
            continue  # cursor placeholder for more comments
        depth = tree.get("depth", 0)
        comment = _gql_comment(node)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if stack:
            _, parent, replies = stack[-1]
            if not replies:
                parent["data"]["replies"] = {"kind": "Listing", "data": {"children": replies}}
            replies.append(comment)
        else:
            top.append(comment)
        stack.append((depth, comment, []))

    return {"kind": "Listing", "data": {"children": top}}


def _gql_comment(node: dict) -> dict:
    """Map one GraphQL comment node to a JSON API ``t1`` child."""
    author = (node.get("authorInfo") or {}).get("name") or "[deleted]"
    body = (node.get("content") or {}).get("markdown") or ""
    if node.get("__typename") == "DeletedComment":
        author, body = "[deleted]", "[deleted]"
    elif node.get("isAdminTakedown"):
        body = "[ Removed by Reddit ]"
    elif node.get("isRemoved"):
        author, body = "[deleted]", "[removed]"

    created_utc = 0.0
    if created := node.get("createdAt"):
        with contextlib.suppress(ValueError):
            created_utc = datetime.fromisoformat(created).timestamp()

    fullname = node.get("id") or ""
    return {
        "kind": "t1",
        "data": {
            "id": fullname.removeprefix("t1_"),
            "author": author,
            "body": body,
            "score": node.get("score") or 0,
            "created_utc": created_utc,
            "replies": "",
        },
    }


async def _fetch_reddit_content(url: str) -> tuple[str, str]:
    """Fetch and format a Reddit page.

    Returns ``(title, full_markdown)``.  The caller (pipeline) handles
    frontmatter, fencing, truncation, and cache population.

    On any error the title is ``"Reddit"`` and the markdown is the error
    message — this function never raises.
    """
    page_type = _classify_reddit_url(url)

    # Resolve short links first
    if page_type == RedditPageType.SHORT_LINK:
        resolved = await _resolve_redd_it(url)
        if resolved is None:
            return "Reddit", f"Error: Could not resolve short link {url}"
        url = resolved
        page_type = _classify_reddit_url(url)

    data = await _fetch_reddit_json(url)
    if isinstance(data, str):
        # Error string from _fetch_reddit_json
        return "Reddit", data

    try:
        if page_type == RedditPageType.COMMENT_THREAD and isinstance(data, list):
            return _format_comment_thread(data)
        if page_type == RedditPageType.USER:
            return _format_listing(data, kind="user")
        if page_type == RedditPageType.SEARCH:
            query = parse_qs(urlparse(url).query).get("q", [""])[0]
            return _format_listing(data, kind="search", query=query)
        return _format_listing(data, kind="subreddit")
    except Exception as exc:
        logger.debug("Reddit formatting error", exc_info=True)
        return "Reddit", f"Error: Failed to parse Reddit response — {type(exc).__name__}"


# ---------------------------------------------------------------------------
# Formatting — comment threads
# ---------------------------------------------------------------------------

def _format_timestamp(utc: float) -> str:
    """Convert Unix timestamp to human-readable UTC date string."""
    dt = datetime.fromtimestamp(utc, tz=UTC)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _format_comment_thread(data: list) -> tuple[str, str]:
    """Format a comment-thread JSON response as markdown.

    Returns ``(title, markdown)``.
    """
    post_listing = data[0]
    comment_listing = data[1]

    post_data = post_listing["data"]["children"][0]["data"]

    title = post_data.get("title", "Untitled")
    author = post_data.get("author", "[deleted]")
    score = post_data.get("score", 0)
    num_comments = post_data.get("num_comments", 0)
    subreddit = post_data.get("subreddit", "")
    created = post_data.get("created_utc", 0)
    flair = post_data.get("link_flair_text")
    is_self = post_data.get("is_self", True)
    selftext = post_data.get("selftext", "")
    link_url = post_data.get("url", "")
    upvote_ratio = post_data.get("upvote_ratio", 0)

    parts: list[str] = []

    # Header
    parts.append(f"# {title}\n")
    meta_parts = [
        f"**u/{author}**",
        f"{score} points ({upvote_ratio:.0%} upvoted)",
        f"{num_comments} comments",
        f"r/{subreddit}",
    ]
    if flair:
        meta_parts.append(f"[{flair}]")
    meta_parts.append(_format_timestamp(created))
    parts.append(" | ".join(meta_parts) + "\n")

    # Body
    if is_self and selftext:
        parts.append(selftext + "\n")
    elif not is_self:
        parts.append(f"Link: {link_url}\n")

    # Comments
    comment_children = comment_listing["data"]["children"]
    comments_md = _render_comments(comment_children, depth=0)
    if comments_md:
        parts.append("## Comments\n")
        parts.append(comments_md)

    return title, "\n".join(parts)


def _render_comments(
    children: list[dict], depth: int,
) -> str:
    """Recursively render a comment tree as markdown.

    Each comment becomes a heading (### at depth 0, #### at depth 1, etc.)
    with the comment ID as the heading text.  This enables section-based
    navigation: ``section="ochpsln"`` extracts a specific comment, and
    ``web_fetch_sections`` shows the comment tree as a section hierarchy
    with ancestry breadcrumbs (``Comments > ochpsln > oci19t7``).
    """
    if depth >= _MAX_COMMENT_DEPTH:
        return ""

    # Heading level: ### (h3) for top-level comments under ## Comments (h2)
    hlevel = "#" * min(depth + 3, 6)
    parts: list[str] = []

    for child in children:
        if child.get("kind") != "t1":
            continue

        cdata = child["data"]
        comment_id = cdata.get("id", cdata.get("name", ""))
        author = cdata.get("author", "[deleted]")
        body = cdata.get("body", "")
        score = cdata.get("score", 0)
        created = cdata.get("created_utc", 0)

        # Heading is just the comment ID — enables section= matching
        parts.append(f"{hlevel} {comment_id}\n")

        # Metadata line
        parts.append(f"**u/{author}** ({score} points) — {_format_timestamp(created)}\n")

        # Comment body
        if body:
            parts.append(body + "\n")

        # Recurse into replies
        replies = cdata.get("replies")
        if replies and isinstance(replies, dict):
            reply_children = replies.get("data", {}).get("children", [])
            if reply_children:
                nested = _render_comments(reply_children, depth + 1)
                if nested:
                    parts.append(nested)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Comment-aware splitting for BM25 indexing
# ---------------------------------------------------------------------------

# Matches comment headings (### id, #### id, etc.) at the start of a line
_COMMENT_HEADING_RE = re.compile(r"^(#{3,6}) \S+$", re.MULTILINE)


def _split_by_comments(markdown: str) -> list[tuple[int, str]]:
    """Split formatted Reddit markdown into per-comment chunks.

    The post body (everything before the first ``###`` comment heading)
    becomes slice 0.  Each subsequent comment heading and its content
    (up to the next heading at the same or higher level) becomes its own
    slice.  This produces one BM25-indexed slice per comment rather than
    arbitrary ~1600-char text chunks.

    Returns ``[(char_offset, chunk_text), ...]`` suitable for
    ``_PageCache.store(presplit=...)``.
    """
    splits = list(_COMMENT_HEADING_RE.finditer(markdown))

    if not splits:
        # No comment headings — single chunk (listing or empty thread)
        return [(0, markdown)]

    chunks: list[tuple[int, str]] = []

    # Chunk 0: post body (before first comment heading)
    first_offset = splits[0].start()
    if first_offset > 0:
        chunks.append((0, markdown[:first_offset].rstrip()))

    # Each comment heading → next heading boundary
    for i, match in enumerate(splits):
        start = match.start()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(markdown)
        chunks.append((start, markdown[start:end].rstrip()))

    return chunks


# ---------------------------------------------------------------------------
# Section tree for web_fetch_sections
# ---------------------------------------------------------------------------

def _format_relative_time(comment_utc: float, post_utc: float) -> str:
    """Format a comment timestamp as T+HH:MM:SS relative to the post time."""
    delta = max(0, int(comment_utc - post_utc))
    hours, remainder = divmod(delta, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"T+{hours:02d}:{minutes:02d}:{seconds:02d}"


def _build_comment_section_tree(data: list) -> tuple[str, str]:
    """Build a custom section listing from comment thread JSON.

    Returns ``(title, section_body)`` where section_body has lines like::

        - #ochpsln — u/ManyInterests (54 pts, 223 chars, T+00:40:00)
          - #oci19t7 — u/dan_ohn (11 pts, 110 chars, T+01:42:00)

    The post title includes its absolute timestamp.  Comment times are
    relative to the post (``T+HH:MM:SS``), which conveys conversation
    pacing without cluttering the listing with absolute dates.

    This is used by ``web_fetch_sections`` to show the comment tree as
    navigable sections instead of the generic heading-based listing.
    """
    post_listing = data[0]
    comment_listing = data[1]

    post_data = post_listing["data"]["children"][0]["data"]
    title = post_data.get("title", "Untitled")
    post_utc = post_data.get("created_utc", 0.0)

    lines: list[str] = [f"# {title} ({_format_timestamp(post_utc)})\n"]
    comment_children = comment_listing["data"]["children"]
    _walk_comment_tree(comment_children, depth=0, post_utc=post_utc, lines=lines)

    return title, "\n".join(lines)


def _walk_comment_tree(
    children: list[dict], depth: int, post_utc: float, lines: list[str],
) -> None:
    """Recursively build indented section lines for the comment tree."""
    if depth >= _MAX_COMMENT_DEPTH:
        return

    indent = "  " * depth

    for child in children:
        if child.get("kind") != "t1":
            continue

        cdata = child["data"]
        comment_id = cdata.get("id", cdata.get("name", ""))
        author = cdata.get("author", "[deleted]")
        score = cdata.get("score", 0)
        body = cdata.get("body", "")
        char_len = len(body)
        comment_utc = cdata.get("created_utc", 0.0)
        reltime = _format_relative_time(comment_utc, post_utc)

        lines.append(
            f"{indent}- #{comment_id} — u/{author} ({score} pts, {char_len} chars, {reltime})"
        )

        replies = cdata.get("replies")
        if replies and isinstance(replies, dict):
            reply_children = replies.get("data", {}).get("children", [])
            if reply_children:
                _walk_comment_tree(reply_children, depth + 1, post_utc=post_utc, lines=lines)


# ---------------------------------------------------------------------------
# Formatting — subreddit and user listings
# ---------------------------------------------------------------------------

def _format_listing(
    data: list | dict, *, kind: str = "subreddit", query: str | None = None,
) -> tuple[str, str]:
    """Format a subreddit, user, or search listing as markdown.

    Returns ``(title, markdown)``.  For ``kind="search"`` each result line
    carries its subreddit and a fetchable permalink, since search results
    span communities and the natural next step is to open a specific thread.
    """
    # Listings come as either a single dict or a one-element list
    listing = data[0] if isinstance(data, list) else data

    children = listing.get("data", {}).get("children", [])

    # Determine title from first entry
    if kind == "user" and children:
        first = children[0].get("data", {})
        user = first.get("author", "unknown")
        title = f"u/{user}"
    elif kind == "search":
        title = f"Search: {query}" if query else "Reddit search"
    elif children:
        first = children[0].get("data", {})
        sub = first.get("subreddit", "unknown")
        title = f"r/{sub}"
    else:
        title = "Reddit"

    parts: list[str] = [f"# {title}\n"]

    for i, child in enumerate(children, 1):
        cdata = child.get("data", {})
        ckind = child.get("kind", "")

        if ckind == "t3":
            # Post
            ptitle = cdata.get("title", "Untitled")
            score = cdata.get("score", 0)
            num_comments = cdata.get("num_comments", 0)
            author = cdata.get("author", "[deleted]")
            flair = cdata.get("link_flair_text")
            flair_str = f" [{flair}]" if flair else ""
            if kind == "search":
                sub = cdata.get("subreddit", "")
                permalink = cdata.get("permalink", "")
                link = f"https://www.reddit.com{permalink}" if permalink else ""
                parts.append(
                    f"{i}. **{ptitle}**{flair_str} "
                    f"({score} pts, {num_comments} comments) — r/{sub} — u/{author}\n"
                    f"   {link}"
                )
            else:
                parts.append(
                    f"{i}. **{ptitle}**{flair_str} "
                    f"({score} pts, {num_comments} comments) — u/{author}"
                )
        elif ckind == "t1":
            # Comment (user pages mix posts and comments)
            body_preview = (cdata.get("body", "") or "")[:120]
            if len(cdata.get("body", "")) > 120:
                body_preview += "…"
            score = cdata.get("score", 0)
            subreddit = cdata.get("subreddit", "")
            parts.append(
                f"{i}. r/{subreddit} ({score} pts): {body_preview}"
            )
        elif ckind == "t5":
            # Subreddit hit (search with type=sr). Without this branch the
            # results render empty despite Reddit returning matches.
            name = cdata.get("display_name_prefixed") or f"r/{cdata.get('display_name', '')}"
            subs = cdata.get("subscribers") or 0
            desc = (cdata.get("public_description") or "").strip().replace("\n", " ")
            if len(desc) > 120:
                desc = desc[:120] + "…"
            line = f"{i}. **{name}** ({subs:,} subscribers)"
            if desc:
                line += f" — {desc}"
            sr_url = cdata.get("url", "")
            if sr_url:
                line += f"\n   https://www.reddit.com{sr_url}"
            parts.append(line)
        elif ckind == "t2":
            # User-account hit (search with type=user).
            uname = cdata.get("name", "[unknown]")
            link_karma = cdata.get("link_karma", 0) or 0
            comment_karma = cdata.get("comment_karma", 0) or 0
            parts.append(
                f"{i}. **u/{uname}** ({link_karma:,} link / {comment_karma:,} comment karma)\n"
                f"   https://www.reddit.com/user/{uname}/"
            )

    if not children:
        parts.append("*No posts found.*")

    # Pagination hint
    after = listing.get("data", {}).get("after")
    if after:
        parts.append(f"\n*More posts available (pagination cursor: {after})*")

    return title, "\n".join(parts)
