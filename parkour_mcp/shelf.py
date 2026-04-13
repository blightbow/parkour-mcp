"""Research shelf — in-memory session-scoped document tracker.

Passively populated by arXiv, Semantic Scholar, and DOI handlers when a
single paper is inspected.  Provides BibTeX/RIS export, JSON import/export
for agent memory persistence, and an MCP tool for interactive management.

The shelf lives in the MCP server process memory for the session lifetime.
Cross-session persistence is agent-managed via export json / import.

Concurrency: the shelf is shared across all agents in an MCP session
(subagents reuse the parent's MCP connections by default).  All public
methods that touch _records are serialized by an asyncio.Lock to prevent
race conditions from concurrent tool calls (e.g. two subagents tracking
the same paper via different DOIs simultaneously).
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field, fields, asdict
from typing import Annotated, Optional

from pydantic import Field as PydanticField

from .common import tool_name

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Citation record
# ---------------------------------------------------------------------------

@dataclass
class CitationRecord:
    """A single tracked paper on the research shelf."""

    doi: str                                    # primary key (prefer journal DOI over preprint)
    title: str
    authors: list[str] = field(default_factory=list)   # ["Last, First", ...]
    year: Optional[int] = None
    venue: Optional[str] = None
    alt_dois: list[str] = field(default_factory=list)  # alternate DOIs (preprint ↔ journal)
    source_tool: Optional[str] = None           # "arxiv", "semantic_scholar", "doi"
    bibtex: Optional[str] = None
    orcids: Optional[dict[str, str]] = None     # {"Author Name": "0000-..."}
    added: Optional[str] = None                 # ISO 8601 timestamp
    score: Optional[int] = None                 # LLM-assigned
    confirmed: bool = False                     # LLM-managed
    notes: Optional[str] = None                 # LLM-managed freetext
    # Populated when CrossRef reports an updated-by entry of type=retraction
    # (see parkour_mcp.doi.fetch_crossref_metadata).  Presence of this
    # field classifies the record as retracted and routes it to the separate
    # retracted bucket on the shelf — never mixed with citable entries.
    retraction: Optional[dict] = None           # {notice_doi, date, source, label}


@dataclass
class ShelfTrackResult:
    """Result of a track operation, for frontmatter assembly.

    status_line is a compact summary (e.g. "3 tracked (1 retracted)")
    suitable for the ``shelf:`` frontmatter field.  shelf_note explains a
    notable shelving event (retraction routing, moved between buckets) and
    is suitable for a ``note:`` frontmatter field; it is None for routine
    tracking.
    """

    status_line: Optional[str] = None
    shelf_note: Optional[str] = None


def _doi_priority(doi: str) -> int:
    """Return a priority score for DOI type (higher = more authoritative).

    Journal/publisher DOIs are preferred over preprint server DOIs,
    which are preferred over synthesized arXiv DOIs.
    """
    if doi.startswith("10.48550/arXiv."):
        return 0  # synthesized arXiv DOI — lowest
    if doi.startswith("10.1101/"):
        return 1  # bioRxiv/medRxiv — preprint server
    return 2      # journal/publisher — highest


# ---------------------------------------------------------------------------
# BibTeX / RIS formatting
# ---------------------------------------------------------------------------

def _sanitize_bibtex_key(record: CitationRecord) -> str:
    """Generate a BibTeX entry key from first author + year."""
    if record.authors:
        # Extract last name from "Last, First" or just use full name
        first_author = record.authors[0]
        last_name = first_author.split(",")[0].strip() if "," in first_author else first_author.split()[-1]
        last_name = re.sub(r'[^a-zA-Z]', '', last_name).lower()
    else:
        last_name = "unknown"
    year = str(record.year) if record.year else "nd"
    return f"{last_name}{year}"


def _escape_bibtex(text: str) -> str:
    """Escape special LaTeX characters in BibTeX field values."""
    for char, escaped in [("&", r"\&"), ("%", r"\%"), ("#", r"\#"), ("_", r"\_")]:
        text = text.replace(char, escaped)
    return text


def record_to_bibtex(record: CitationRecord) -> str:
    """Format a CitationRecord as a BibTeX entry."""
    # Use pre-existing BibTeX from S2 if available
    if record.bibtex:
        return record.bibtex.strip()

    key = _sanitize_bibtex_key(record)
    fields = []
    if record.authors:
        authors_str = " and ".join(record.authors)
        fields.append(f"  author = {{{_escape_bibtex(authors_str)}}}")
    fields.append(f"  title = {{{_escape_bibtex(record.title)}}}")
    if record.year:
        fields.append(f"  year = {{{record.year}}}")
    if record.venue:
        fields.append(f"  journal = {{{_escape_bibtex(record.venue)}}}")
    if record.doi:
        fields.append(f"  doi = {{{record.doi}}}")

    # arXiv eprint fields per convention (archivePrefix + eprint ID)
    arxiv_id = _extract_arxiv_id(record)
    if arxiv_id:
        fields.append(f"  eprint = {{{arxiv_id}}}")
        fields.append("  archivePrefix = {arXiv}")

    fields_str = ",\n".join(fields)
    return f"@misc{{{key},\n{fields_str}\n}}"


def _extract_arxiv_id(record: CitationRecord) -> Optional[str]:
    """Extract arXiv paper ID from a record's DOI or alt_dois."""
    _PREFIX = "10.48550/arXiv."
    if record.doi.startswith(_PREFIX):
        return record.doi[len(_PREFIX):]
    for alt in record.alt_dois:
        if alt.startswith(_PREFIX):
            return alt[len(_PREFIX):]
    return None


def record_to_ris(record: CitationRecord) -> str:
    """Format a CitationRecord as an RIS entry."""
    lines = ["TY  - GEN"]
    for author in record.authors:
        lines.append(f"AU  - {author}")
    lines.append(f"TI  - {record.title}")
    if record.venue:
        lines.append(f"JO  - {record.venue}")
    if record.year:
        lines.append(f"PY  - {record.year}")
    if record.doi:
        lines.append(f"DO  - {record.doi}")
    lines.append("ER  - ")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Shelf storage (in-memory, session-scoped)
# ---------------------------------------------------------------------------
# The shelf lives in MCP server process memory for the session lifetime.
# Cross-session persistence is agent-managed via export json / import:
# agents write exports to their memory files and import on future sessions.
# This avoids cross-project contamination from a shared file path.


class ResearchShelf:
    """In-memory research document tracker for the current session.

    All public methods are async and serialized by an internal lock to
    prevent race conditions when multiple agents share the same MCP server.
    """

    def __init__(self):
        self._records: dict[str, CitationRecord] = {}
        self._retracted: dict[str, CitationRecord] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _find_in_store(
        record: CitationRecord, store: dict[str, CitationRecord],
    ) -> Optional[str]:
        """Find an existing record in ``store`` sharing a DOI with ``record``.

        Checks:
        1. New record's DOI against store keys
        2. New record's alt_dois against store keys
        3. New record's primary DOI / alt_dois against existing records' alt_dois
        Returns the matching primary key, or None.
        """
        if record.doi in store:
            return record.doi
        for alt in record.alt_dois:
            if alt in store:
                return alt
        new_dois = {record.doi} | set(record.alt_dois)
        for key, existing in store.items():
            if new_dois.intersection(existing.alt_dois):
                return key
        return None

    def _merge_into_existing(
        self, record: CitationRecord, existing: CitationRecord,
    ) -> None:
        """Merge existing preserved fields into ``record`` (mutates ``record``).

        Preserves user-managed fields (score/confirmed/notes) and added
        timestamp, then promotes to the highest-priority DOI and sorts
        alt_dois.  Also propagates retraction status sticky-positive:
        once a record is flagged retracted, re-inspecting a non-retracted
        sibling DOI does NOT clear the flag.
        """
        record.score = existing.score
        record.confirmed = existing.confirmed
        record.notes = existing.notes
        record.added = existing.added
        if existing.retraction and not record.retraction:
            record.retraction = existing.retraction
        all_dois = set(existing.alt_dois) | set(record.alt_dois)
        all_dois.add(existing.doi)
        all_dois.add(record.doi)
        record.doi = max(all_dois, key=_doi_priority)
        record.alt_dois = sorted(d for d in all_dois if d != record.doi)

    def _track_unlocked(self, record: CitationRecord) -> ShelfTrackResult:
        """Core upsert logic — caller must hold self._lock.

        Returns a ShelfTrackResult describing the action taken.  Retracted
        papers are routed to a separate bucket and never mixed with the
        citable set.  Notable events (first-seeing a retracted paper,
        moving an active entry to the retracted bucket, a version-linked
        retraction pulling in its sibling) emit a shelf_note.
        """
        # Normalize input: if the incoming record is retracted, route to
        # the retracted store.  Otherwise check if it (or any version-
        # linked alt DOI) is already known to be retracted — sticky flag.
        is_retracted_input = record.retraction is not None

        active_match = self._find_in_store(record, self._records)
        retracted_match = self._find_in_store(record, self._retracted)

        shelf_note: Optional[str] = None

        if is_retracted_input:
            # Case A: flagged retracted.  Route to retracted store.
            if retracted_match:
                # Already in retracted bucket — merge, no-op note.
                existing = self._retracted[retracted_match]
                self._merge_into_existing(record, existing)
                if retracted_match != record.doi and retracted_match in self._retracted:
                    del self._retracted[retracted_match]
                self._retracted[record.doi] = record
            elif active_match:
                # Moving an active entry to retracted.  Preserve user fields.
                existing = self._records[active_match]
                self._merge_into_existing(record, existing)
                del self._records[active_match]
                self._retracted[record.doi] = record
                shelf_note = (
                    "existing shelf entry moved to retracted bucket "
                    "(not citable)"
                )
            else:
                # First time seen; insert into retracted store.
                record.added = record.added or time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(),
                )
                self._retracted[record.doi] = record
                shelf_note = (
                    "tracked in retracted shelf bucket "
                    "(not added to active citations)"
                )
        else:
            # Case B: not flagged retracted by this fetch.  If any known
            # DOI links to the retracted store, treat as retracted
            # (sticky — retraction status is never unset by re-inspection).
            if retracted_match:
                existing = self._retracted[retracted_match]
                self._merge_into_existing(record, existing)
                if retracted_match != record.doi and retracted_match in self._retracted:
                    del self._retracted[retracted_match]
                self._retracted[record.doi] = record
                # No note — the retraction was previously surfaced; this
                # inspection is just adding alt DOIs to an existing entry.
            elif active_match:
                existing = self._records[active_match]
                self._merge_into_existing(record, existing)
                if active_match != record.doi and active_match in self._records:
                    del self._records[active_match]
                self._records[record.doi] = record
            else:
                record.added = record.added or time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(),
                )
                self._records[record.doi] = record

        return ShelfTrackResult(
            status_line=self._status_line_unlocked(),
            shelf_note=shelf_note,
        )

    async def track(self, record: CitationRecord) -> ShelfTrackResult:
        """Upsert a record — updates metadata, preserves score/confirmed/notes.

        Deduplicates across preprint/journal DOIs: when the same paper is
        tracked via different DOIs (e.g. arXiv + bioRxiv, or preprint +
        journal), merges into a single entry keyed on the journal DOI.

        Retracted records are routed to a separate bucket and never mixed
        with the citable set.  Returns a ShelfTrackResult describing the
        action taken (compact status line + optional explanatory note).
        """
        async with self._lock:
            return self._track_unlocked(record)

    def _resolve_doi(self, doi: str) -> tuple[Optional[str], Optional[str]]:
        """Resolve a DOI to its primary key plus bucket ("active"/"retracted").

        Checks alt_dois as fallback.  Returns (None, None) if the DOI is
        not on either bucket.
        """
        if doi in self._records:
            return doi, "active"
        if doi in self._retracted:
            return doi, "retracted"
        for key, rec in self._records.items():
            if doi in rec.alt_dois:
                return key, "active"
        for key, rec in self._retracted.items():
            if doi in rec.alt_dois:
                return key, "retracted"
        return None, None

    def _get_record(self, key: str, bucket: str) -> CitationRecord:
        """Return the record from the specified bucket. Caller guarantees key exists."""
        return self._retracted[key] if bucket == "retracted" else self._records[key]

    async def remove(self, dois: list[str]) -> list[str]:
        """Batch remove by DOI (resolves alt_dois across both buckets).

        Returns list of actually removed DOIs.
        """
        async with self._lock:
            removed = []
            for doi in dois:
                key, bucket = self._resolve_doi(doi)
                if key and bucket:
                    store = self._retracted if bucket == "retracted" else self._records
                    del store[key]
                    removed.append(doi)
            return removed

    async def set_score(self, doi: str, value: int) -> bool:
        """Set score for a paper. Returns False if DOI not found."""
        async with self._lock:
            key, bucket = self._resolve_doi(doi)
            if not key or not bucket:
                return False
            self._get_record(key, bucket).score = value
            return True

    async def confirm(self, doi: str) -> bool:
        """Mark a paper as confirmed. Returns False if DOI not found."""
        async with self._lock:
            key, bucket = self._resolve_doi(doi)
            if not key or not bucket:
                return False
            self._get_record(key, bucket).confirmed = True
            return True

    async def set_note(self, doi: str, text: str) -> bool:
        """Set freetext note. Returns False if DOI not found."""
        async with self._lock:
            key, bucket = self._resolve_doi(doi)
            if not key or not bucket:
                return False
            self._get_record(key, bucket).notes = text
            return True

    async def list_all(
        self, section: str = "active",
    ) -> list[CitationRecord]:
        """Return records sorted by added timestamp.

        ``section`` may be "active" (default, citable only), "retracted",
        or "all" (concatenated: active then retracted).
        """
        async with self._lock:
            active = sorted(self._records.values(), key=lambda r: r.added or "")
            retracted = sorted(self._retracted.values(), key=lambda r: r.added or "")
            if section == "retracted":
                return retracted
            if section == "all":
                return active + retracted
            return active

    async def counts(self) -> tuple[int, int]:
        """Return (active_count, retracted_count)."""
        async with self._lock:
            return len(self._records), len(self._retracted)

    async def export_bibtex(self, include_retracted: bool = False) -> str:
        """Export records as a BibTeX file.

        Active entries only by default; set ``include_retracted=True`` to
        append retracted entries with a loud ``note`` field identifying
        the retraction notice.
        """
        async with self._lock:
            entries = [record_to_bibtex(r) for r in sorted(
                self._records.values(), key=lambda r: r.added or "",
            )]
            if include_retracted:
                for r in sorted(self._retracted.values(), key=lambda r: r.added or ""):
                    entries.append(_retracted_bibtex(r))
            return "\n\n".join(entries)

    async def export_ris(self, include_retracted: bool = False) -> str:
        """Export records as an RIS file.

        Active entries only by default; set ``include_retracted=True`` to
        append retracted entries with an N1 note identifying the retraction.
        """
        async with self._lock:
            entries = [record_to_ris(r) for r in sorted(
                self._records.values(), key=lambda r: r.added or "",
            )]
            if include_retracted:
                for r in sorted(self._retracted.values(), key=lambda r: r.added or ""):
                    entries.append(_retracted_ris(r))
            return "\n\n".join(entries)

    async def export_json(self) -> str:
        """Export full shelf as JSON string for agent memory persistence.

        Includes both buckets under separate top-level keys for full
        fidelity (programmatic consumers see the retraction field).
        """
        async with self._lock:
            payload = {
                "active": {doi: asdict(rec) for doi, rec in self._records.items()},
                "retracted": {doi: asdict(rec) for doi, rec in self._retracted.items()},
            }
            return json.dumps(payload, indent=2, ensure_ascii=False)

    async def import_json(self, data: str) -> tuple[int, int]:
        """Import shelf from JSON string. Returns (new_count, updated_count).

        Accepts both the new ``{"active": {...}, "retracted": {...}}``
        format and the legacy flat ``{doi: record}`` format for backward
        compatibility with older exports.

        Unknown keys in the import payload are silently dropped so that
        exports written by future versions (with new fields) or older
        versions (with fields since removed) round-trip cleanly.
        """
        # Known CitationRecord field names, computed once per call.
        known_fields = {f.name for f in fields(CitationRecord)}

        def _build_record(rec_dict: dict) -> CitationRecord:
            filtered = {k: v for k, v in rec_dict.items() if k in known_fields}
            return CitationRecord(**filtered)

        async with self._lock:
            parsed = json.loads(data)
            new_count = 0
            updated_count = 0

            # Detect format: new structured export has "active"/"retracted" keys
            if (
                isinstance(parsed, dict)
                and "active" in parsed
                and "retracted" in parsed
            ):
                for bucket_name in ("active", "retracted"):
                    for doi, rec_dict in (parsed.get(bucket_name) or {}).items():
                        is_new = doi not in self._records and doi not in self._retracted
                        self._track_unlocked(_build_record(rec_dict))
                        if is_new:
                            new_count += 1
                        else:
                            updated_count += 1
            else:
                # Legacy flat format
                for doi, rec_dict in parsed.items():
                    is_new = doi not in self._records and doi not in self._retracted
                    self._track_unlocked(_build_record(rec_dict))
                    if is_new:
                        new_count += 1
                    else:
                        updated_count += 1
            return new_count, updated_count

    async def clear(self) -> int:
        """Remove all entries from both buckets. Returns count removed."""
        async with self._lock:
            count = len(self._records) + len(self._retracted)
            self._records.clear()
            self._retracted.clear()
            return count

    def _status_line_unlocked(self) -> Optional[str]:
        """Internal status-line computation; caller must hold the lock."""
        active = len(self._records)
        retracted = len(self._retracted)
        if active == 0 and retracted == 0:
            return None
        confirmed = sum(1 for r in self._records.values() if r.confirmed)
        bits = [f"{active} tracked"]
        bits.append(f"{confirmed} confirmed" if confirmed else "0 confirmed")
        if retracted:
            bits.append(f"{retracted} retracted")
        return f"{bits[0]} ({', '.join(bits[1:])}) — use {tool_name('research_shelf')} to review"

    async def status_line(self) -> Optional[str]:
        """Compact status for frontmatter. Returns None if shelf is empty."""
        async with self._lock:
            return self._status_line_unlocked()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_shelf: Optional[ResearchShelf] = None


def _get_shelf() -> ResearchShelf:
    """Return the global in-memory shelf instance."""
    global _shelf
    if _shelf is None:
        _shelf = ResearchShelf()
    return _shelf


def _reset_shelf() -> None:
    """Reset the global shelf instance (for testing)."""
    global _shelf
    _shelf = None


async def _track_on_shelf(record: CitationRecord) -> ShelfTrackResult:
    """Track a record on the shelf and return the track result.

    Fire-and-forget helper for handlers — catches all exceptions and
    returns an empty ShelfTrackResult on failure so tracking never blocks
    tool output.
    """
    try:
        shelf = _get_shelf()
        return await shelf.track(record)
    except Exception:
        logger.debug("Shelf tracking failed for %s", record.doi, exc_info=True)
        return ShelfTrackResult()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_shelf_list(
    records: list[CitationRecord],
    *,
    bucket: str = "active",
    other_bucket_count: int = 0,
) -> str:
    """Format shelf contents as a compact markdown table.

    ``bucket`` controls the rendered columns: "active" uses the standard
    citation table; "retracted" adds retraction metadata columns.  When
    ``other_bucket_count`` is non-zero and the primary view is "active",
    a footer line points to the retracted listing.
    """
    if not records:
        if bucket == "retracted":
            return "No retracted entries on shelf."
        empty_msg = "Research shelf is empty."
        if other_bucket_count:
            empty_msg += (
                f"\n\n_({other_bucket_count} retracted entries hidden "
                "— list with section=\"retracted\" to view)_"
            )
        return empty_msg

    if bucket == "retracted":
        lines = [
            "| # | Title | DOI | Retracted | Notice | Source |",
            "|---|-------|-----|-----------|--------|--------|",
        ]
        for i, r in enumerate(records, 1):
            title = r.title[:50] + "..." if len(r.title) > 50 else r.title
            ret = r.retraction or {}
            date = ret.get("date") or "—"
            notice = ret.get("notice_doi") or "—"
            src = ret.get("source") or "—"
            lines.append(f"| {i} | {title} | {r.doi} | {date} | {notice} | {src} |")
        return "\n".join(lines)

    lines = [
        "| # | Score | Status | Title | DOI | Source |",
        "|---|-------|--------|-------|-----|--------|",
    ]
    for i, r in enumerate(records, 1):
        score = str(r.score) if r.score is not None else "—"
        status = "confirmed" if r.confirmed else ""
        title = r.title[:50] + "..." if len(r.title) > 50 else r.title
        source = r.source_tool or "—"
        lines.append(f"| {i} | {score} | {status} | {title} | {r.doi} | {source} |")
    if other_bucket_count:
        lines.append("")
        lines.append(
            f"_({other_bucket_count} retracted entries hidden "
            "— list with section=\"retracted\" to view)_"
        )
    return "\n".join(lines)


def _retraction_note_string(record: CitationRecord) -> str:
    """Build the human-readable retraction descriptor for exports."""
    ret = record.retraction or {}
    parts = ["RETRACTED"]
    if notice_doi := ret.get("notice_doi"):
        parts.append(f"by {notice_doi}")
    if date := ret.get("date"):
        parts.append(f"on {date}")
    if source := ret.get("source"):
        if source != "unknown":
            parts.append(f"(source: {source})")
    return " ".join(parts)


def _retracted_bibtex(record: CitationRecord) -> str:
    """Render a retracted record as a BibTeX entry with a prominent note field.

    Emits a minimal stub pointing to ``record_to_bibtex`` output then
    appends a ``note`` field.  The note is visible in any downstream
    BibTeX renderer, surviving export→import round trips.
    """
    base = record_to_bibtex(record)
    note = _retraction_note_string(record)
    # Insert the note field before the closing brace (last line is "}")
    if base.rstrip().endswith("}"):
        trimmed = base.rstrip()[:-1].rstrip()
        # Ensure previous field ends with a comma
        if not trimmed.rstrip().endswith(","):
            trimmed += ","
        return f"{trimmed}\n  note = {{{note}}}\n}}"
    return base + f"\n% {note}"


def _retracted_ris(record: CitationRecord) -> str:
    """Render a retracted record as an RIS entry with a prominent N1 note."""
    base = record_to_ris(record)
    note = _retraction_note_string(record)
    # N1 = Notes; insert before ER
    lines = base.split("\n")
    if lines and lines[-1].startswith("ER"):
        lines.insert(-1, f"N1  - {note}")
        return "\n".join(lines)
    return base + f"\nN1  - {note}"


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------

async def research_shelf(
    action: Annotated[str, PydanticField(
        description=(
            "The operation to perform. "
            "list: show all tracked papers. "
            "confirm: mark a paper as confirmed/useful. "
            "remove: batch remove papers by DOI (comma-separated). "
            "score: set an integer score for a paper. "
            "note: set a freetext note on a paper. "
            "export: export shelf in bibtex, ris, or json format. "
            "import: import shelf from a JSON export string. "
            "clear: remove all entries from the shelf."
        ),
    )],
    query: Annotated[str, PydanticField(
        description=(
            "For confirm/score/note: the DOI of the paper. "
            "For remove: comma-separated DOIs to remove. "
            "For score: DOI followed by space and integer value (e.g. '10.1234/foo 8'). "
            "For note: DOI followed by space and note text. "
            "For list: section name (active, retracted, all) — default active. "
            "For export: format name (bibtex, ris, json), optionally "
            "followed by 'with_retracted' to include retracted entries "
            "(e.g. 'bibtex with_retracted'). "
            "For import: the JSON string to import. "
            "For clear: ignored (pass any value)."
        ),
    )] = "",
) -> str:
    """Manage the research shelf — a persistent tracker for inspected papers."""
    shelf = _get_shelf()

    if action == "list":
        section = query.strip().lower() or "active"
        if section not in ("active", "retracted", "all"):
            return (
                f"Error: Unknown section '{section}'. "
                "Use active, retracted, or all."
            )
        records = await shelf.list_all(section=section)
        if section == "active":
            _, retracted_count = await shelf.counts()
            return _format_shelf_list(
                records, bucket="active", other_bucket_count=retracted_count,
            )
        if section == "retracted":
            return _format_shelf_list(records, bucket="retracted")
        # section == "all": always render both sections so the output
        # reflects the full shelf state regardless of which buckets are
        # populated.  Skipping an empty section would misrepresent intent.
        active_count, _ = await shelf.counts()
        active_records = records[:active_count]
        retracted_records = records[active_count:]
        parts = [
            "## Active\n",
            _format_shelf_list(active_records, bucket="active"),
            "",
            "## Retracted\n",
            _format_shelf_list(retracted_records, bucket="retracted"),
        ]
        return "\n".join(parts)

    elif action == "confirm":
        doi = query.strip()
        if not doi:
            return "Error: DOI is required for confirm action."
        if await shelf.confirm(doi):
            return f"Confirmed: {doi}"
        return f"Error: DOI not found on shelf: {doi}"

    elif action == "remove":
        dois = [d.strip() for d in query.split(",") if d.strip()]
        if not dois:
            return "Error: At least one DOI is required for remove action."
        removed = await shelf.remove(dois)
        if removed:
            return f"Removed {len(removed)} paper(s): {', '.join(removed)}"
        return "No matching DOIs found on shelf."

    elif action == "score":
        parts = query.strip().split(None, 1)
        if len(parts) != 2:
            return "Error: score action requires 'DOI VALUE' (e.g. '10.1234/foo 8')."
        doi, value_str = parts
        try:
            value = int(value_str)
        except ValueError:
            return f"Error: Score must be an integer, got '{value_str}'."
        if await shelf.set_score(doi, value):
            return f"Score set to {value} for {doi}"
        return f"Error: DOI not found on shelf: {doi}"

    elif action == "note":
        parts = query.strip().split(None, 1)
        if len(parts) < 2:
            return "Error: note action requires 'DOI TEXT'."
        doi, text = parts
        if await shelf.set_note(doi, text):
            return f"Note set for {doi}"
        return f"Error: DOI not found on shelf: {doi}"

    elif action == "export":
        tokens = query.strip().lower().split()
        if not tokens:
            return "Error: export action requires a format (bibtex, ris, or json)."
        fmt = tokens[0]
        include_retracted = "with_retracted" in tokens[1:]
        if fmt == "bibtex":
            result = await shelf.export_bibtex(include_retracted=include_retracted)
            return result if result else "Shelf is empty."
        elif fmt == "ris":
            result = await shelf.export_ris(include_retracted=include_retracted)
            return result if result else "Shelf is empty."
        elif fmt == "json":
            return await shelf.export_json()
        else:
            return f"Error: Unknown export format '{fmt}'. Use bibtex, ris, or json."

    elif action == "import":
        if not query.strip():
            return "Error: JSON data is required for import action."
        try:
            new, updated = await shelf.import_json(query)
            parts = []
            if new:
                parts.append(f"{new} new")
            if updated:
                parts.append(f"{updated} updated")
            return f"Imported: {', '.join(parts)}." if parts else "No records in import data."
        except (json.JSONDecodeError, TypeError) as e:
            return f"Error: Invalid JSON — {e}"

    elif action == "clear":
        count = await shelf.clear()
        return f"Cleared {count} record(s) from shelf."

    else:
        return (
            f"Error: Unknown action '{action}'. "
            "Valid actions: list, confirm, remove, score, note, export, import, clear"
        )
