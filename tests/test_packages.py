"""Tests for parkour_mcp.packages module."""

import httpx
import pytest
import respx

from parkour_mcp.common import _DEPSDEV_BASE, _depsdev_get
from parkour_mcp.packages import (
    _cvss_severity,
    _encode_name,
    _format_advisory,
    _format_dependencies,
    _format_package,
    _format_project,
    _format_version,
    _parse_query,
    _resolve_system,
    packages,
)


# ---------------------------------------------------------------------------
# Test fixtures — deps.dev API responses
# ---------------------------------------------------------------------------

PACKAGE_RESPONSE = {
    "packageKey": {"system": "PYPI", "name": "requests"},
    "versions": [
        {
            "versionKey": {"system": "PYPI", "name": "requests", "version": "2.31.0"},
            "publishedAt": "2023-05-22T15:12:44Z",
            "isDefault": False,
            "isDeprecated": False,
            "deprecatedReason": "",
        },
        {
            "versionKey": {"system": "PYPI", "name": "requests", "version": "2.32.3"},
            "publishedAt": "2024-05-29T15:37:47Z",
            "isDefault": True,
            "isDeprecated": False,
            "deprecatedReason": "",
        },
    ],
}

DEPRECATED_PACKAGE_RESPONSE = {
    "packageKey": {"system": "NPM", "name": "request"},
    "versions": [
        {
            "versionKey": {"system": "NPM", "name": "request", "version": "2.88.2"},
            "publishedAt": "2019-12-18T00:00:00Z",
            "isDefault": True,
            "isDeprecated": True,
            "deprecatedReason": "request has been deprecated, see https://github.com/request/request/issues/3142",
        },
    ],
}

VERSION_RESPONSE = {
    "versionKey": {"system": "PYPI", "name": "requests", "version": "2.32.3"},
    "publishedAt": "2024-05-29T15:37:47Z",
    "isDefault": True,
    "isDeprecated": False,
    "deprecatedReason": "",
    "licenses": ["Apache-2.0"],
    "advisoryKeys": [
        {"id": "GHSA-9hjg-9r4m-mvj7"},
        {"id": "GHSA-gc5v-m9x4-r6x2"},
    ],
    "links": [
        {"label": "DOCUMENTATION", "url": "https://requests.readthedocs.io"},
        {"label": "SOURCE_REPO", "url": "https://github.com/psf/requests"},
    ],
    "slsaProvenances": [],
    "attestations": [],
    "registries": ["https://pypi.org/simple"],
    "relatedProjects": [
        {
            "projectKey": {"id": "github.com/psf/requests"},
            "relationProvenance": "UNVERIFIED_METADATA",
            "relationType": "SOURCE_REPO",
        },
    ],
    "projectStatus": {"status": "active", "reason": ""},
}

VERSION_NO_ADVISORIES = {
    "versionKey": {"system": "CARGO", "name": "serde", "version": "1.0.228"},
    "publishedAt": "2025-09-27T16:51:35Z",
    "isDefault": True,
    "isDeprecated": False,
    "deprecatedReason": "",
    "licenses": ["MIT", "Apache-2.0"],
    "advisoryKeys": [],
    "links": [
        {"label": "SOURCE_REPO", "url": "https://github.com/serde-rs/serde"},
    ],
    "slsaProvenances": [],
    "attestations": [],
    "registries": [],
    "projectStatus": {"status": "active", "reason": ""},
}

DEPENDENCIES_RESPONSE = {
    "nodes": [
        {
            "versionKey": {"system": "PYPI", "name": "requests", "version": "2.32.3"},
            "bundled": False,
            "relation": "SELF",
            "errors": [],
        },
        {
            "versionKey": {"system": "PYPI", "name": "certifi", "version": "2026.2.25"},
            "bundled": False,
            "relation": "DIRECT",
            "errors": [],
        },
        {
            "versionKey": {"system": "PYPI", "name": "charset-normalizer", "version": "3.4.6"},
            "bundled": False,
            "relation": "DIRECT",
            "errors": [],
        },
        {
            "versionKey": {"system": "PYPI", "name": "idna", "version": "3.11.0"},
            "bundled": False,
            "relation": "DIRECT",
            "errors": [],
        },
        {
            "versionKey": {"system": "PYPI", "name": "urllib3", "version": "2.6.3"},
            "bundled": False,
            "relation": "DIRECT",
            "errors": [],
        },
    ],
    "edges": [
        {"fromNode": 0, "toNode": 1, "requirement": ">=2017.4.17"},
        {"fromNode": 0, "toNode": 2, "requirement": "<4,>=2"},
        {"fromNode": 0, "toNode": 3, "requirement": "<4,>=2.5"},
        {"fromNode": 0, "toNode": 4, "requirement": "<3,>=1.21.1"},
    ],
    "error": "",
}

REQUIREMENTS_RESPONSE = {
    "pypi": {
        "dependencies": [
            {
                "projectName": "charset-normalizer",
                "extras": "",
                "versionSpecifier": "<4,>=2",
                "environmentMarker": "",
            },
            {
                "projectName": "idna",
                "extras": "",
                "versionSpecifier": "<4,>=2.5",
                "environmentMarker": "",
            },
            {
                "projectName": "urllib3",
                "extras": "",
                "versionSpecifier": "<3,>=1.21.1",
                "environmentMarker": "",
            },
            {
                "projectName": "certifi",
                "extras": "",
                "versionSpecifier": ">=2017.4.17",
                "environmentMarker": "",
            },
            {
                "projectName": "pysocks",
                "extras": "",
                "versionSpecifier": "!=1.5.7,>=1.5.6",
                "environmentMarker": "extra == 'socks'",
            },
        ],
    },
}

PROJECT_RESPONSE = {
    "projectKey": {"id": "github.com/psf/requests"},
    "openIssuesCount": 234,
    "starsCount": 53861,
    "forksCount": 9823,
    "license": "Apache-2.0",
    "description": "A simple, yet elegant, HTTP library.",
    "homepage": "https://requests.readthedocs.io/en/latest/",
    "scorecard": {
        "date": "2026-03-23T00:00:00Z",
        "repository": {
            "name": "github.com/psf/requests",
            "commit": "abc123",
        },
        "scorecard": {"version": "v5.4.1", "commit": "def456"},
        "checks": [
            {"name": "Maintained", "score": 10, "reason": "30 commits in last 90 days", "details": []},
            {"name": "Code-Review", "score": 7, "reason": "14/18 approved changesets", "details": []},
            {"name": "Security-Policy", "score": 10, "reason": "security policy detected", "details": []},
            {"name": "Binary-Artifacts", "score": 10, "reason": "no binaries found", "details": []},
            {"name": "CII-Best-Practices", "score": 0, "reason": "no badge detected", "details": []},
            {"name": "Signed-Releases", "score": 0, "reason": "no signed releases", "details": []},
            {"name": "Fuzzing", "score": 10, "reason": "project is fuzzed", "details": []},
            {"name": "License", "score": 10, "reason": "license file detected", "details": []},
        ],
        "overallScore": 7.2,
        "metadata": [],
    },
    "ossFuzz": {
        "lineCount": 7900,
        "lineCoverCount": 3356,
        "date": "2026-03-31T00:00:00Z",
        "configUrl": "https://github.com/google/oss-fuzz/tree/master/projects/requests",
    },
}

PROJECT_NO_SCORECARD = {
    "projectKey": {"id": "github.com/some/project"},
    "openIssuesCount": 5,
    "starsCount": 100,
    "forksCount": 10,
    "license": "MIT",
    "description": "A small project.",
    "homepage": "",
}

ADVISORY_RESPONSE = {
    "advisoryKey": {"id": "GHSA-9hjg-9r4m-mvj7"},
    "url": "https://osv.dev/vulnerability/GHSA-9hjg-9r4m-mvj7",
    "title": "Requests vulnerable to .netrc credentials leak via malicious URLs",
    "aliases": ["CVE-2024-47081"],
    "cvss3Score": 5.3,
    "cvss3Vector": "CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:U/C:H/I:N/A:N",
}


# ---------------------------------------------------------------------------
# TestResolveSystem
# ---------------------------------------------------------------------------


class TestResolveSystem:
    def test_canonical_names(self):
        assert _resolve_system("pypi") == "PYPI"
        assert _resolve_system("npm") == "NPM"
        assert _resolve_system("cargo") == "CARGO"
        assert _resolve_system("go") == "GO"
        assert _resolve_system("maven") == "MAVEN"
        assert _resolve_system("nuget") == "NUGET"
        assert _resolve_system("rubygems") == "RUBYGEMS"

    def test_aliases(self):
        assert _resolve_system("crates") == "CARGO"
        assert _resolve_system("golang") == "GO"
        assert _resolve_system("gems") == "RUBYGEMS"

    def test_case_insensitive(self):
        assert _resolve_system("PyPI") == "PYPI"
        assert _resolve_system("NPM") == "NPM"
        assert _resolve_system("Cargo") == "CARGO"

    def test_unknown_returns_none(self):
        assert _resolve_system("homebrew") is None
        assert _resolve_system("docker") is None
        assert _resolve_system("") is None


# ---------------------------------------------------------------------------
# TestParseQuery
# ---------------------------------------------------------------------------


class TestParseQuery:
    def test_simple_package(self):
        system, name, version = _parse_query("pypi/requests")
        assert system == "PYPI"
        assert name == "requests"
        assert version is None

    def test_package_with_version(self):
        system, name, version = _parse_query("pypi/requests@2.32.3")
        assert system == "PYPI"
        assert name == "requests"
        assert version == "2.32.3"

    def test_scoped_npm(self):
        system, name, version = _parse_query("npm/@types/node@20.0.0")
        assert system == "NPM"
        assert name == "@types/node"
        assert version == "20.0.0"

    def test_scoped_npm_no_version(self):
        system, name, version = _parse_query("npm/@types/node")
        assert system == "NPM"
        assert name == "@types/node"
        assert version is None

    def test_maven_colon(self):
        system, name, version = _parse_query("maven/org.apache.commons:commons-lang3@3.12.0")
        assert system == "MAVEN"
        assert name == "org.apache.commons:commons-lang3"
        assert version == "3.12.0"

    def test_go_module(self):
        system, name, version = _parse_query("go/golang.org/x/net@v0.20.0")
        assert system == "GO"
        assert name == "golang.org/x/net"
        assert version == "v0.20.0"

    def test_invalid_no_slash(self):
        system, name, version = _parse_query("requests")
        assert system is None

    def test_unknown_ecosystem(self):
        system, name, version = _parse_query("homebrew/ffmpeg")
        assert system is None
        assert name == "homebrew"  # raw eco preserved for error msg

    def test_empty_name(self):
        system, name, version = _parse_query("pypi/")
        assert system is None


# ---------------------------------------------------------------------------
# TestEncodeName
# ---------------------------------------------------------------------------


class TestEncodeName:
    def test_simple(self):
        assert _encode_name("requests") == "requests"

    def test_scoped_npm(self):
        assert _encode_name("@types/node") == "%40types%2Fnode"

    def test_maven_colon(self):
        assert _encode_name("org.apache.commons:commons-lang3") == "org.apache.commons%3Acommons-lang3"

    def test_go_slashes(self):
        assert _encode_name("golang.org/x/net") == "golang.org%2Fx%2Fnet"


# ---------------------------------------------------------------------------
# TestDepsdevGet
# ---------------------------------------------------------------------------


class TestDepsdevGet:
    @respx.mock
    @pytest.mark.asyncio
    async def test_success(self):
        respx.get(f"{_DEPSDEV_BASE}/systems/PYPI/packages/requests").respond(
            200, json=PACKAGE_RESPONSE,
        )
        result = await _depsdev_get("/systems/PYPI/packages/requests")
        assert isinstance(result, dict)
        assert result["packageKey"]["name"] == "requests"

    @respx.mock
    @pytest.mark.asyncio
    async def test_404(self):
        respx.get(f"{_DEPSDEV_BASE}/systems/PYPI/packages/nonexistent").respond(404)
        result = await _depsdev_get("/systems/PYPI/packages/nonexistent")
        assert isinstance(result, str)
        assert "Not found" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_timeout(self):
        respx.get(f"{_DEPSDEV_BASE}/systems/PYPI/packages/slow").mock(
            side_effect=httpx.ReadTimeout("timed out"),
        )
        result = await _depsdev_get("/systems/PYPI/packages/slow")
        assert isinstance(result, str)
        assert "timed out" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_network_error(self):
        respx.get(f"{_DEPSDEV_BASE}/systems/PYPI/packages/broken").mock(
            side_effect=httpx.ConnectError("connection refused"),
        )
        result = await _depsdev_get("/systems/PYPI/packages/broken")
        assert isinstance(result, str)
        assert "ConnectError" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_server_error(self):
        respx.get(f"{_DEPSDEV_BASE}/systems/PYPI/packages/error").respond(500)
        result = await _depsdev_get("/systems/PYPI/packages/error")
        assert isinstance(result, str)
        assert "500" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_unexpected_array_response(self):
        respx.get(f"{_DEPSDEV_BASE}/test").respond(200, json=[1, 2, 3])
        result = await _depsdev_get("/test")
        assert isinstance(result, str)
        assert "Unexpected" in result


# ---------------------------------------------------------------------------
# TestCvssSeverity
# ---------------------------------------------------------------------------


class TestCvssSeverity:
    def test_critical(self):
        assert _cvss_severity(9.5) == "Critical"

    def test_high(self):
        assert _cvss_severity(7.0) == "High"

    def test_medium(self):
        assert _cvss_severity(5.3) == "Medium"

    def test_low(self):
        assert _cvss_severity(2.0) == "Low"

    def test_none(self):
        assert _cvss_severity(0.0) == "None"


# ---------------------------------------------------------------------------
# TestFormatters
# ---------------------------------------------------------------------------


class TestFormatPackage:
    def test_basic(self):
        result = _format_package(PACKAGE_RESPONSE, VERSION_RESPONSE, "PYPI", "requests")
        assert "requests (PyPI)" in result
        assert "2.32.3" in result
        assert "Apache-2.0" in result
        assert "2 " in result or "Advisories" in result  # advisory count

    def test_deprecated(self):
        ver_data = {
            **VERSION_RESPONSE,
            "versionKey": {"system": "NPM", "name": "request", "version": "2.88.2"},
        }
        result = _format_package(DEPRECATED_PACKAGE_RESPONSE, ver_data, "NPM", "request")
        assert "DEPRECATED" in result

    def test_no_version_detail(self):
        result = _format_package(PACKAGE_RESPONSE, None, "PYPI", "requests")
        assert "requests (PyPI)" in result
        assert "2.32.3" in result  # from default version


class TestFormatVersion:
    def test_with_advisories(self):
        result = _format_version(VERSION_RESPONSE, "PYPI", "requests")
        assert "2.32.3" in result
        assert "Apache-2.0" in result
        assert "GHSA-9hjg-9r4m-mvj7" in result
        assert "GHSA-gc5v-m9x4-r6x2" in result

    def test_no_advisories(self):
        result = _format_version(VERSION_NO_ADVISORIES, "CARGO", "serde")
        assert "serde" in result
        assert "None known" in result


class TestFormatDependencies:
    def test_basic(self):
        result = _format_dependencies(
            DEPENDENCIES_RESPONSE, REQUIREMENTS_RESPONSE,
            "PYPI", "requests", "2.32.3",
        )
        assert "certifi" in result
        assert "urllib3" in result
        assert "Direct" in result
        assert ">=2017.4.17" in result  # requirement constraint

    def test_no_requirements(self):
        result = _format_dependencies(
            DEPENDENCIES_RESPONSE, None,
            "PYPI", "requests", "2.32.3",
        )
        assert "certifi" in result
        assert "Resolved" in result

    def test_empty_graph(self):
        empty = {"nodes": [], "edges": [], "error": ""}
        result = _format_dependencies(empty, None, "PYPI", "requests", "2.32.3")
        assert "No resolved dependencies" in result


class TestFormatProject:
    def test_with_scorecard(self):
        result = _format_project(PROJECT_RESPONSE)
        assert "psf/requests" in result
        assert "53,861" in result
        # Overall score and assessment date now live in frontmatter, not
        # the body.  The body still lists weak checks scoring <= 5/10.
        assert "**Overall score:**" not in result
        assert "**Assessed:**" not in result
        assert "CII-Best-Practices" in result
        assert "Signed-Releases" in result

    def test_no_scorecard(self):
        result = _format_project(PROJECT_NO_SCORECARD)
        assert "some/project" in result
        assert "No scorecard data" in result

    def test_oss_fuzz(self):
        result = _format_project(PROJECT_RESPONSE)
        assert "OSS-Fuzz" in result
        assert "3,356" in result
        assert "7,900" in result

    def test_no_oss_fuzz(self):
        result = _format_project(PROJECT_NO_SCORECARD)
        assert "Not enrolled" in result


class TestFormatAdvisory:
    def test_basic(self):
        result = _format_advisory(ADVISORY_RESPONSE)
        assert "GHSA-9hjg-9r4m-mvj7" in result
        assert "CVE-2024-47081" in result
        assert "5.3" in result
        assert "Medium" in result
        assert "CVSS:3.1" in result


# ---------------------------------------------------------------------------
# TestPackages (integration — tool function)
# ---------------------------------------------------------------------------


class TestPackagesPackage:
    @respx.mock
    @pytest.mark.asyncio
    async def test_basic_lookup(self):
        respx.get(f"{_DEPSDEV_BASE}/systems/PYPI/packages/requests").respond(
            200, json=PACKAGE_RESPONSE,
        )
        respx.get(f"{_DEPSDEV_BASE}/systems/PYPI/packages/requests/versions/2.32.3").respond(
            200, json=VERSION_RESPONSE,
        )
        result = await packages(action="package", query="pypi/requests")
        assert "---" in result  # frontmatter
        assert "deps.dev" in result
        assert "requests" in result
        assert "untrusted content" in result  # fenced

    @pytest.mark.asyncio
    async def test_unknown_ecosystem(self):
        result = await packages(action="package", query="homebrew/ffmpeg")
        assert "Error:" in result
        assert "homebrew" in result

    @pytest.mark.asyncio
    async def test_invalid_format(self):
        result = await packages(action="package", query="requests")
        assert "Error:" in result
        assert "ecosystem/package" in result


class TestPackagesVersion:
    @respx.mock
    @pytest.mark.asyncio
    async def test_basic(self):
        respx.get(f"{_DEPSDEV_BASE}/systems/PYPI/packages/requests/versions/2.32.3").respond(
            200, json=VERSION_RESPONSE,
        )
        result = await packages(action="version", query="pypi/requests@2.32.3")
        assert "---" in result
        assert "Apache-2.0" in result
        assert "GHSA-9hjg-9r4m-mvj7" in result

    @pytest.mark.asyncio
    async def test_version_required(self):
        result = await packages(action="version", query="pypi/requests")
        assert "Error:" in result
        assert "Version required" in result


class TestPackagesDependencies:
    @respx.mock
    @pytest.mark.asyncio
    async def test_basic(self):
        base = f"{_DEPSDEV_BASE}/systems/PYPI/packages/requests/versions/2.32.3"
        respx.get(f"{base}:dependencies").respond(200, json=DEPENDENCIES_RESPONSE)
        respx.get(f"{base}:requirements").respond(200, json=REQUIREMENTS_RESPONSE)
        result = await packages(action="dependencies", query="pypi/requests@2.32.3")
        assert "---" in result
        assert "certifi" in result
        assert "direct_deps: 4" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_requirements_failure_still_works(self):
        base = f"{_DEPSDEV_BASE}/systems/PYPI/packages/requests/versions/2.32.3"
        respx.get(f"{base}:dependencies").respond(200, json=DEPENDENCIES_RESPONSE)
        respx.get(f"{base}:requirements").respond(404)
        result = await packages(action="dependencies", query="pypi/requests@2.32.3")
        # Should still show resolved deps even if requirements failed
        assert "certifi" in result

    @pytest.mark.asyncio
    async def test_version_required(self):
        result = await packages(action="dependencies", query="pypi/requests")
        assert "Error:" in result
        assert "Version required" in result


class TestPackagesProject:
    @respx.mock
    @pytest.mark.asyncio
    async def test_with_scorecard(self):
        respx.get(f"{_DEPSDEV_BASE}/projects/github.com%2Fpsf%2Frequests").respond(
            200, json=PROJECT_RESPONSE,
        )
        result = await packages(action="project", query="github.com/psf/requests")
        assert "---" in result
        # Frontmatter carries score and assessment date via the shared
        # scorecard.format_score helper; body no longer repeats them.
        assert "openssf_scorecard: 7.2/10 (@ 2026-03-23)" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_404(self):
        respx.get(f"{_DEPSDEV_BASE}/projects/github.com%2Fnot%2Ffound").respond(404)
        result = await packages(action="project", query="github.com/not/found")
        assert "Error:" in result
        assert "Not found" in result


class TestPackagesAdvisory:
    @respx.mock
    @pytest.mark.asyncio
    async def test_basic(self):
        respx.get(f"{_DEPSDEV_BASE}/advisories/GHSA-9hjg-9r4m-mvj7").respond(
            200, json=ADVISORY_RESPONSE,
        )
        result = await packages(action="advisory", query="GHSA-9hjg-9r4m-mvj7")
        assert "---" in result
        assert "CVE-2024-47081" in result
        assert "5.3" in result


class TestPackagesInvalidAction:
    @pytest.mark.asyncio
    async def test_unknown_action(self):
        result = await packages(action="search", query="anything")
        assert "Error:" in result
        assert "Unknown action" in result
        assert "package, version, dependencies, project, advisory" in result
