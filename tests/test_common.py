"""Tests for parkour_mcp.common module."""

import asyncio
import logging
import os
import socket
import stat
from unittest.mock import AsyncMock, patch

import httpcore
import httpx
import pytest
import respx

from parkour_mcp.common import (
    _PROXY_ENV_VARS,
    BlockedAddress,
    _GuardedTransport,
    _is_private_ip,
    _parse_truthy_env,
    _PinningBackend,
    _resolve_and_check,
    check_url_scheme,
    check_url_ssrf,
    guarded_client,
    guarded_fetch,
    load_credential,
    proxy_in_effect,
)


@pytest.fixture
def no_proxy_env(monkeypatch):
    """Clear proxy variables so transport tests do not depend on the
    developer's shell."""
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestParseTruthyEnv:
    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "Yes", " true ", "YES"])
    def test_affirmative_values(self, monkeypatch, value):
        monkeypatch.setenv("PARKOUR_TEST_GATE", value)
        assert _parse_truthy_env("PARKOUR_TEST_GATE") is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  ", "2"])
    def test_negative_values(self, monkeypatch, value):
        monkeypatch.setenv("PARKOUR_TEST_GATE", value)
        assert _parse_truthy_env("PARKOUR_TEST_GATE") is False

    def test_unset_is_false(self, monkeypatch):
        monkeypatch.delenv("PARKOUR_TEST_GATE", raising=False)
        assert _parse_truthy_env("PARKOUR_TEST_GATE") is False

    def test_uppercase_true_is_truthy(self, monkeypatch):
        # Regression: the pre-helper MCP_ALLOW_PRIVATE_IPS gate omitted
        # .lower(), so "True"/"YES" silently failed to enable the bypass.
        monkeypatch.setenv("PARKOUR_TEST_GATE", "True")
        assert _parse_truthy_env("PARKOUR_TEST_GATE") is True


class TestLoadCredential:
    def test_env_var_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PARKOUR_TEST_KEY", "from-env")
        cfg = tmp_path / "key"
        cfg.write_text("from-file")
        assert load_credential("PARKOUR_TEST_KEY", cfg) == "from-env"

    def test_file_fallback(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PARKOUR_TEST_KEY", raising=False)
        cfg = tmp_path / "key"
        cfg.write_text("  from-file\n")
        assert load_credential("PARKOUR_TEST_KEY", cfg) == "from-file"

    def test_missing_both_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PARKOUR_TEST_KEY", raising=False)
        assert load_credential("PARKOUR_TEST_KEY", tmp_path / "absent") == ""

    def test_unsubstituted_template_env_falls_through_to_file(self, monkeypatch, tmp_path):
        # clean_env rejects the mcpb ${...} sentinel; the file should win.
        monkeypatch.setenv("PARKOUR_TEST_KEY", "${user_config.PARKOUR_TEST_KEY}")
        cfg = tmp_path / "key"
        cfg.write_text("from-file")
        assert load_credential("PARKOUR_TEST_KEY", cfg) == "from-file"


class TestCredentialFilePermissions:
    """parkour reads these files and never creates them, so their mode is
    whatever created them left behind — and ``echo key > file`` under a default
    umask leaves an API key readable by every local account. That is what this
    project's own setup instructions produced for anyone who followed them
    literally, so the mode is repaired rather than merely reported.
    """

    @staticmethod
    def _cred(tmp_path, mode, name="key"):
        path = tmp_path / name
        path.write_text("s3cret")
        os.chmod(path, mode)
        return path

    @staticmethod
    def _mode(path):
        return stat.S_IMODE(path.stat().st_mode)

    @pytest.fixture(autouse=True)
    def _reset_ledger(self, monkeypatch):
        from parkour_mcp import common

        common._permission_handled.clear()
        monkeypatch.delenv("PARKOUR_TEST_KEY", raising=False)
        yield
        common._permission_handled.clear()

    @pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666, 0o660])
    def test_a_readable_credential_is_tightened_on_disk(self, mode, tmp_path, caplog):
        path = self._cred(tmp_path, mode)
        with caplog.at_level(logging.WARNING, logger="parkour_mcp.common"):
            assert load_credential("PARKOUR_TEST_KEY", path) == "s3cret"
        assert self._mode(path) == 0o600
        assert "Tightened to 600" in caplog.text

    @pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
    def test_the_warning_survives_the_repair(self, tmp_path, caplog):
        """The chmod closes the window rather than undoing it: whatever could
        read the key already could, so the operator still needs to know."""
        path = self._cred(tmp_path, 0o644)
        with caplog.at_level(logging.WARNING, logger="parkour_mcp.common"):
            load_credential("PARKOUR_TEST_KEY", path)
        assert "rotate the credential" in caplog.text

    @pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
    @pytest.mark.parametrize("mode", [0o600, 0o400])
    def test_an_owner_only_credential_is_left_alone(self, mode, tmp_path, caplog):
        path = self._cred(tmp_path, mode)
        with caplog.at_level(logging.WARNING, logger="parkour_mcp.common"):
            assert load_credential("PARKOUR_TEST_KEY", path) == "s3cret"
        assert self._mode(path) == mode
        assert caplog.text == ""

    @pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
    def test_an_unrepairable_file_still_yields_its_credential(self, tmp_path, caplog, monkeypatch):
        """Read-only mount, not the owner, an overriding ACL.

        Refusing the key would break a working setup over a condition the
        caller may not be able to change.
        """
        from parkour_mcp import common

        path = self._cred(tmp_path, 0o644)
        monkeypatch.setattr(
            common.Path, "chmod",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("read-only")),
        )
        with caplog.at_level(logging.WARNING, logger="parkour_mcp.common"):
            assert load_credential("PARKOUR_TEST_KEY", path) == "s3cret"
        assert "could not be tightened" in caplog.text
        assert "chmod 600" in caplog.text

    @pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
    def test_an_unrepairable_file_is_not_re_reported_every_call(self, tmp_path, caplog, monkeypatch):
        """Credential files are read on every authenticated call."""
        from parkour_mcp import common

        path = self._cred(tmp_path, 0o644)
        monkeypatch.setattr(
            common.Path, "chmod",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("read-only")),
        )
        with caplog.at_level(logging.WARNING, logger="parkour_mcp.common"):
            for _ in range(3):
                load_credential("PARKOUR_TEST_KEY", path)
        assert caplog.text.count("could not be tightened") == 1

    @pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
    def test_an_empty_credential_file_is_still_tightened(self, tmp_path):
        """It reads as absent today and may hold a key tomorrow; leaving it
        loose means the secret lands world-readable when it arrives."""
        path = tmp_path / "empty"
        path.write_text("")
        os.chmod(path, 0o644)
        assert load_credential("PARKOUR_TEST_KEY", path) == ""
        assert self._mode(path) == 0o600

    def test_windows_mode_bits_are_not_consulted(self, tmp_path, caplog, monkeypatch):
        """Python's reported mode does not describe the NTFS ACL, so the same
        test on Windows would fire on every file regardless of who can read it.
        """
        from parkour_mcp import common

        path = tmp_path / "key"
        path.write_text("s3cret")
        if os.name == "posix":
            os.chmod(path, 0o644)
        monkeypatch.setattr(common.os, "name", "nt")
        with caplog.at_level(logging.WARNING, logger="parkour_mcp.common"):
            assert load_credential("PARKOUR_TEST_KEY", path) == "s3cret"
        assert caplog.text == ""
        if os.name == "posix":
            assert self._mode(path) == 0o644, "must not touch the file either"


class TestIsPrivateIp:
    """Unit tests for _is_private_ip helper."""

    @pytest.mark.parametrize("addr", [
        "127.0.0.1",       # IPv4 loopback
        "10.0.0.1",        # RFC 1918
        "172.16.0.1",      # RFC 1918
        "192.168.1.1",     # RFC 1918
        "169.254.169.254", # link-local (cloud metadata)
        "0.0.0.0",  # noqa: S104 - unspecified addr; test input asserting it's flagged
        "::1",             # IPv6 loopback
        "fe80::1",         # IPv6 link-local
        "fc00::1",         # IPv6 unique local
        "fd12::1",         # IPv6 unique local
        "100.64.0.1",      # RFC 6598 shared address space (carrier-grade NAT)
        "100.100.100.200", # RFC 6598, and Alibaba Cloud's metadata endpoint
        "224.0.0.1",       # IPv4 multicast, which reports is_global true
        "239.0.0.1",       # IPv4 administratively-scoped multicast
        "ff02::1",         # IPv6 link-local multicast
        "64:ff9b::7f00:1", # NAT64 of 127.0.0.1, also is_global true
        "198.18.0.1",      # RFC 2544 benchmarking
        "240.0.0.1",       # reserved for future use
        "203.0.113.5",     # TEST-NET-3
    ])
    def test_private_addresses(self, addr):
        assert _is_private_ip(addr) is True

    @pytest.mark.parametrize("addr", [
        "8.8.8.8",
        "1.1.1.1",
        "142.250.80.46",
        "2607:f8b0:4004:800::200e",  # Google IPv6
    ])
    def test_public_addresses(self, addr):
        assert _is_private_ip(addr) is False

    def test_invalid_address(self):
        assert _is_private_ip("not-an-ip") is False


class TestCheckUrlSsrf:
    """Unit tests for check_url_ssrf."""

    def test_blocks_localhost_ip(self):
        result = check_url_ssrf("http://127.0.0.1/admin")
        assert result is not None
        assert "private/reserved" in result

    def test_blocks_private_ip(self):
        result = check_url_ssrf("http://192.168.1.1/")
        assert result is not None
        assert "private/reserved" in result

    def test_blocks_metadata_endpoint(self):
        result = check_url_ssrf("http://169.254.169.254/latest/meta-data/")
        assert result is not None
        assert "private/reserved" in result

    def test_blocks_ipv6_loopback(self):
        result = check_url_ssrf("http://[::1]/")
        assert result is not None
        assert "private/reserved" in result

    def test_blocks_ipv6_link_local(self):
        result = check_url_ssrf("http://[fe80::1]/")
        assert result is not None
        assert "private/reserved" in result

    def test_allows_public_ip(self):
        assert check_url_ssrf("http://8.8.8.8/") is None

    def test_blocks_hostname_resolving_to_private(self):
        """Hostname that DNS-resolves to a private IP should be blocked."""
        fake_addrinfo = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]
        with patch("parkour_mcp.common.socket.getaddrinfo", return_value=fake_addrinfo):
            result = check_url_ssrf("http://evil.example.com/steal")
            assert result is not None
            assert "private/reserved" in result

    def test_allows_hostname_resolving_to_public(self):
        """Hostname that DNS-resolves to a public IP should pass."""
        fake_addrinfo = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.80.46", 0)),
        ]
        with patch("parkour_mcp.common.socket.getaddrinfo", return_value=fake_addrinfo):
            result = check_url_ssrf("http://example.com/page")
            assert result is None

    def test_dns_failure_passes_through(self):
        """DNS resolution failure should not block — let httpx report the error."""
        with patch("parkour_mcp.common.socket.getaddrinfo", side_effect=socket.gaierror):
            result = check_url_ssrf("http://nonexistent.invalid/")
            assert result is None

    def test_allows_when_env_override_set(self):
        """MCP_ALLOW_PRIVATE_IPS=1 should bypass all checks."""
        with patch("parkour_mcp.common._ALLOW_PRIVATE_IPS", new=True):
            assert check_url_ssrf("http://127.0.0.1/admin") is None
            assert check_url_ssrf("http://192.168.1.1/") is None
            assert check_url_ssrf("http://[::1]/") is None

    def test_no_hostname(self):
        """Malformed URL with no hostname should pass through."""
        assert check_url_ssrf("not-a-url") is None

    def test_blocks_mixed_resolution(self):
        """If any resolved address is private, block the request."""
        fake_addrinfo = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.80.46", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]
        with patch("parkour_mcp.common.socket.getaddrinfo", return_value=fake_addrinfo):
            result = check_url_ssrf("http://dual-homed.example.com/")
            assert result is not None


class TestCheckUrlScheme:
    """Unit tests for check_url_scheme."""

    @pytest.mark.parametrize("url", [
        "http://example.com/page",
        "https://example.com/page",
        "HTTPS://EXAMPLE.COM/PAGE",
    ])
    def test_allows_http_and_https(self, url):
        assert check_url_scheme(url) is None

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "gopher://127.0.0.1:6379/_INFO",
        "dict://127.0.0.1:11211/stat",
        "ftp://example.com/pub",
        "data:text/html,<h1>inline</h1>",
        "javascript:alert(1)",
    ])
    def test_rejects_other_schemes(self, url):
        result = check_url_scheme(url)
        assert result is not None
        assert "Unsupported URL scheme" in result

    def test_rejects_missing_scheme(self):
        result = check_url_scheme("example.com/page")
        assert result is not None
        assert "must be absolute" in result

    def test_env_override_does_not_relax_scheme(self):
        """MCP_ALLOW_PRIVATE_IPS opts into private *hosts* for local network
        crawling.  It must not also re-open non-network schemes, or enabling
        local crawling would silently restore arbitrary local file reads."""
        with patch("parkour_mcp.common._ALLOW_PRIVATE_IPS", new=True):
            assert check_url_ssrf("http://127.0.0.1/admin") is None
            assert check_url_scheme("file:///etc/passwd") is not None

    def test_address_guard_does_not_cover_file_urls(self):
        """The two checks are not redundant.  A file:// URL has no hostname,
        so check_url_ssrf finds nothing to resolve and passes it through;
        scheme is the only property that distinguishes it."""
        assert check_url_ssrf("file:///etc/passwd") is None
        assert check_url_scheme("file:///etc/passwd") is not None


class TestGuardedTransport:
    """The address check binds to the connection only when no proxy is
    configured.  Behind a proxy the backend is handed the proxy's address
    rather than the destination's, so the check moves a layer up and the
    guarantee weakens; `pinned` is what records which applies."""

    def test_pins_when_no_proxy(self, no_proxy_env):
        assert _GuardedTransport().pinned is True

    def test_does_not_pin_behind_proxy(self, no_proxy_env):
        assert _GuardedTransport(proxy="http://127.0.0.1:8888").pinned is False

    @pytest.mark.asyncio
    async def test_blocks_private_literal(self):
        with pytest.raises(BlockedAddress, match="127.0.0.1"):
            await _resolve_and_check("127.0.0.1", 80)

    @pytest.mark.asyncio
    async def test_allows_public_literal(self):
        assert await _resolve_and_check("8.8.8.8", 443) == ["8.8.8.8"]

    @pytest.mark.asyncio
    async def test_rejects_if_any_resolved_address_is_private(self):
        """A name carrying both a public and a private record is refused.
        Checking only the first would let whoever controls the zone choose
        which record gets used."""
        fake = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.80.46", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0)),
        ]
        loop = asyncio.get_running_loop()
        with (
            patch.object(loop, "getaddrinfo", new=AsyncMock(return_value=fake)),
            pytest.raises(BlockedAddress, match="10.0.0.1"),
        ):
            await _resolve_and_check("dual-homed.example.com", 443)

    @pytest.mark.asyncio
    async def test_env_override_disables_address_check(self):
        with patch("parkour_mcp.common._ALLOW_PRIVATE_IPS", new=True):
            assert await _resolve_and_check("127.0.0.1", 80) == ["127.0.0.1"]

    def test_guarded_client_honors_environment_proxy(self, no_proxy_env, monkeypatch):
        """httpx computes `allow_env_proxies = trust_env and transport is
        None`, so passing a transport makes it skip HTTPS_PROXY entirely.
        guarded_client reproduces the proxy map itself; without that a
        configured egress proxy is silently dropped, every transport reports
        pinned, and proxy_warning() describes a degradation that did not
        happen while staying silent about the one that did."""
        monkeypatch.setenv("HTTPS_PROXY", "http://egress:3128")
        client = guarded_client()
        assert client._mounts, "environment proxy was dropped"
        for transport in client._mounts.values():
            assert isinstance(transport, _GuardedTransport)
            assert transport.pinned is False

    def test_guarded_client_pins_when_unproxied(self, no_proxy_env):
        client = guarded_client()
        assert client._mounts == {}
        transport = client._transport
        assert isinstance(transport, _GuardedTransport)
        assert transport.pinned is True

    @pytest.mark.parametrize("kwarg", ["transport", "mounts"])
    def test_guarded_client_refuses_guard_replacing_kwargs(self, kwarg):
        """Both would replace the transport that performs the check, and
        httpx would accept them silently."""
        with pytest.raises(TypeError, match="address check"):
            guarded_client(**{kwarg: httpx.AsyncHTTPTransport()})

    def test_explicit_proxy_still_gets_a_guarded_transport(self, no_proxy_env):
        """httpx builds a plain AsyncHTTPTransport for an explicit proxy=
        mount, so the proxy has to be routed through _GuardedTransport
        instead of handed to AsyncClient."""
        client = guarded_client(proxy="http://egress:3128")
        assert client._mounts
        for transport in client._mounts.values():
            assert isinstance(transport, _GuardedTransport)
            assert transport.pinned is False

    @pytest.mark.asyncio
    async def test_falls_through_to_the_next_validated_address(self):
        """Pinning bypasses anyio's Happy Eyeballs (RFC 6555), which resolves
        and walks the address list itself.  Without an equivalent walk here a
        host whose first address is unreachable fails outright, which is the
        dual-stack case the RFC exists for.  Falling through costs no reach:
        _resolve_and_check refuses the whole name if any address is refused,
        so everything in the list already passed."""
        tried: list[str] = []

        class _Inner(httpcore.AsyncNetworkBackend):
            async def connect_tcp(
                self, host, port, timeout=None, local_address=None,
                socket_options=None,
            ):
                tried.append(host)
                if host == "142.250.80.46":
                    raise httpcore.ConnectError("unreachable")
                return "stream"

        fake = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.80.46", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0)),
        ]
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", new=AsyncMock(return_value=fake)):
            result = await _PinningBackend(_Inner()).connect_tcp("dual.example", 443)

        assert result == "stream"
        assert tried == ["142.250.80.46", "1.1.1.1"]

    @pytest.mark.asyncio
    async def test_last_address_failure_propagates(self):
        """Exhausting the list must raise, not fall out of the loop."""
        class _Inner(httpcore.AsyncNetworkBackend):
            async def connect_tcp(
                self, host, port, timeout=None, local_address=None,
                socket_options=None,
            ):
                raise httpcore.ConnectError("unreachable")

        fake = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.80.46", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0)),
        ]
        loop = asyncio.get_running_loop()
        with (
            patch.object(loop, "getaddrinfo", new=AsyncMock(return_value=fake)),
            pytest.raises(httpcore.ConnectError),
        ):
            await _PinningBackend(_Inner()).connect_tcp("dual.example", 443)

    def test_blocked_address_is_a_request_error(self):
        """Callers already handle httpx.RequestError, so a blocked target
        surfaces through their existing error path rather than escaping."""
        assert issubclass(BlockedAddress, httpx.RequestError)


class TestProxyInEffect:
    def test_false_when_unset(self, no_proxy_env):
        assert proxy_in_effect() is False

    @pytest.mark.parametrize("var", _PROXY_ENV_VARS)
    def test_true_for_each_spelling(self, no_proxy_env, monkeypatch, var):
        monkeypatch.setenv(var, "http://127.0.0.1:8888")
        assert proxy_in_effect() is True


class TestGuardedFetchHttp2Fallback:
    """guarded_fetch issues HTTP/2 and pivots to HTTP/1.1 on a broken-h2 server.

    ALPN downgrade for HTTP/1.1-only origins is automatic inside httpx and
    needs no fallback; the only failure these tests cover is an origin that
    *negotiates* HTTP/2 and then violates the protocol mid-flight.
    """

    @pytest.mark.asyncio
    @respx.mock
    async def test_remote_protocol_error_retries_on_http1(self):
        """RemoteProtocolError on the first (HTTP/2) attempt retries once, and
        the HTTP/1.1 attempt's response is returned."""
        route = respx.get("https://example.com/doc").mock(
            side_effect=[
                httpx.RemoteProtocolError("server broke HTTP/2"),
                httpx.Response(200, text="recovered"),
            ]
        )
        resp = await guarded_fetch("https://example.com/doc")
        assert resp.status_code == 200
        assert resp.text == "recovered"
        assert route.call_count == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_remote_protocol_error_both_attempts_propagates(self):
        """If the HTTP/1.1 retry also fails, the error surfaces — exactly one
        retry, no loop."""
        route = respx.get("https://example.com/doc").mock(
            side_effect=httpx.RemoteProtocolError("broken both ways")
        )
        with pytest.raises(httpx.RemoteProtocolError):
            await guarded_fetch("https://example.com/doc")
        assert route.call_count == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_clean_response_makes_no_retry(self):
        """A clean first response is returned with a single request."""
        route = respx.get("https://example.com/doc").mock(
            return_value=httpx.Response(200, text="ok")
        )
        resp = await guarded_fetch("https://example.com/doc")
        assert resp.status_code == 200
        assert resp.text == "ok"
        assert route.call_count == 1
