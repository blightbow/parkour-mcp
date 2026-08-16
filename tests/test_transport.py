"""Tests for the wreq-backed generic fetch transport.

The network is never touched.  `build_client` is the single seam through
which `_transport` reaches wreq, so the redirect, pinning, and size-cap tests
replace it with a fake that speaks the parts of wreq's response surface the
module reads.  That keeps the guarantees under test (address checking per
hop, the byte caps, pin verification) rather than testing wreq itself.
"""

import asyncio
from unittest.mock import patch

import pytest

from parkour_mcp import _transport
from parkour_mcp._transport import (
    FetchResponse,
    FetchStatusError,
    FetchTimeout,
    PinMismatch,
    TransportFailure,
    _host_port,
    _iter_headers,
    _verify_pin,
    _version_string,
    build_client,
    guarded_fetch,
)
from parkour_mcp.common import BlockedAddress, FetchError, ResponseTooLarge

# ---------------------------------------------------------------------------
# Fakes standing in for wreq's response surface
# ---------------------------------------------------------------------------


class _FakeStatus:
    def __init__(self, code: int) -> None:
        self._code = code

    def as_int(self) -> int:
        return self._code

    def is_redirection(self) -> bool:
        return 300 <= self._code < 400


class _FakeHeaders:
    """wreq's HeaderMap: iterates ``(bytes, bytes)``, has no ``items()``."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._pairs = [(k.lower().encode(), v.encode()) for k, v in mapping.items()]

    def __iter__(self):
        return iter(self._pairs)

    def get(self, key: str):
        wanted = key.lower().encode()
        return next((v for k, v in self._pairs if k == wanted), None)


class _FakeAddr:
    def __init__(self, ip: str) -> None:
        self._ip = ip

    def ip(self) -> str:
        return self._ip


class _FakeVersion:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
        peer: str = "93.184.216.34",
        version: str = "HTTP_2",
        content_length: int = 0,
    ) -> None:
        self.status = _FakeStatus(status)
        self.headers = _FakeHeaders(headers or {})
        self.remote_addr = _FakeAddr(peer)
        self.version = _FakeVersion(version)
        self.content_length = content_length
        self._chunks = chunks if chunks is not None else [b"body"]

    def stream(self):
        return _FakeStream(self._chunks)


class _FakeClient:
    """Returns queued responses and records the URLs it was asked for."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.requested: list[str] = []

    async def get(self, url: str, headers=None):
        self.requested.append(url)
        if not self._responses:
            raise AssertionError(f"no fake response queued for {url}")
        return self._responses.pop(0)


@pytest.fixture
def fake_transport(monkeypatch):
    """Replace `build_client` and the address check with controllable fakes.

    Returns a callable taking the queued responses; it yields the record of
    what `build_client` was handed, so tests can assert a pin was installed
    for every hop.
    """
    state: dict = {"pins": [], "client": None}

    def install(responses, *, validated=("93.184.216.34",)):
        client = _FakeClient(responses)
        state["client"] = client

        def _build(*, pin=None, follow_redirects=False, timeout=30.0):
            state["pins"].append(pin)
            return client

        async def _check(host, port):
            return list(validated)

        monkeypatch.setattr(_transport, "build_client", _build)
        monkeypatch.setattr(_transport, "_resolve_and_check", _check)
        return state

    return install


# ---------------------------------------------------------------------------
# FetchResponse: the surface callers read
# ---------------------------------------------------------------------------


class TestFetchResponse:
    def _resp(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        content: bytes = b"hello",
        url: str = "https://example.com/",
        http_version: str = "HTTP/2",
    ) -> FetchResponse:
        return FetchResponse(
            status_code=status_code,
            headers={"content-type": "text/html"} if headers is None else headers,
            content=content,
            url=url,
            http_version=http_version,
        )

    def test_text_uses_declared_charset(self):
        resp = self._resp(
            headers={"content-type": "text/html; charset=iso-8859-1"},
            content="café".encode("iso-8859-1"),
        )
        assert resp.text == "café"

    def test_text_defaults_to_utf8_when_unlabelled(self):
        assert self._resp(content="café".encode()).text == "café"

    def test_text_is_lenient_about_mislabelled_bytes(self):
        """A wrong charset must not strand the page behind an exception."""
        resp = self._resp(
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"\xff\xfe invalid",
        )
        assert "invalid" in resp.text

    def test_charset_tolerates_quoting(self):
        resp = self._resp(
            headers={"content-type": 'text/html; charset="utf-8"'},
            content=b"ok",
        )
        assert resp.text == "ok"

    def test_json(self):
        assert self._resp(content=b'{"a": 1}').json() == {"a": 1}

    def test_raise_for_status_passes_below_400(self):
        self._resp(status_code=399).raise_for_status()

    def test_raise_for_status_raises_at_400(self):
        resp = self._resp(status_code=404)
        with pytest.raises(FetchStatusError) as excinfo:
            resp.raise_for_status()
        assert excinfo.value.response is resp

    def test_status_error_is_a_fetch_error(self):
        """One hierarchy: callers should never need the transport's types."""
        assert issubclass(FetchStatusError, FetchError)

    def test_defaults_to_pinned(self):
        assert self._resp().pinned is True


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


class TestNormalisation:
    def test_version_names_match_httpx_spelling(self):
        """test_live asserts ``== "HTTP/2"``; the vocabulary must not shift."""
        assert _version_string(_FakeVersion("HTTP_2")) == "HTTP/2"
        assert _version_string(_FakeVersion("HTTP_11")) == "HTTP/1.1"
        assert _version_string(_FakeVersion("HTTP_3")) == "HTTP/3"

    def test_unknown_version_degrades_to_its_name(self):
        assert _version_string(_FakeVersion("HTTP_42")) == "HTTP_42"

    def test_headers_decode_from_bytes(self):
        pairs = dict(_iter_headers(_FakeHeaders({"Content-Type": "text/html"})))
        assert pairs == {"content-type": "text/html"}

    def test_headers_accept_str_pairs(self):
        class _StrHeaders:
            def __iter__(self):
                return iter([("X-Thing", "value")])

        assert dict(_iter_headers(_StrHeaders())) == {"X-Thing": "value"}

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://example.com/x", ("example.com", 443)),
            ("http://example.com/x", ("example.com", 80)),
            ("https://example.com:8443/x", ("example.com", 8443)),
        ],
    )
    def test_host_port(self, url, expected):
        assert _host_port(url) == expected

    def test_host_port_rejects_hostless_url(self):
        with pytest.raises(TransportFailure):
            _host_port("file:///etc/passwd")


# ---------------------------------------------------------------------------
# build_client: the anti-footgun seam
# ---------------------------------------------------------------------------


class TestBuildClient:
    def test_rejects_unknown_keyword(self):
        """The whole reason this wrapper exists.

        wreq's own constructors silently ignore unrecognised keywords, so
        ``dns=`` instead of ``dns_options=`` would disable address pinning
        while the request still succeeded against an unvalidated address.
        Funnelling construction through one explicit signature turns that
        typo into a TypeError here instead of a fail-open at the socket.
        """
        with pytest.raises(TypeError):
            build_client(dns={"example.com": ["93.184.216.34"]})  # ty: ignore[unknown-argument]

    def test_builds_without_a_pin(self):
        assert build_client() is not None

    def test_builds_with_a_pin(self):
        assert build_client(pin={"example.com": ["93.184.216.34"]}) is not None

    def test_rejects_a_malformed_pin_address(self):
        """A non-address in the pin must fail loudly, not silently skip."""
        with pytest.raises(ValueError):
            build_client(pin={"example.com": ["not-an-ip"]})


# ---------------------------------------------------------------------------
# Pin verification
# ---------------------------------------------------------------------------


class TestVerifyPin:
    def test_validated_peer_reports_pinned(self):
        resp = _FakeResponse(peer="93.184.216.34")
        assert _verify_pin(resp, ["93.184.216.34"], "example.com") == (
            "93.184.216.34",
            True,
        )

    def test_unvalidated_peer_raises_when_unproxied(self):
        resp = _FakeResponse(peer="10.0.0.1")
        with (
            patch.object(_transport, "proxy_in_effect", return_value=False),
            pytest.raises(PinMismatch, match="10.0.0.1"),
        ):
            _verify_pin(resp, ["93.184.216.34"], "example.com")

    def test_unvalidated_peer_degrades_when_proxied(self):
        """A proxy resolves for us, so the peer is the proxy and no pin binds."""
        resp = _FakeResponse(peer="10.0.0.1")
        with patch.object(_transport, "proxy_in_effect", return_value=True):
            peer, pinned = _verify_pin(resp, ["93.184.216.34"], "example.com")
        assert (peer, pinned) == ("10.0.0.1", False)

    def test_no_proxy_exempt_host_stays_pinned(self):
        """Regression guard for the NO_PROXY hole.

        `proxy_in_effect` does not evaluate NO_PROXY, so branching on it
        before checking the peer would drop pinning for exactly the hosts a
        NO_PROXY entry still routes directly. Verifying the peer first keeps
        them pinned even while a proxy is configured for other hosts.
        """
        resp = _FakeResponse(peer="93.184.216.34")
        with patch.object(_transport, "proxy_in_effect", return_value=True):
            assert _verify_pin(resp, ["93.184.216.34"], "example.com")[1] is True


# ---------------------------------------------------------------------------
# guarded_fetch
# ---------------------------------------------------------------------------


class TestGuardedFetch:
    @pytest.mark.asyncio
    async def test_returns_a_buffered_response(self, fake_transport):
        fake_transport([_FakeResponse(chunks=[b"hello ", b"world"])])
        resp = await guarded_fetch("https://example.com/")
        assert resp.status_code == 200
        assert resp.content == b"hello world"
        assert resp.http_version == "HTTP/2"
        assert resp.pinned is True

    @pytest.mark.asyncio
    async def test_pins_every_hop(self, fake_transport):
        """A redirect introduces a destination the first check never saw."""
        state = fake_transport(
            [
                _FakeResponse(status=301, headers={"location": "https://example.com/b"}),
                _FakeResponse(chunks=[b"done"]),
            ]
        )
        resp = await guarded_fetch("https://example.com/a")
        assert resp.url == "https://example.com/b"
        assert resp.history == ("https://example.com/a",)
        assert state["client"].requested == [
            "https://example.com/a",
            "https://example.com/b",
        ]
        # One pin installed per hop, each naming the host it validated.
        assert len(state["pins"]) == 2
        assert all(p == {"example.com": ["93.184.216.34"]} for p in state["pins"])

    @pytest.mark.asyncio
    async def test_relative_redirect_is_resolved(self, fake_transport):
        fake_transport(
            [
                _FakeResponse(status=302, headers={"location": "/elsewhere"}),
                _FakeResponse(chunks=[b"ok"]),
            ]
        )
        resp = await guarded_fetch("https://example.com/a")
        assert resp.url == "https://example.com/elsewhere"

    @pytest.mark.asyncio
    async def test_redirect_not_followed_when_disabled(self, fake_transport):
        fake_transport(
            [_FakeResponse(status=301, headers={"location": "https://example.com/b"})]
        )
        resp = await guarded_fetch("https://example.com/a", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.history == ()

    @pytest.mark.asyncio
    async def test_blocked_address_propagates(self, monkeypatch):
        """The address check runs before anything is fetched."""

        async def _refuse(host, port):
            raise BlockedAddress(f"{host} resolved to 127.0.0.1")

        monkeypatch.setattr(_transport, "_resolve_and_check", _refuse)
        with pytest.raises(BlockedAddress):
            await guarded_fetch("https://evil.example/")

    @pytest.mark.asyncio
    async def test_content_length_gate_rejects_before_reading(self, fake_transport):
        state = fake_transport([_FakeResponse(content_length=10_000, chunks=[b"x"])])
        with pytest.raises(ResponseTooLarge, match="Content-Length"):
            await guarded_fetch("https://example.com/", max_bytes=1024)
        assert state["client"].requested == ["https://example.com/"]

    @pytest.mark.asyncio
    async def test_absent_content_length_does_not_trip_the_gate(self, fake_transport):
        """wreq reports 0, not None, when the origin omits the header.

        A zero must read as "unknown" rather than "empty", or every chunked
        response would either be refused or bypass the streaming cap.
        """
        fake_transport([_FakeResponse(content_length=0, chunks=[b"abc"])])
        resp = await guarded_fetch("https://example.com/", max_bytes=1024)
        assert resp.content == b"abc"

    @pytest.mark.asyncio
    async def test_streaming_cap_trips_mid_body(self, fake_transport):
        fake_transport([_FakeResponse(chunks=[b"a" * 600, b"b" * 600])])
        with pytest.raises(ResponseTooLarge, match="exceeded"):
            await guarded_fetch("https://example.com/", max_bytes=1000)

    @pytest.mark.asyncio
    async def test_caps_disabled_when_max_bytes_is_none(self, fake_transport):
        fake_transport([_FakeResponse(content_length=10**9, chunks=[b"x" * 5000])])
        resp = await guarded_fetch("https://example.com/", max_bytes=None)
        assert len(resp.content) == 5000

    @pytest.mark.asyncio
    async def test_wall_clock_deadline(self, monkeypatch):
        """The deadline covers the whole operation, redirect chain included."""

        async def _check(host, port):
            await asyncio.sleep(0.5)
            return ["93.184.216.34"]

        monkeypatch.setattr(_transport, "_resolve_and_check", _check)
        with pytest.raises(FetchTimeout, match="deadline"):
            await guarded_fetch("https://example.com/", deadline=0.05)

    @pytest.mark.asyncio
    async def test_redirect_loop_is_bounded(self, fake_transport):
        fake_transport(
            [
                _FakeResponse(status=301, headers={"location": "https://example.com/x"})
                for _ in range(_transport._MAX_REDIRECTS + 2)
            ]
        )
        with pytest.raises(TransportFailure, match="redirect"):
            await guarded_fetch("https://example.com/a")

    @pytest.mark.asyncio
    async def test_transport_errors_are_wrapped(self, monkeypatch):
        """wreq's own exception types must not escape this module."""

        async def _check(host, port):
            return ["93.184.216.34"]

        class _Boom:
            async def get(self, url, headers=None):
                raise RuntimeError("connection reset")

        monkeypatch.setattr(_transport, "_resolve_and_check", _check)
        monkeypatch.setattr(_transport, "build_client", lambda **kw: _Boom())
        with pytest.raises(TransportFailure, match="connection reset"):
            await guarded_fetch("https://example.com/")

    @pytest.mark.asyncio
    async def test_guard_errors_are_not_wrapped(self, monkeypatch):
        """A size refusal must stay a size refusal, not become a transport one."""

        async def _check(host, port):
            return ["93.184.216.34"]

        class _TooBig:
            async def get(self, url, headers=None):
                raise ResponseTooLarge("nope")

        monkeypatch.setattr(_transport, "_resolve_and_check", _check)
        monkeypatch.setattr(_transport, "build_client", lambda **kw: _TooBig())
        with pytest.raises(ResponseTooLarge):
            await guarded_fetch("https://example.com/")


class TestExceptionHierarchy:
    @pytest.mark.parametrize(
        "exc",
        [BlockedAddress, ResponseTooLarge, TransportFailure, FetchTimeout,
         FetchStatusError, PinMismatch],
    )
    def test_everything_is_a_fetch_error(self, exc):
        """One `except FetchError` arm has to be enough for callers."""
        assert issubclass(exc, FetchError)
