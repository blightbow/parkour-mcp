"""Software package inspection via deps.dev (Google Open Source Insights).

Provides a standalone Packages tool with version lookup, dependency graphs,
security advisories, and OpenSSF Scorecard data across 7 ecosystems:
npm, PyPI, Go, Maven, Cargo, NuGet, and RubyGems.

Uses httpx directly for consistency with the rest of the codebase.
No authentication required.
"""

import asyncio
import logging
from typing import Annotated, Optional
from urllib.parse import quote

import httpx
from pydantic import Field

from .common import _API_HEADERS, RateLimiter
from .markdown import _build_frontmatter, _fence_content

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEPSDEV_BASE = "https://api.deps.dev/v3"

# Friendly ecosystem names → deps.dev system enum values.
_SYSTEM_ALIASES: dict[str, str] = {
    "pypi": "PYPI",
    "npm": "NPM",
    "cargo": "CARGO",
    "crates": "CARGO",
    "go": "GO",
    "golang": "GO",
    "maven": "MAVEN",
    "nuget": "NUGET",
    "rubygems": "RUBYGEMS",
    "gems": "RUBYGEMS",
}

_VALID_ECOSYSTEMS = "pypi, npm, cargo, go, maven, nuget, rubygems"

# Display labels for API system enum values.
_SYSTEM_LABELS: dict[str, str] = {
    "PYPI": "PyPI",
    "NPM": "npm",
    "CARGO": "Cargo",
    "GO": "Go",
    "MAVEN": "Maven",
    "NUGET": "NuGet",
    "RUBYGEMS": "RubyGems",
}

_MAX_VERSIONS = 20

# ---------------------------------------------------------------------------
# Rate limiter — 1 req/sec politeness (no documented limit, Google API ToS)
# ---------------------------------------------------------------------------

_depsdev_limiter = RateLimiter(1.0)

# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


async def _depsdev_get(path: str) -> dict | str:
    """GET a deps.dev API path.  Returns parsed JSON or an error string."""
    await _depsdev_limiter.wait()
    url = f"{_DEPSDEV_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=_API_HEADERS)
    except httpx.TimeoutException:
        return "Error: deps.dev API request timed out."
    except httpx.RequestError as exc:
        return f"Error: deps.dev API request failed — {type(exc).__name__}"

    if resp.status_code == 200:
        data = resp.json()
        # API always returns objects for our endpoints; guard against arrays
        if not isinstance(data, dict):
            return "Error: Unexpected response format from deps.dev."
        return data
    if resp.status_code == 404:
        return "Error: Not found on deps.dev."
    return f"Error: deps.dev API returned HTTP {resp.status_code}."


# ---------------------------------------------------------------------------
# Query parsing
# ---------------------------------------------------------------------------


def _resolve_system(name: str) -> Optional[str]:
    """Map a friendly ecosystem name to the deps.dev system enum.

    Returns the uppercase system string (e.g. ``"PYPI"``) or None.
    """
    return _SYSTEM_ALIASES.get(name.lower())


def _parse_query(query: str) -> tuple[Optional[str], str, Optional[str]]:
    """Parse ``ecosystem/name[@version]`` into ``(system, name, version)``.

    Returns ``(None, "", None)`` if the format is invalid.
    """
    slash = query.find("/")
    if slash < 1:
        return None, "", None

    eco = query[:slash]
    system = _resolve_system(eco)
    if system is None:
        return None, eco, None  # keep raw eco for error message

    rest = query[slash + 1:]
    if not rest:
        return None, "", None

    # Split version on last '@' — package names can contain '@' (npm scoped)
    at = rest.rfind("@")
    if at > 0:
        name = rest[:at]
        version = rest[at + 1:]
        return system, name, version if version else None

    return system, rest, None


def _encode_name(name: str) -> str:
    """URL-encode a package name for deps.dev API paths."""
    return quote(name, safe="")


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _format_package(
    pkg_data: dict,
    ver_data: Optional[dict],
    system: str,
    name: str,
) -> str:
    """Format package overview with latest version details."""
    label = _SYSTEM_LABELS.get(system, system)
    versions = pkg_data.get("versions") or []

    # Find default version
    default_ver = None
    for v in versions:
        if v.get("isDefault"):
            default_ver = v
            break

    # Sort by publish date descending for display
    dated = [v for v in versions if v.get("publishedAt")]
    dated.sort(key=lambda v: v["publishedAt"], reverse=True)
    recent = dated[:_MAX_VERSIONS]

    lines = [f"# {name} ({label})\n"]

    # Latest version summary from ver_data (enriched detail call)
    if ver_data and not isinstance(ver_data, str):
        vk = ver_data.get("versionKey", {})
        ver_num = vk.get("version", "")
        published = ver_data.get("publishedAt", "")
        licenses = ver_data.get("licenses") or []
        advisories = ver_data.get("advisoryKeys") or []
        links = ver_data.get("links") or []
        status = ver_data.get("projectStatus", {})

        lines.append(f"**Latest version:** {ver_num}")
        if published:
            lines.append(f"**Published:** {published[:10]}")
        if licenses:
            lines.append(f"**License:** {', '.join(licenses)}")
        if status.get("status"):
            lines.append(f"**Status:** {status['status']}")
        if advisories:
            lines.append(f"**Advisories:** {len(advisories)}")
        else:
            lines.append("**Advisories:** 0")

        # Links
        link_lines = []
        for lnk in links:
            link_label = lnk.get("label", "").replace("_", " ").title()
            link_url = lnk.get("url", "")
            if link_url:
                link_lines.append(f"- {link_label}: {link_url}")
        if link_lines:
            lines.append("")
            lines.append("**Links:**")
            lines.extend(link_lines)
    elif default_ver:
        ver_num = default_ver["versionKey"].get("version", "")
        published = default_ver.get("publishedAt", "")
        lines.append(f"**Latest version:** {ver_num}")
        if published:
            lines.append(f"**Published:** {published[:10]}")

    # Deprecation notice
    if default_ver and default_ver.get("isDeprecated"):
        reason = default_ver.get("deprecatedReason", "")
        lines.append(f"\n**DEPRECATED:** {reason}" if reason else "\n**DEPRECATED**")

    lines.append("")

    # Version table
    if recent:
        lines.append("## Recent Versions\n")
        lines.append("| Version | Published | Deprecated |")
        lines.append("|---------|-----------|------------|")
        for v in recent:
            vnum = v["versionKey"].get("version", "")
            pub = v.get("publishedAt", "")[:10]
            dep = "yes" if v.get("isDeprecated") else ""
            lines.append(f"| {vnum} | {pub} | {dep} |")

        total = len(versions)
        if total > _MAX_VERSIONS:
            lines.append(
                f"\n*{_MAX_VERSIONS} of {total} versions shown. "
                "Use version action for specific version details.*"
            )

    return "\n".join(lines)


def _format_version(ver_data: dict, system: str, name: str) -> str:
    """Format specific version details."""
    label = _SYSTEM_LABELS.get(system, system)
    vk = ver_data.get("versionKey", {})
    version = vk.get("version", "")
    published = ver_data.get("publishedAt", "")
    licenses = ver_data.get("licenses") or []
    advisories = ver_data.get("advisoryKeys") or []
    links = ver_data.get("links") or []
    slsa = ver_data.get("slsaProvenances") or []
    attestations = ver_data.get("attestations") or []
    registries = ver_data.get("registries") or []
    status = ver_data.get("projectStatus", {})
    deprecated = ver_data.get("isDeprecated", False)
    deprecated_reason = ver_data.get("deprecatedReason", "")

    lines = [f"# {name} {version} ({label})\n"]

    if licenses:
        lines.append(f"**License:** {', '.join(licenses)}")
    if published:
        lines.append(f"**Published:** {published}")
    if status.get("status"):
        lines.append(f"**Status:** {status['status']}")
    if deprecated:
        lines.append(f"**DEPRECATED:** {deprecated_reason}" if deprecated_reason else "**DEPRECATED**")

    # SLSA / attestations
    if slsa:
        lines.append(f"**SLSA provenance:** {len(slsa)} attestation(s)")
    if attestations:
        lines.append(f"**Attestations:** {len(attestations)}")

    # Links
    if links:
        lines.append("")
        lines.append("**Links:**")
        for lnk in links:
            link_label = lnk.get("label", "").replace("_", " ").title()
            link_url = lnk.get("url", "")
            if link_url:
                lines.append(f"- {link_label}: {link_url}")

    # Advisories
    lines.append("")
    if advisories:
        lines.append(f"## Advisories ({len(advisories)})\n")
        for adv in advisories:
            adv_id = adv.get("id", "")
            lines.append(f"- {adv_id}")
    else:
        lines.append("## Advisories\n")
        lines.append("None known.")

    # Registries
    if registries:
        lines.append("")
        lines.append("## Registries\n")
        for reg in registries:
            lines.append(f"- {reg}")

    return "\n".join(lines)


def _format_dependencies(
    deps_data: dict,
    reqs_data: Optional[dict],
    system: str,
    name: str,
    version: str,
) -> str:
    """Format merged dependency graph and requirements."""
    label = _SYSTEM_LABELS.get(system, system)
    lines = [f"# Dependencies: {name} {version} ({label})\n"]

    # --- Requirements (declared constraints) ---
    # reqs_data has system-specific keys (e.g. "pypi", "npm", "maven")
    req_deps = []
    if reqs_data and isinstance(reqs_data, dict):
        for sys_key, sys_reqs in reqs_data.items():
            if isinstance(sys_reqs, dict):
                req_deps = sys_reqs.get("dependencies") or []
                break

    if req_deps:
        lines.append("## Requirements (declared)\n")
        lines.append("| Package | Constraint | Condition |")
        lines.append("|---------|-----------|-----------|")
        for dep in req_deps:
            dep_name = dep.get("projectName", "")
            constraint = dep.get("versionSpecifier", "") or dep.get("requirement", "")
            marker = dep.get("environmentMarker", "")
            lines.append(f"| {dep_name} | {constraint} | {marker} |")
        lines.append("")

    # --- Resolved dependency graph ---
    nodes = deps_data.get("nodes") or []
    edges = deps_data.get("edges") or []

    if not nodes:
        lines.append("No resolved dependencies.")
        return "\n".join(lines)

    # Classify nodes by relation
    direct = []
    transitive = []
    for node in nodes:
        relation = node.get("relation", "")
        if relation == "SELF":
            continue
        if relation == "DIRECT":
            direct.append(node)
        else:
            transitive.append(node)

    lines.append("## Resolved Dependencies\n")
    lines.append(f"**Direct:** {len(direct)} packages")
    lines.append(f"**Transitive:** {len(transitive)} additional packages")
    lines.append(f"**Total:** {len(direct) + len(transitive)} packages")
    lines.append("")

    # Build edge lookup for requirement strings
    edge_reqs: dict[int, str] = {}
    for edge in edges:
        to_node = edge.get("toNode", -1)
        req = edge.get("requirement", "")
        if req:
            edge_reqs[to_node] = req

    # Direct dependencies table
    if direct:
        lines.append("| Package | Resolved | Constraint |")
        lines.append("|---------|----------|------------|")
        for node in direct:
            vk = node.get("versionKey", {})
            dep_name = vk.get("name", "")
            dep_ver = vk.get("version", "")
            # Find this node's index to look up edge requirement
            node_idx = nodes.index(node)
            req = edge_reqs.get(node_idx, "")
            lines.append(f"| {dep_name} | {dep_ver} | {req} |")

    # Transitive summary
    if transitive:
        lines.append("")
        if len(transitive) <= 20:
            lines.append("### Transitive Dependencies\n")
            lines.append("| Package | Resolved |")
            lines.append("|---------|----------|")
            for node in transitive:
                vk = node.get("versionKey", {})
                lines.append(f"| {vk.get('name', '')} | {vk.get('version', '')} |")
        else:
            lines.append(
                f"*{len(transitive)} transitive dependencies omitted. "
                "Use version action on individual packages for details.*"
            )

    return "\n".join(lines)


def _format_project(proj_data: dict) -> str:
    """Format project health and OpenSSF Scorecard."""
    project_id = proj_data.get("projectKey", {}).get("id", "")
    # Extract owner/repo from project ID like "github.com/psf/requests"
    display_name = project_id.removeprefix("github.com/") if project_id.startswith("github.com/") else project_id

    description = proj_data.get("description", "")
    stars = proj_data.get("starsCount", 0)
    forks = proj_data.get("forksCount", 0)
    issues = proj_data.get("openIssuesCount", 0)
    license_ = proj_data.get("license", "")

    lines = [f"# {display_name}\n"]

    if description:
        lines.append(f"**Description:** {description}")
    lines.append(f"**Stars:** {stars:,} | **Forks:** {forks:,} | **Open issues:** {issues:,}")
    if license_:
        lines.append(f"**License:** {license_}")
    lines.append("")

    # OpenSSF Scorecard
    scorecard = proj_data.get("scorecard")
    if scorecard:
        overall = scorecard.get("overallScore")
        checks = scorecard.get("checks") or []
        sc_date = scorecard.get("date", "")[:10]

        lines.append("## OpenSSF Scorecard\n")
        if overall is not None:
            lines.append(f"**Overall score:** {overall}/10")
        if sc_date:
            lines.append(f"**Assessed:** {sc_date}")
        lines.append("")

        # Show checks scoring <=5 (weak spots)
        weak = [c for c in checks if isinstance(c.get("score"), (int, float)) and c["score"] <= 5 and c["score"] >= 0]
        if weak:
            lines.append("### Checks Scoring 5 or Below\n")
            lines.append("| Check | Score | Reason |")
            lines.append("|-------|-------|--------|")
            for c in sorted(weak, key=lambda x: x.get("score", 0)):
                cname = c.get("name", "")
                cscore = c.get("score", "")
                creason = c.get("reason", "")
                lines.append(f"| {cname} | {cscore}/10 | {creason} |")
            above = len(checks) - len(weak)
            if above:
                lines.append(f"\n*{above} of {len(checks)} checks scored above 5/10.*")
        else:
            lines.append(f"All {len(checks)} checks scored above 5/10.")
        lines.append("")
    else:
        lines.append("## OpenSSF Scorecard\n")
        lines.append("No scorecard data available.")
        lines.append("")

    # OSS-Fuzz
    fuzz = proj_data.get("ossFuzz")
    if fuzz:
        line_count = fuzz.get("lineCount", 0)
        cover_count = fuzz.get("lineCoverCount", 0)
        fuzz_date = fuzz.get("date", "")[:10]
        coverage_pct = (cover_count / line_count * 100) if line_count else 0
        lines.append("## OSS-Fuzz\n")
        lines.append(f"**Coverage:** {cover_count:,}/{line_count:,} lines ({coverage_pct:.1f}%)")
        if fuzz_date:
            lines.append(f"**Last assessed:** {fuzz_date}")
    else:
        lines.append("## OSS-Fuzz\n")
        lines.append("Not enrolled.")

    return "\n".join(lines)


def _format_advisory(adv_data: dict) -> str:
    """Format security advisory details."""
    adv_id = adv_data.get("advisoryKey", {}).get("id", "")
    title = adv_data.get("title", "")
    aliases = adv_data.get("aliases") or []
    cvss_score = adv_data.get("cvss3Score")
    cvss_vector = adv_data.get("cvss3Vector", "")
    osv_url = adv_data.get("url", "")

    lines = [f"# {adv_id}\n"]

    if title:
        lines.append(f"**Title:** {title}")
    if aliases:
        lines.append(f"**CVEs:** {', '.join(aliases)}")
    if cvss_score is not None:
        severity = _cvss_severity(cvss_score)
        lines.append(f"**CVSS:** {cvss_score} ({severity})")
        if cvss_vector:
            lines.append(f"**Vector:** {cvss_vector}")
    if osv_url:
        lines.append(f"**OSV:** {osv_url}")

    return "\n".join(lines)


def _cvss_severity(score: float) -> str:
    """Map a CVSS 3.x score to a severity label."""
    if score >= 9.0:
        return "Critical"
    if score >= 7.0:
        return "High"
    if score >= 4.0:
        return "Medium"
    if score > 0.0:
        return "Low"
    return "None"


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


async def _action_package(system: str, name: str) -> str:
    """Handle the ``package`` action."""
    label = _SYSTEM_LABELS.get(system, system)
    encoded = _encode_name(name)

    # Step 1: fetch package (version list)
    pkg = await _depsdev_get(f"/systems/{system}/packages/{encoded}")
    if isinstance(pkg, str):
        return pkg

    # Step 2: find default version, fetch its details
    versions = pkg.get("versions") or []
    default_ver = None
    for v in versions:
        if v.get("isDefault"):
            default_ver = v
            break

    ver_data = None
    if default_ver:
        ver_num = default_ver["versionKey"].get("version", "")
        encoded_ver = _encode_name(ver_num)
        result = await _depsdev_get(
            f"/systems/{system}/packages/{encoded}/versions/{encoded_ver}"
        )
        if isinstance(result, dict):
            ver_data = result

    # Find source repo for see_also
    repo_link = None
    if ver_data:
        for lnk in ver_data.get("links") or []:
            if lnk.get("label") == "SOURCE_REPO":
                repo_link = lnk.get("url", "")
                break

    body = _format_package(pkg, ver_data, system, name)

    # Frontmatter
    default_version = default_ver["versionKey"].get("version", "") if default_ver else ""
    deprecated = default_ver.get("isDeprecated", False) if default_ver else False
    advisory_count = len(ver_data.get("advisoryKeys") or []) if ver_data else 0

    fm = _build_frontmatter({
        "source": f"https://deps.dev/{label.lower()}/{quote(name, safe='')}",
        "api": "deps.dev",
        "ecosystem": label,
        "default_version": default_version or None,
        "versions": f"{len(versions)} total",
        "note": "Package is deprecated" if deprecated else (
            f"{advisory_count} known security advisory(ies) on latest version"
            if advisory_count > 0 else None
        ),
        "hint": (
            f"Use dependencies action with {system.lower()}/{name}@{default_version} "
            "for dependency graph"
        ) if default_version else None,
        "see_also": (
            "Use project action with the repository URL for OpenSSF Scorecard"
            if repo_link else None
        ),
    })

    return fm + "\n\n" + _fence_content(body, title=None)


async def _action_version(system: str, name: str, version: str) -> str:
    """Handle the ``version`` action."""
    label = _SYSTEM_LABELS.get(system, system)
    encoded = _encode_name(name)
    encoded_ver = _encode_name(version)

    ver = await _depsdev_get(
        f"/systems/{system}/packages/{encoded}/versions/{encoded_ver}"
    )
    if isinstance(ver, str):
        return ver

    advisories = ver.get("advisoryKeys") or []
    repo_link = None
    for lnk in ver.get("links") or []:
        if lnk.get("label") == "SOURCE_REPO":
            repo_link = lnk.get("url", "")
            break

    body = _format_version(ver, system, name)

    fm = _build_frontmatter({
        "source": f"https://deps.dev/{label.lower()}/{quote(name, safe='')}/{version}",
        "api": "deps.dev",
        "ecosystem": label,
        "advisories": len(advisories),
        "note": (
            f"{len(advisories)} known security advisory(ies)"
            if advisories else None
        ),
        "hint": (
            "Use advisory action for CVE details on any advisory ID listed"
            if advisories else
            f"Use dependencies action for dependency graph of {name}@{version}"
        ),
        "see_also": (
            "Use project action with the repository URL for OpenSSF Scorecard"
            if repo_link else None
        ),
    })

    return fm + "\n\n" + _fence_content(body, title=None)


async def _action_dependencies(system: str, name: str, version: str) -> str:
    """Handle the ``dependencies`` action."""
    label = _SYSTEM_LABELS.get(system, system)
    encoded = _encode_name(name)
    encoded_ver = _encode_name(version)
    base = f"/systems/{system}/packages/{encoded}/versions/{encoded_ver}"

    # Concurrent: resolved graph + native requirements
    deps_result, reqs_result = await asyncio.gather(
        _depsdev_get(f"{base}:dependencies"),
        _depsdev_get(f"{base}:requirements"),
        return_exceptions=True,
    )

    deps = deps_result if isinstance(deps_result, dict) else None
    reqs = reqs_result if isinstance(reqs_result, dict) else None

    if deps is None:
        # Both failed — return the error from deps
        err = deps_result if isinstance(deps_result, str) else "Error: Failed to fetch dependency graph."
        return err

    # Count direct/transitive for frontmatter
    nodes = deps.get("nodes") or []
    direct_count = sum(1 for n in nodes if n.get("relation") == "DIRECT")
    transitive_count = sum(1 for n in nodes if n.get("relation") not in ("SELF", "DIRECT"))

    body = _format_dependencies(deps, reqs, system, name, version)

    fm = _build_frontmatter({
        "api": "deps.dev",
        "ecosystem": label,
        "action": "dependencies",
        "package": f"{name}@{version}",
        "direct_deps": direct_count,
        "transitive_deps": transitive_count,
        "hint": "Use version action on any dependency for its license and advisory details",
    })

    return fm + "\n\n" + _fence_content(body, title=None)


async def _action_project(query: str) -> str:
    """Handle the ``project`` action."""
    project_id = query.strip()
    encoded = quote(project_id, safe="")

    proj = await _depsdev_get(f"/projects/{encoded}")
    if isinstance(proj, str):
        return proj

    body = _format_project(proj)

    # Extract scorecard score for frontmatter
    scorecard = proj.get("scorecard")
    sc_score = scorecard.get("overallScore") if scorecard else None

    # Build source URL
    source_url = None
    if project_id.startswith("github.com/"):
        source_url = f"https://{project_id}"

    fm = _build_frontmatter({
        "source": source_url,
        "api": "deps.dev",
        "action": "project",
        "scorecard_score": f"{sc_score}/10" if sc_score is not None else None,
        "hint": "Use GitHub tool for repo README, issues, and code search",
    })

    return fm + "\n\n" + _fence_content(body, title=None)


async def _action_advisory(query: str) -> str:
    """Handle the ``advisory`` action."""
    adv_id = query.strip()

    adv = await _depsdev_get(f"/advisories/{_encode_name(adv_id)}")
    if isinstance(adv, str):
        return adv

    body = _format_advisory(adv)

    osv_url = adv.get("url", "")

    fm = _build_frontmatter({
        "api": "deps.dev",
        "action": "advisory",
        "source": osv_url or None,
        "hint": "Use version action to check which package versions are affected",
    })

    return fm + "\n\n" + _fence_content(body, title=None)


# ---------------------------------------------------------------------------
# Standalone MCP tool
# ---------------------------------------------------------------------------


async def packages(
    action: Annotated[str, Field(
        description=(
            "The operation to perform. "
            "package: get package info and recent versions (query: ecosystem/name, e.g. pypi/requests). "
            "version: get specific version details with license and advisories "
            "(query: ecosystem/name@version, e.g. pypi/requests@2.32.3). "
            "dependencies: get dependency graph with resolved versions "
            "(query: ecosystem/name@version). "
            "project: get repo health and OpenSSF Scorecard "
            "(query: github.com/owner/repo, e.g. github.com/psf/requests). "
            "advisory: get security advisory details "
            "(query: advisory ID, e.g. GHSA-9hjg-9r4m-mvj7)."
        ),
    )],
    query: Annotated[str, Field(
        description=(
            "Query format depends on action. "
            "For package/version/dependencies: ecosystem/name[@version] where ecosystem is "
            "one of: pypi, npm, cargo, go, maven, nuget, rubygems. "
            "For project: github.com/owner/repo. "
            "For advisory: advisory ID (e.g. GHSA-9hjg-9r4m-mvj7)."
        ),
    )],
) -> str:
    """Search and inspect software packages across language ecosystems."""
    action = action.strip().lower()

    if action == "project":
        return await _action_project(query)

    if action == "advisory":
        return await _action_advisory(query)

    # Actions that require ecosystem/name parsing
    if action in ("package", "version", "dependencies"):
        system, name, version = _parse_query(query)

        if system is None:
            if name:
                return (
                    f"Error: Unknown ecosystem '{name}'. "
                    f"Valid ecosystems: {_VALID_ECOSYSTEMS}"
                )
            return (
                "Error: Invalid query format. "
                "Expected ecosystem/package[@version] "
                "(e.g. pypi/requests or npm/express@4.18.2)"
            )

        if action == "package":
            return await _action_package(system, name)

        if version is None:
            return (
                f"Error: Version required for {action} action. "
                "Use ecosystem/name@version format "
                f"(e.g. {system.lower()}/{name}@1.0.0)"
            )

        if action == "version":
            return await _action_version(system, name, version)

        return await _action_dependencies(system, name, version)

    return (
        f"Error: Unknown action '{action}'. "
        "Valid actions: package, version, dependencies, project, advisory"
    )
