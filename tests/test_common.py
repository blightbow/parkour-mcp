"""Tests for kagi_research_mcp.common module."""

import socket
from unittest.mock import patch

import pytest

from kagi_research_mcp.common import check_url_ssrf, _is_private_ip


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
        with patch("kagi_research_mcp.common.socket.getaddrinfo", return_value=fake_addrinfo):
            result = check_url_ssrf("http://evil.example.com/steal")
            assert result is not None
            assert "private/reserved" in result

    def test_allows_hostname_resolving_to_public(self):
        """Hostname that DNS-resolves to a public IP should pass."""
        fake_addrinfo = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.80.46", 0)),
        ]
        with patch("kagi_research_mcp.common.socket.getaddrinfo", return_value=fake_addrinfo):
            result = check_url_ssrf("http://example.com/page")
            assert result is None

    def test_dns_failure_passes_through(self):
        """DNS resolution failure should not block — let httpx report the error."""
        with patch("kagi_research_mcp.common.socket.getaddrinfo", side_effect=socket.gaierror):
            result = check_url_ssrf("http://nonexistent.invalid/")
            assert result is None

    def test_allows_when_env_override_set(self):
        """MCP_ALLOW_PRIVATE_IPS=1 should bypass all checks."""
        with patch("kagi_research_mcp.common._ALLOW_PRIVATE_IPS", True):
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
        with patch("kagi_research_mcp.common.socket.getaddrinfo", return_value=fake_addrinfo):
            result = check_url_ssrf("http://dual-homed.example.com/")
            assert result is not None
