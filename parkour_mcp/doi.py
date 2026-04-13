"""DOI content negotiation and resolution helpers.

Provides async functions for fetching citation metadata via DOI content
negotiation (doi.org) and the DataCite REST API.  These are used for:
  - Passive enrichment of arXiv and Semantic Scholar paper responses
  - The DOI URL fast-path handler (P5)
  - DataCite metadata enrichment (P6)
"""

import asyncio
import logging
import re
from typing import Optional

import httpx

from .common import _API_USER_AGENT, RateLimiter, s2_enabled, tool_name
from .markdown import (
    _build_frontmatter,
    _fence_content,
    _format_retraction_banner,
    _TRUST_ADVISORY,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiters
# ---------------------------------------------------------------------------
# doi.org: CrossRef polite pool 10 req/s with mailto, 5 req/s without.
# DataCite via doi.org: 1,000/5min (~3.3/s). Conservative default: 5/sec.
_doi_limiter = RateLimiter(0.2)


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------
DOI_URL_RE = re.compile(
    r'https?://(?:dx\.)?doi\.org/(10\.\S+)',
    re.IGNORECASE,
)

ARXIV_DOI_RE = re.compile(
    r'^10\.48550/arXiv\.(.+)$',
    re.IGNORECASE,
)


def _detect_doi_url(url: str) -> Optional[str]:
    """Extract a bare DOI from a doi.org URL, or None."""
    m = DOI_URL_RE.search(url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Registration Agency detection (cached per-prefix)
# ---------------------------------------------------------------------------
_ra_cache: dict[str, str] = {}


async def _detect_ra(doi: str, *, timeout: float = 5.0) -> Optional[str]:
    """Detect the Registration Agency for a DOI prefix.

    Uses doi.org/doiRA/{prefix} API with in-memory caching.
    Returns "DataCite", "Crossref", etc., or None on failure.
    """
    prefix = doi.split("/")[0]  # "10.48550"
    if prefix in _ra_cache:
        return _ra_cache[prefix]

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"https://doi.org/doiRA/{prefix}",
                headers={"User-Agent": _API_USER_AGENT},
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    ra = data[0].get("RA", "")
                    _ra_cache[prefix] = ra
                    return ra
    except Exception as e:
        logger.debug("RA detection failed for %s: %s", prefix, e)
    return None


# ---------------------------------------------------------------------------
# DataCite REST API
# ---------------------------------------------------------------------------
_datacite_limiter = RateLimiter(0.1)  # 10 req/s


async def fetch_datacite_metadata(
    doi: str, *, timeout: float = 5.0,
) -> Optional[dict]:
    """Fetch enriched metadata from DataCite REST API.

    Returns a simplified dict with ORCIDs, affiliations, SPDX license,
    and related identifiers — or None on failure.
    """
    await _datacite_limiter.wait()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"https://api.datacite.org/dois/{doi}",
                headers={"User-Agent": _API_USER_AGENT, "Accept": "application/json"},
            )
            if resp.status_code != 200:
                return None
            raw = resp.json()
            attrs = raw.get("data", {}).get("attributes", {})

            # Extract ORCIDs from creators
            orcids: dict[str, str] = {}
            creators = attrs.get("creators") or []
            for c in creators:
                name = c.get("name", "")
                for ni in c.get("nameIdentifiers") or []:
                    if ni.get("nameIdentifierScheme") == "ORCID":
                        orcid_url = ni.get("nameIdentifier", "")
                        # Normalize: may be full URL or bare ID
                        orcid_id = orcid_url.replace("https://orcid.org/", "")
                        if orcid_id:
                            orcids[name] = orcid_id

            # SPDX license
            rights_list = attrs.get("rightsList") or []
            license_id = None
            license_url = None
            for r in rights_list:
                if r.get("rightsIdentifierScheme") == "SPDX":
                    license_id = r.get("rightsIdentifier")
                    license_url = r.get("rightsUri")
                    break
                # Fallback: any rights entry with a URI
                if not license_id and r.get("rightsUri"):
                    license_id = r.get("rights")
                    license_url = r.get("rightsUri")

            # Related identifiers
            related = []
            for ri in (attrs.get("relatedIdentifiers") or [])[:10]:
                related.append({
                    "type": ri.get("relatedIdentifierType"),
                    "relation": ri.get("relationType"),
                    "id": ri.get("relatedIdentifier"),
                })

            return {
                "orcids": orcids,
                "license_id": license_id,
                "license_url": license_url,
                "related": related,
                "resource_type": attrs.get("types", {}).get("resourceTypeGeneral"),
            }
    except Exception as e:
        logger.debug("DataCite fetch failed for %s: %s", doi, e)
        return None


# ---------------------------------------------------------------------------
# CrossRef REST API (retraction + adjacent enrichment)
# ---------------------------------------------------------------------------
# CrossRef polite pool: 10 req/s with mailto, 5 req/s without.
_crossref_limiter = RateLimiter(0.2)

# DOI format guard: "10." followed by registrant + "/" + suffix.  Applied
# to any DOI-ish value pulled from CrossRef before it lands in frontmatter
# (which lives outside the content fence).  This prevents an attacker who
# controls a CrossRef record from injecting structure into the trusted
# metadata zone.
_DOI_SAFE_RE = re.compile(r"^10\.\d{4,9}/[^\s\"'<>\\]+$")

# CrossRef updated-by type values we recognize.  See:
# https://gitlab.com/crossref/schema/-/blob/master/schemas/common5.3.1.xsd
_UPDATE_TYPE_RETRACTION = frozenset({"retraction", "withdrawal", "removal"})
_UPDATE_TYPE_EOC = frozenset({"expression_of_concern"})
_UPDATE_TYPE_CORRECTION = frozenset({"correction", "erratum"})
_UPDATE_TYPE_PRIORITY = (
    _UPDATE_TYPE_RETRACTION,  # highest priority
    _UPDATE_TYPE_EOC,
    _UPDATE_TYPE_CORRECTION,
)

# CrossRef relation buckets we surface to callers.
_RELATION_BUCKETS = ("is-preprint-of", "has-preprint", "is-version-of", "has-version")


def _format_crossref_date(date_parts_obj: Optional[dict]) -> Optional[str]:
    """Format a CrossRef date-parts object as ISO YYYY-MM-DD / YYYY-MM / YYYY.

    CrossRef wraps dates as ``{"date-parts": [[YYYY, MM, DD]]}`` (month/day
    optional).  Returns None for missing or malformed values.
    """
    if not isinstance(date_parts_obj, dict):
        return None
    parts = date_parts_obj.get("date-parts")
    if not parts or not isinstance(parts, list) or not parts[0]:
        return None
    dp = parts[0]
    try:
        if len(dp) >= 3:
            return f"{int(dp[0]):04d}-{int(dp[1]):02d}-{int(dp[2]):02d}"
        if len(dp) >= 2:
            return f"{int(dp[0]):04d}-{int(dp[1]):02d}"
        return f"{int(dp[0]):04d}"
    except (TypeError, ValueError):
        return None


def _classify_update_type(raw_type: str) -> Optional[str]:
    """Map a raw CrossRef update type to our normalized taxonomy.

    Returns "retraction", "expression_of_concern", "correction", or None.
    """
    rt = (raw_type or "").strip().lower()
    if rt in _UPDATE_TYPE_RETRACTION:
        return "retraction"
    if rt in _UPDATE_TYPE_EOC:
        return "expression_of_concern"
    if rt in _UPDATE_TYPE_CORRECTION:
        return "correction"
    return None


def _extract_update_notice(updated_by: list) -> tuple[Optional[dict], Optional[dict]]:
    """Scan CrossRef ``message.updated-by`` and return (retraction, other_update).

    Returns the highest-priority retraction entry as ``retraction``.  If no
    retraction is present, returns the highest-priority EoC/correction entry
    as ``other_update``.  Each dict has keys: type (for other_update only),
    notice_doi, date, source, label.
    """
    if not isinstance(updated_by, list) or not updated_by:
        return None, None

    by_class: dict[str, list[dict]] = {
        "retraction": [],
        "expression_of_concern": [],
        "correction": [],
    }
    for entry in updated_by:
        if not isinstance(entry, dict):
            continue
        normalized = _classify_update_type(entry.get("type", ""))
        if normalized is None:
            continue
        by_class[normalized].append(entry)

    def _pick(entries: list[dict]) -> Optional[dict]:
        """Pick the most recently-dated entry from the list.

        When CrossRef carries multiple entries for the same classification
        (commonly both a publisher and a retraction-watch entry for the
        same retraction), the later date is almost always the
        authoritative public-facing notice.  Publisher entries sometimes
        carry anomalous early dates (e.g. the Lancet hydroxychloroquine
        paper has a publisher retraction dated 2020-05-22 — before the
        paper's own publication — while retraction-watch correctly dates
        the actual retraction at 2020-06-05).  Latest-date wins is a
        simple, robust tiebreaker.
        """
        if not entries:
            return None
        if len(entries) == 1:
            return entries[0]

        def _sort_key(e: dict) -> str:
            date = _format_crossref_date(e.get("updated")) or ""
            # Empty dates sort last (epoch comparison would be risky)
            return date or "0000"

        return max(entries, key=_sort_key)

    def _normalize(entry: dict, *, include_type: Optional[str] = None) -> Optional[dict]:
        notice_doi = entry.get("DOI") or ""
        if not _DOI_SAFE_RE.match(notice_doi):
            notice_doi = ""
        source = (entry.get("source") or "").lower()
        if source not in ("publisher", "retraction-watch"):
            source = "unknown"
        date = _format_crossref_date(entry.get("updated"))
        # label is free-form CrossRef text; cap length and strip control chars
        label_raw = str(entry.get("label") or "")[:120]
        label = "".join(c if c.isprintable() else " " for c in label_raw).strip()
        out = {
            "notice_doi": notice_doi or None,
            "date": date,
            "source": source,
            "label": label or None,
        }
        if include_type:
            out["type"] = include_type
        # Require at least one identifying field; otherwise the entry is noise.
        if not (notice_doi or date or label):
            return None
        return out

    retraction = _pick(by_class["retraction"])
    if retraction:
        return _normalize(retraction), None
    eoc = _pick(by_class["expression_of_concern"])
    if eoc:
        return None, _normalize(eoc, include_type="expression_of_concern")
    correction = _pick(by_class["correction"])
    if correction:
        return None, _normalize(correction, include_type="correction")
    return None, None


def _extract_relations(relation_obj: Optional[dict]) -> dict:
    """Filter CrossRef ``message.relation`` to our tracked buckets.

    Returns ``{bucket: [doi, ...]}`` with only DOI-typed entries, each
    validated against ``_DOI_SAFE_RE``.
    """
    result: dict[str, list[str]] = {}
    if not isinstance(relation_obj, dict):
        return result
    for bucket in _RELATION_BUCKETS:
        entries = relation_obj.get(bucket) or []
        if not isinstance(entries, list):
            continue
        dois: list[str] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            if (e.get("id-type") or "").lower() != "doi":
                continue
            doi_val = (e.get("id") or "").strip().lower()
            if _DOI_SAFE_RE.match(doi_val):
                dois.append(doi_val)
        if dois:
            result[bucket.replace("-", "_")] = dois
    return result


def _extract_licenses(license_obj: Optional[list]) -> list[dict]:
    """Filter CrossRef ``message.license`` to url/content-version/start dicts."""
    if not isinstance(license_obj, list):
        return []
    out: list[dict] = []
    for entry in license_obj:
        if not isinstance(entry, dict):
            continue
        url = entry.get("URL") or ""
        if not url.startswith(("http://", "https://")):
            continue
        cv = (entry.get("content-version") or "").lower()
        if cv not in ("vor", "am", "tdm", "unspecified", ""):
            cv = "unspecified"
        rec: dict = {"url": url, "content_version": cv or "unspecified"}
        if start_date := _format_crossref_date(entry.get("start")):
            rec["start"] = start_date
        out.append(rec)
    return out


async def fetch_crossref_metadata(
    doi: str, *, timeout: float = 5.0,
) -> Optional[dict]:
    """Fetch retraction + adjacent enrichment from the CrossRef REST API.

    Calls ``https://api.crossref.org/works/{doi}``.  The singular-work
    endpoint does NOT support the ``select=`` parameter (that's list-only),
    so we fetch the full work record (~5-50 KB typically) and discard the
    fields we don't need in the normalizer.  Returns a normalized dict or
    None on any HTTP/network error (non-fatal enrichment).

    Normalized dict shape:
        - retraction: {notice_doi, date, source, label} | None
            (populated only when updated-by carries a retraction entry)
        - other_update: {type, notice_doi, date, source, label} | None
            (populated for EoC or correction when no retraction present)
        - relations: {is_preprint_of, has_preprint, is_version_of, has_version}
            (only non-empty buckets included)
        - licenses: [{url, content_version, start?}]
        - crossref_citation_count: int | None
        - crossref_type: str | None
    """
    await _crossref_limiter.wait()
    params: dict[str, str] = {}
    from .common import clean_env
    contact = clean_env("MCP_CONTACT_EMAIL")
    if contact:
        params["mailto"] = contact
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"https://api.crossref.org/works/{doi}",
                params=params or None,
                headers={"User-Agent": _API_USER_AGENT, "Accept": "application/json"},
            )
            if resp.status_code != 200:
                logger.debug(
                    "CrossRef REST HTTP %d for %s", resp.status_code, doi,
                )
                return None
            raw = resp.json()
    except Exception as e:
        logger.debug("CrossRef REST fetch failed for %s: %s", doi, e)
        return None

    if not isinstance(raw, dict):
        return None
    msg = raw.get("message")
    if not isinstance(msg, dict):
        return None

    retraction, other_update = _extract_update_notice(msg.get("updated-by") or [])
    relations = _extract_relations(msg.get("relation"))
    licenses = _extract_licenses(msg.get("license"))

    citation_count = msg.get("is-referenced-by-count")
    if not isinstance(citation_count, int):
        citation_count = None

    cr_type_raw = msg.get("type") or ""
    cr_type = cr_type_raw if isinstance(cr_type_raw, str) and cr_type_raw.isascii() else None

    return {
        "retraction": retraction,
        "other_update": other_update,
        "relations": relations,
        "licenses": licenses,
        "crossref_citation_count": citation_count,
        "crossref_type": cr_type,
    }


def _build_alert_message(
    retraction: Optional[dict], other_update: Optional[dict],
) -> Optional[str]:
    """Compose the ``alert:`` frontmatter value for a retraction or EoC.

    Returns None for corrections (which should use ``note:`` instead) or
    when neither flag is present.  Uses only structurally-validated
    fields (date, notice_doi, source) — no free-form ``label`` — since
    the value lands in the trusted frontmatter zone.
    """
    if retraction:
        verb = "retracted"
        entry = retraction
    elif other_update and other_update.get("type") == "expression_of_concern":
        verb = "expression of concern"
        entry = other_update
    else:
        return None
    bits = [verb]
    if date := entry.get("date"):
        bits.append(date)
    if notice_doi := entry.get("notice_doi"):
        bits.append(f"— notice: {notice_doi}")
    source = entry.get("source")
    if source and source != "unknown":
        bits.append(f"({source})")
    return " ".join(bits)


def _build_correction_note(other_update: Optional[dict]) -> Optional[str]:
    """Compose a ``note:`` frontmatter value for a correction notice."""
    if not other_update or other_update.get("type") != "correction":
        return None
    bits = ["correction published"]
    if date := other_update.get("date"):
        bits.append(date)
    if notice_doi := other_update.get("notice_doi"):
        bits.append(f"— notice: {notice_doi}")
    return " ".join(bits)


def _relations_fm_entry(relations: Optional[dict]) -> Optional[list[str]]:
    """Build a compact ``relation:`` frontmatter list from CrossRef relations.

    Returns None if no relations are present.  Each entry is a single
    YAML-safe string like ``"is_version_of: 10.x/..."``.
    """
    if not relations:
        return None
    lines: list[str] = []
    for bucket, dois in relations.items():
        for d in dois:
            lines.append(f"{bucket}: {d}")
    return lines or None


def _alt_dois_from_relations(relations: Optional[dict]) -> list[str]:
    """Extract version-linkage DOIs usable as shelf ``alt_dois``.

    Includes is/has-version and is/has-preprint buckets; excludes buckets
    like cites/references that point to unrelated papers.
    """
    if not relations:
        return []
    buckets = ("is_version_of", "has_version", "is_preprint_of", "has_preprint")
    out: list[str] = []
    for b in buckets:
        for d in relations.get(b) or []:
            if d not in out:
                out.append(d)
    return out


# ---------------------------------------------------------------------------
# Content negotiation
# ---------------------------------------------------------------------------

# Hosts that doi.org redirects to for content negotiation.
# DataCite DOIs → data.crosscite.org; CrossRef DOIs → api.crossref.org.
_DOI_REDIRECT_ALLOW = frozenset({
    "doi.org",
    "data.crosscite.org",
    "api.crossref.org",
})


async def _doi_content_negotiate(
    doi: str, accept: str, *, timeout: float = 5.0,
) -> Optional[httpx.Response]:
    """Send a rate-limited content negotiation request to doi.org.

    Follows redirects only to known metadata hosts (CrossRef, DataCite,
    CrossCite).  Rejects redirects to unknown hosts to prevent SSRF.

    Returns the Response on HTTP 200, or None on any failure.
    Never raises — designed for concurrent use with asyncio.gather.
    """
    from urllib.parse import urlparse

    await _doi_limiter.wait()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.get(
                f"https://doi.org/{doi}",
                headers={"User-Agent": _API_USER_AGENT, "Accept": accept},
            )
            # Follow redirect manually with host validation
            if resp.is_redirect:
                location = resp.headers.get("location", "")
                target_host = urlparse(location).hostname or ""
                if target_host not in _DOI_REDIRECT_ALLOW:
                    logger.debug(
                        "DOI redirect to untrusted host %s for %s", target_host, doi,
                    )
                    return None
                resp = await client.get(
                    location,
                    headers={"User-Agent": _API_USER_AGENT, "Accept": accept},
                )
            if resp.status_code == 200:
                return resp
            logger.debug("DOI content negotiation HTTP %d for %s (%s)", resp.status_code, doi, accept)
            return None
    except Exception as e:
        logger.debug("DOI content negotiation failed for %s: %s", doi, e)
        return None


async def fetch_formatted_citation(
    doi: str, *, style: str = "apa", timeout: float = 5.0,
) -> Optional[str]:
    """Fetch a pre-formatted citation string via DOI content negotiation.

    Returns the citation string, or None on any failure.
    """
    resp = await _doi_content_negotiate(doi, f"text/x-bibliography; style={style}", timeout=timeout)
    if resp is None:
        return None
    text = resp.text.strip()
    return text if text else None


async def fetch_csl_json(
    doi: str, *, timeout: float = 5.0,
) -> Optional[dict]:
    """Fetch structured CSL-JSON metadata via DOI content negotiation.

    Returns parsed JSON dict, or None on any failure.
    """
    resp = await _doi_content_negotiate(doi, "application/vnd.citationstyles.csl+json", timeout=timeout)
    return resp.json() if resp is not None else None


# ---------------------------------------------------------------------------
# CSL-JSON → markdown formatting
# ---------------------------------------------------------------------------

def _format_csl_author(author: dict) -> str:
    """Format a single CSL-JSON author entry."""
    if literal := author.get("literal"):
        return literal
    family = author.get("family", "")
    given = author.get("given", "")
    if family and given:
        return f"{family}, {given}"
    return family or given or "Unknown"


def _format_csl_date(issued: dict) -> Optional[str]:
    """Format a CSL-JSON date-parts or literal date."""
    if literal := issued.get("literal"):
        return literal
    parts = issued.get("date-parts")
    if parts and parts[0]:
        dp = parts[0]
        if len(dp) >= 3:
            return f"{dp[0]}-{dp[1]:02d}-{dp[2]:02d}"
        elif len(dp) >= 2:
            return f"{dp[0]}-{dp[1]:02d}"
        else:
            return str(dp[0])
    return None


def _format_csl_json_as_markdown(data: dict, *, datacite: Optional[dict] = None) -> str:
    """Format CSL-JSON metadata into readable markdown.

    When datacite enrichment dict is provided, merges ORCIDs into author
    display and adds SPDX license info.
    """
    parts = []
    dc_orcids = (datacite or {}).get("orcids") or {}

    title = data.get("title", "Untitled")
    parts.append(f"# {title}\n")

    # Authors (with ORCIDs from DataCite when available)
    authors = data.get("author") or []
    if authors:
        author_strs = []
        for a in authors[:10]:
            name = _format_csl_author(a)
            # Match ORCID by "Last, First" name
            orcid = dc_orcids.get(name)
            if not orcid:
                # DataCite uses "Last, First" format — also try CSL family name
                family = a.get("family", "")
                given = a.get("given", "")
                if family and given:
                    orcid = dc_orcids.get(f"{family}, {given}")
            if orcid:
                name += f" [ORCID](https://orcid.org/{orcid})"
            author_strs.append(name)
        if len(authors) > 10:
            author_strs.append(f"... and {len(authors) - 10} more")
        parts.append(f"**Authors:** {', '.join(author_strs)}\n")

    # Date, publisher, type
    meta_bits = []
    if issued := data.get("issued"):
        if date_str := _format_csl_date(issued):
            meta_bits.append(f"**Published:** {date_str}")
    if publisher := data.get("publisher"):
        meta_bits.append(f"**Publisher:** {publisher}")
    if container := data.get("container-title"):
        meta_bits.append(f"**Journal:** {container}")
    csl_type = data.get("type")
    # Prefer DataCite's resource type (more specific than CSL-JSON mapping)
    dc_type = (datacite or {}).get("resource_type")
    display_type = dc_type or csl_type
    if display_type:
        meta_bits.append(f"**Type:** {display_type}")
    if meta_bits:
        parts.append("  \n".join(meta_bits) + "\n")

    # DOI link
    if doi := data.get("DOI"):
        parts.append(f"**DOI:** [{doi}](https://doi.org/{doi})\n")

    # License — prefer SPDX from DataCite, fall back to CSL-JSON copyright
    dc_license_id = (datacite or {}).get("license_id")
    dc_license_url = (datacite or {}).get("license_url")
    if dc_license_id and dc_license_url:
        parts.append(f"**License:** [{dc_license_id}]({dc_license_url})\n")
    elif dc_license_id:
        parts.append(f"**License:** {dc_license_id}\n")
    elif copyright_text := data.get("copyright"):
        parts.append(f"**License:** {copyright_text}\n")

    # Abstract
    if abstract := data.get("abstract"):
        clean = re.sub(r'<[^>]+>', '', abstract).strip()
        if clean:
            parts.append(f"## Abstract\n\n{clean}\n")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# DOI fast-path handler
# ---------------------------------------------------------------------------

async def _fetch_doi_paper(doi: str) -> str:
    """Fetch DOI metadata via content negotiation and return formatted markdown.

    For arXiv DOIs (10.48550/arXiv.*), delegates to the arXiv handler.
    For all other DOIs, fetches CSL-JSON and APA citation concurrently.
    """
    # Delegate arXiv DOIs to the arXiv handler
    arxiv_match = ARXIV_DOI_RE.match(doi)
    if arxiv_match:
        from .arxiv import _fetch_arxiv_paper
        arxiv_id = arxiv_match.group(1)
        return await _fetch_arxiv_paper(arxiv_id)

    # Delegate RFC DOIs to the IETF handler
    rfc_match = re.match(r'^10\.17487/RFC(\d+)$', doi, re.IGNORECASE)
    if rfc_match:
        from .ietf import _fetch_rfc_paper
        return await _fetch_rfc_paper(int(rfc_match.group(1)))

    # Concurrent: CSL-JSON metadata + formatted APA citation + RA detection
    #            + CrossRef REST enrichment (retraction / relations / license)
    csl_result, cite_result, ra_result, crossref_result = await asyncio.gather(
        fetch_csl_json(doi),
        fetch_formatted_citation(doi),
        _detect_ra(doi),
        fetch_crossref_metadata(doi),
        return_exceptions=True,
    )
    csl_data = csl_result if isinstance(csl_result, dict) else None
    citation_text = cite_result if isinstance(cite_result, str) else None
    ra = ra_result if isinstance(ra_result, str) else None
    crossref_meta = crossref_result if isinstance(crossref_result, dict) else None

    if not csl_data and not citation_text:
        return f"Error: Could not resolve DOI: {doi}. No metadata returned from doi.org."

    # DataCite enrichment (only for DataCite DOIs)
    datacite = None
    if ra == "DataCite":
        datacite = await fetch_datacite_metadata(doi)

    # Format body from CSL-JSON, or minimal fallback
    if csl_data:
        body = _format_csl_json_as_markdown(csl_data, datacite=datacite)
        title = csl_data.get("title", "Untitled")
    else:
        title = "Untitled"
        body = f"# {doi}\n"

    # Prepend retraction/EoC/correction banner (inside the fence since it
    # includes the free-form `label` field from CrossRef).
    retraction = (crossref_meta or {}).get("retraction")
    other_update = (crossref_meta or {}).get("other_update")
    if banner := _format_retraction_banner(retraction, other_update):
        body = banner + "\n\n" + body

    if citation_text:
        body += f"\n## Citation\n\n{citation_text}\n"

    # Passive shelf tracking
    from .shelf import _track_on_shelf, CitationRecord
    authors = [_format_csl_author(a) for a in (csl_data or {}).get("author", [])]
    year = None
    if issued := (csl_data or {}).get("issued"):
        parts = issued.get("date-parts")
        if parts and parts[0]:
            year = parts[0][0]
    # Populate alt_dois with version-linked DOIs from CrossRef relations so
    # shelf dedup can match preprint↔journal pairs even on first inspection.
    relations = (crossref_meta or {}).get("relations") or {}
    shelf_result = await _track_on_shelf(CitationRecord(
        doi=doi,
        title=title,
        authors=authors,
        year=year,
        venue=(csl_data or {}).get("container-title"),
        alt_dois=_alt_dois_from_relations(relations),
        source_tool="doi",
        orcids=datacite.get("orcids") if datacite else None,
        retraction=retraction,
    ))

    fm_entries: dict = {
        "title": title,
        "source": f"https://doi.org/{doi}",
        "api": "DOI",
        "trust": _TRUST_ADVISORY,
        "alert": _build_alert_message(retraction, other_update),
        "note": shelf_result.shelf_note or _build_correction_note(other_update),
        "relation": _relations_fm_entry(relations),
        "see_also": (
            f"DOI:{doi} with {tool_name('semantic_scholar')} for citation counts and references"
            if s2_enabled() else None
        ),
        "shelf": shelf_result.status_line,
    }
    fm = _build_frontmatter(fm_entries)

    fenced = _fence_content(body, title=None)  # title already in body as H1
    return fm + "\n\n" + fenced
