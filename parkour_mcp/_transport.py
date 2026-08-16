"""Outbound transport for the generic fetch path, over ``wreq``.

The generic path fetches whatever URL a human asked for, so it presents a
browser identity.  Modern WAFs score the *coherence* of that claimed identity
against the observed transport fingerprint (TLS JA3/JA4, the HTTP/2 SETTINGS
frame, header ordering), and httpx cannot satisfy them: it emits the ``h2``
library's fingerprint while the User-Agent claims Chrome.  Akamai Bot Manager
and strict Cloudflare Managed Challenge zones read that mismatch in opposite
directions, so no choice of HTTP version fixes both.  ``wreq`` emulates a real
Chrome down to the protocol layer, which is coherent by construction.  The full
finding, and why a challenge-triggered retry is worse than useless, is in
``.claude/TECH_DEBT.md``.

Two properties of this module are deliberate and load-bearing:

* **Nothing here leaks a transport library's types.**  Callers catch
  `FetchError` and read a `FetchResponse`.  The previous design returned
  ``httpx.Response`` and raised ``httpx.*``, which spread the library across 31
  ``except`` arms and made the transport effectively unswappable.  The seam
  exists so the next swap costs one module.
* **`build_client` takes explicit named parameters, never ``**kwargs``.**
  wreq's constructors silently ignore unrecognized keyword arguments, so
  ``dns=`` instead of ``dns_options=`` disables address pinning and the request
  still succeeds against an unvalidated address.  Routing every construction
  through one signature makes that class of typo a `TypeError` here rather than
  a silent fail-open at the socket.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from ipaddress import ip_address
from typing import Any
from urllib.parse import urljoin, urlsplit

from wreq import Client, DnsOptions, Emulation
from wreq.redirect import Policy

from .common import (
    _FETCH_DEADLINE_SECONDS,
    _FETCH_HEADERS,
    _MAX_RESPONSE_BYTES,
    BlockedAddress,
    FetchError,
    ResponseTooLarge,
    _resolve_and_check,
    proxy_in_effect,
)

_logger = logging.getLogger(__name__)

# Chrome emulation profile.  Kept in step with `common._CHROME_MAJOR`, which
# spells the same version into the User-Agent and Client Hints: a profile that
# disagreed with the headers would reintroduce the very incoherence this module
# exists to remove.
_EMULATION = Emulation.Chrome149

# Redirect hops we will follow.  httpx's default is 20; the lower ceiling here
# is affordable because every hop costs a fresh DNS resolution and address
# check, and no legitimate page needs ten.
_MAX_REDIRECTS = 10


class TransportFailure(FetchError):
    """Connection, DNS, or TLS failure."""


class FetchTimeout(FetchError):
    """A per-phase or wall-clock deadline elapsed."""


class FetchStatusError(FetchError):
    """Raised by `FetchResponse.raise_for_status` for a 4xx/5xx response."""

    def __init__(self, message: str, response: FetchResponse) -> None:
        super().__init__(message)
        self.response = response


class PinMismatch(FetchError):
    """The peer address is not one the address check validated.

    Should be unreachable: the pin is installed before the connection is made.
    It is asserted anyway because a silent pin failure is exactly the shape of
    bug that makes an SSRF guard decorative, and wreq's tolerance of unknown
    keyword arguments means a future refactor could disable pinning without
    any other symptom.
    """


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """A fully-buffered response, independent of the client that produced it.

    Mirrors the subset of ``httpx.Response`` this codebase actually reads, so
    call sites did not have to change shape when the transport did.
    """

    status_code: int
    headers: Mapping[str, str]
    content: bytes
    url: str
    http_version: str
    remote_addr: str | None = None
    history: tuple[str, ...] = field(default=())
    pinned: bool = True
    """Whether the connection was bound to an address the check validated.

    False when a proxy performed the resolution instead, which makes the
    address check advisory.  Callers surface that with `proxy_warning` rather
    than letting the weaker guarantee pass as the strong one.
    """

    @property
    def text(self) -> str:
        """Body decoded per the Content-Type charset, falling back to UTF-8.

        Decoding is lenient because the caller wants the readable page, not a
        verdict on the origin's encoding hygiene, and a mislabelled charset is
        common enough on the open web that raising would strand real content.
        """
        return self.content.decode(self._charset(), errors="replace")

    def _charset(self) -> str:
        content_type = self.headers.get("content-type", "")
        for part in content_type.split(";"):
            key, _, value = part.strip().partition("=")
            if key.strip().lower() == "charset" and value:
                return value.strip().strip('"\'') or "utf-8"
        return "utf-8"

    def json(self) -> Any:
        return json.loads(self.content)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise FetchStatusError(
                f"HTTP {self.status_code} for {self.url}", self
            )


def build_client(
    *,
    pin: Mapping[str, list[str]] | None = None,
    follow_redirects: bool = False,
    timeout: float = 30.0,
) -> Client:
    """Construct the one sanctioned outbound client.

    *pin* maps a hostname to the addresses the check already validated.  The
    mapping is installed as wreq's DNS resolution override, so the connection
    goes to an address that was checked rather than to whatever a second
    lookup returns.  Resolving once and connecting to that result is what
    closes the DNS-rebinding race; validating a URL string and letting a lower
    layer resolve it again is the shape behind that whole bug class.

    Environment proxies need no handling here.  wreq honours ``HTTP_PROXY``,
    ``HTTPS_PROXY``, ``ALL_PROXY`` and ``NO_PROXY`` by default, so a configured
    egress proxy is used without being reproduced.  That is a real improvement
    over the httpx path, which computes ``allow_env_proxies = trust_env and
    transport is None`` and therefore silently ignored every proxy variable
    once a custom transport was installed, forcing `guarded_client` to rebuild
    the proxy map by hand through the private ``httpx._utils`` API.

    A proxy does weaken the guarantee, because the proxy performs the
    resolution that reaches the network and no local pin can bind it.  That is
    detected after the fact in `_verify_pin` rather than predicted here, and
    reported as `FetchResponse.pinned`.

    Only these parameters are accepted, on purpose.  See the module docstring:
    a ``**kwargs`` passthrough would let a misspelled ``dns_options`` disable
    pinning silently.
    """
    # Always a real DnsOptions, empty when there is nothing to pin: wreq's
    # Client rejects None here, and building the argument unconditionally
    # keeps every parameter explicitly named (see module docstring).
    dns_options = DnsOptions()
    for host, addresses in (pin or {}).items():
        dns_options.add_resolve(host, [ip_address(a) for a in addresses])

    redirect = Policy.limited(_MAX_REDIRECTS) if follow_redirects else Policy.none()

    # Keyword names here are the load-bearing detail; see module docstring.
    return Client(
        emulation=_EMULATION,
        dns_options=dns_options,
        redirect=redirect,
        timeout=timedelta(seconds=timeout),
    )


def _host_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise TransportFailure(f"URL has no host: {url}")
    return host, parts.port or (443 if parts.scheme == "https" else 80)


def _verify_pin(response: Any, validated: list[str], host: str) -> tuple[str | None, bool]:
    """Confirm the peer is an address the check approved.

    Returns ``(peer, pinned)``.  wreq reports the address it actually connected
    to, which httpx does not expose cheaply, so pinning stops being a property
    we configure and becomes one we observe.

    A peer outside the validated set means one of two things, and they are
    distinguished by whether a proxy is configured rather than assumed:

    * **Proxied.**  The peer is the proxy, and the proxy performs the
      resolution that actually reaches the network, so no local pin can bind.
      Report ``pinned=False`` and let the caller surface `proxy_warning`.
    * **Unproxied.**  Nothing should be able to move the connection off a
      pinned address, so this is a real failure and raises.

    Checking the peer *before* consulting the proxy environment is deliberate.
    `proxy_in_effect` does not evaluate ``NO_PROXY``, so branching on it first
    would drop pinning for every host a ``NO_PROXY`` entry exempts, which are
    exactly the hosts still reached directly and therefore still pinnable.
    """
    remote = getattr(response, "remote_addr", None)
    if remote is None:
        return None, False
    peer = str(remote.ip()) if callable(getattr(remote, "ip", None)) else str(remote)
    if peer in validated:
        return peer, True
    if proxy_in_effect():
        _logger.debug(
            "peer %s for %s is not a validated address; a proxy is configured, "
            "so the address check is advisory", peer, host,
        )
        return peer, False
    raise PinMismatch(
        f"connected to {peer} for {host}, which is not among the "
        f"validated addresses {validated}"
    )


async def _read_capped(response: Any, max_bytes: int | None, url: str) -> bytes:
    """Buffer the body, refusing to exceed *max_bytes*.

    Two layers, matching the previous httpx implementation: the advertised
    Content-Length is rejected before any body is read, then the stream is
    counted as it arrives so a chunked or mislabelled response cannot slip
    past the header check.
    """
    if max_bytes is not None:
        advertised = response.content_length
        # wreq reports 0 rather than None when the origin omits the header, so
        # absent and empty are indistinguishable here; only a positive value
        # carries information.
        if advertised and advertised > max_bytes:
            raise ResponseTooLarge(
                f"Content-Length {advertised} exceeds {max_bytes:,} byte limit"
            )

    chunks: list[bytes] = []
    total = 0
    async with response.stream() as stream:
        async for chunk in stream:
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ResponseTooLarge(
                    f"Response body exceeded {max_bytes:,} byte limit "
                    f"at {total:,} bytes"
                )
            chunks.append(chunk)
    _logger.debug("fetched %s (%d bytes)", url, total)
    return b"".join(chunks)


_VERSION_NAMES = {
    "HTTP_09": "HTTP/0.9",
    "HTTP_10": "HTTP/1.0",
    "HTTP_11": "HTTP/1.1",
    "HTTP_2": "HTTP/2",
    "HTTP_3": "HTTP/3",
}


def _version_string(version: Any) -> str:
    """Normalise wreq's Version enum to httpx's spelling.

    Callers and tests compare against ``"HTTP/2"``; keeping that vocabulary
    means the transport swap is invisible to them.
    """
    name = getattr(version, "name", None) or str(version).rsplit(".", 1)[-1]
    return _VERSION_NAMES.get(name, str(name))


async def guarded_fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    max_bytes: int | None = _MAX_RESPONSE_BYTES,
    deadline: float = _FETCH_DEADLINE_SECONDS,
    follow_redirects: bool = True,
) -> FetchResponse:
    """Fetch *url* with layered protection against oversized and unsafe targets.

    1. **Address check and pin** — the host is resolved once, every address is
       checked, and the connection is pinned to that result.  Rejecting on
       *any* refused address, rather than the one the OS happened to return
       first, denies whoever controls the zone the choice of which record is
       used.  Repeated per redirect hop, because a redirect introduces a
       destination the first check never saw.
    2. **Content-Length gate** — an advertised body larger than *max_bytes* is
       refused before the body is read.
    3. **Streaming size cap** — the body is counted as it arrives and the
       stream is abandoned if it exceeds *max_bytes*.
    4. **Wall-clock deadline** — an ``asyncio.timeout`` wraps the whole
       operation, redirect chain included.  Always applies, including when
       *max_bytes* is ``None``, so a slow-drip firehose is still bounded.

    Redirects are followed manually rather than by the client, because the
    address check has to run against each hop's real destination.

    Raises:
        BlockedAddress: a hop resolved to a private or reserved address
        ResponseTooLarge: body exceeded *max_bytes*
        FetchTimeout: per-phase or wall-clock deadline elapsed
        TransportFailure: connection, DNS, or TLS failure
        PinMismatch: the peer was not a validated address
    """
    request_headers = dict(_FETCH_HEADERS) if headers is None else dict(headers)

    try:
        async with asyncio.timeout(deadline):
            return await _follow(
                url, request_headers, timeout, max_bytes, follow_redirects
            )
    except TimeoutError as exc:
        raise FetchTimeout(
            f"Wall-clock deadline of {deadline}s exceeded for {url}"
        ) from exc


async def _follow(
    url: str,
    headers: dict[str, str],
    timeout: float,
    max_bytes: int | None,
    follow_redirects: bool,
) -> FetchResponse:
    """Walk the redirect chain, address-checking and pinning every hop."""
    history: list[str] = []
    current = url

    for _ in range(_MAX_REDIRECTS + 1):
        host, port = _host_port(current)
        validated = await _resolve_and_check(host, port)

        client = build_client(
            pin={host: validated}, follow_redirects=False, timeout=timeout
        )
        try:
            response = await client.get(current, headers=headers)
        except (BlockedAddress, ResponseTooLarge, FetchError):
            raise
        except Exception as exc:  # wreq's own error types
            raise TransportFailure(f"{type(exc).__name__} for {current}: {exc}") from exc

        peer, pinned = _verify_pin(response, validated, host)

        location = response.headers.get("location")
        if isinstance(location, (bytes, bytearray)):
            location = location.decode("latin-1")

        if response.status.is_redirection() and location and follow_redirects:
            history.append(current)
            current = urljoin(current, location)
            continue

        body = await _read_capped(response, max_bytes, current)
        return FetchResponse(
            status_code=response.status.as_int(),
            headers={k.lower(): v for k, v in _iter_headers(response.headers)},
            content=body,
            url=current,
            http_version=_version_string(response.version),
            remote_addr=peer,
            history=tuple(history),
            pinned=pinned,
        )

    raise TransportFailure(
        f"Exceeded {_MAX_REDIRECTS} redirects starting from {url}"
    )


def _iter_headers(header_map: Any):
    """Yield ``(name, value)`` as ``str`` from wreq's HeaderMap.

    HeaderMap has no ``items()``; iterating it yields ``(bytes, bytes)``
    pairs.  Latin-1 is the right codec rather than a shortcut: RFC 9110 defines
    field values as opaque octets whose historical encoding is ISO-8859-1, and
    it is total, so no real header can fail to decode.
    """
    for key, value in header_map:
        name = key.decode("latin-1") if isinstance(key, (bytes, bytearray)) else str(key)
        val = (
            value.decode("latin-1")
            if isinstance(value, (bytes, bytearray))
            else str(value)
        )
        yield name, val
