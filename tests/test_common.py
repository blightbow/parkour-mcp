"""Tests for parkour_mcp.common module."""

import socket
from unittest.mock import patch

import httpx
import pytest
import respx

from parkour_mcp.common import (
    check_url_ssrf,
    _is_private_ip,
    guarded_fetch,
    _parse_truthy_env,
    load_credential,
)


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


class TestIsPrivateIp:
    """Unit tests for _is_private_ip helper."""

    @pytest.mark.parametrize("addr", [
        "127.0.0.1",       # IPv4 loopback
        "10.0.0.1",        # RFC 1918
        "172.16.0.1",      # RFC 1918
        "192.168.1.1",     # RFC 1918
        "169.254.169.254", # link-local (cloud metadata)
        "0.0.0.0",         # unspecified
        "::1",             # IPv6 loopback
        "fe80::1",         # IPv6 link-local
        "fc00::1",         # IPv6 unique local
        "fd12::1",         # IPv6 unique local
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
        with patch("parkour_mcp.common._ALLOW_PRIVATE_IPS", True):
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
