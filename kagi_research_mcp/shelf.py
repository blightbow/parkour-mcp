"""Research shelf — persistent document tracker for inspected papers.

Passively populated by arXiv, Semantic Scholar, and DOI handlers when a
single paper is inspected.  Provides BibTeX/RIS export, JSON import/export
for agent memory persistence, and an MCP tool for interactive management.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Annotated, Optional

from pydantic import Field as PydanticField

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Citation record
# ---------------------------------------------------------------------------

@dataclass
class CitationRecord:
    """A single tracked paper on the research shelf."""

    doi: str                                    # primary key
    title: str
    authors: list[str] = field(default_factory=list)   # ["Last, First", ...]
    year: Optional[int] = None
    venue: Optional[str] = None
    source_tool: Optional[str] = None           # "arxiv", "semantic_scholar", "doi"
    bibtex: Optional[str] = None
    citation_apa: Optional[str] = None
    orcids: Optional[dict[str, str]] = None     # {"Author Name": "0000-..."}
    added: Optional[str] = None                 # ISO 8601 timestamp
    score: Optional[int] = None                 # LLM-assigned
    confirmed: bool = False                     # LLM-managed
    notes: Optional[str] = None                 # LLM-managed freetext


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

    fields_str = ",\n".join(fields)
    return f"@misc{{{key},\n{fields_str}\n}}"


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
    """In-memory research document tracker for the current session."""

    def __init__(self):
        self._records: dict[str, CitationRecord] = {}

    def track(self, record: CitationRecord) -> None:
        """Upsert a record — updates metadata, preserves score/confirmed/notes."""
        if record.doi in self._records:
            existing = self._records[record.doi]
            # Preserve user-managed fields
            record.score = existing.score
            record.confirmed = existing.confirmed
            record.notes = existing.notes
            record.added = existing.added
        else:
            record.added = record.added or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._records[record.doi] = record

    def remove(self, dois: list[str]) -> list[str]:
        """Batch remove by DOI. Returns list of actually removed DOIs."""
        removed = []
        for doi in dois:
            if doi in self._records:
                del self._records[doi]
                removed.append(doi)
        return removed

    def set_score(self, doi: str, value: int) -> bool:
        """Set score for a paper. Returns False if DOI not found."""
        if doi not in self._records:
            return False
        self._records[doi].score = value
        return True

    def confirm(self, doi: str) -> bool:
        """Mark a paper as confirmed. Returns False if DOI not found."""
        if doi not in self._records:
            return False
        self._records[doi].confirmed = True
        return True

    def set_note(self, doi: str, text: str) -> bool:
        """Set freetext note. Returns False if DOI not found."""
        if doi not in self._records:
            return False
        self._records[doi].notes = text
        return True

    def list_all(self) -> list[CitationRecord]:
        """Return all records sorted by added timestamp."""
        return sorted(self._records.values(), key=lambda r: r.added or "")

    def count(self) -> int:
        """Return total number of tracked papers."""
        return len(self._records)

    def confirmed_count(self) -> int:
        """Return number of confirmed papers."""
        return sum(1 for r in self._records.values() if r.confirmed)

    def export_bibtex(self) -> str:
        """Export all records as a BibTeX file."""
        entries = [record_to_bibtex(r) for r in self.list_all()]
        return "\n\n".join(entries)

    def export_ris(self) -> str:
        """Export all records as an RIS file."""
        entries = [record_to_ris(r) for r in self.list_all()]
        return "\n\n".join(entries)

    def export_json(self) -> str:
        """Export full shelf as JSON string for agent memory persistence."""
        return json.dumps(
            {doi: asdict(rec) for doi, rec in self._records.items()},
            indent=2, ensure_ascii=False,
        )

    def import_json(self, data: str) -> int:
        """Import shelf from JSON string. Returns count of records imported."""
        parsed = json.loads(data)
        count = 0
        for doi, rec_dict in parsed.items():
            record = CitationRecord(**rec_dict)
            if doi not in self._records:
                self._records[doi] = record
                count += 1
            else:
                # Merge: update metadata, preserve local score/confirmed/notes
                existing = self._records[doi]
                record.score = existing.score
                record.confirmed = existing.confirmed
                record.notes = existing.notes
                record.added = existing.added
                self._records[doi] = record
                count += 1
        return count

    def clear(self) -> int:
        """Remove all entries. Returns count removed."""
        count = len(self._records)
        self._records.clear()
        return count

    def status_line(self) -> Optional[str]:
        """Compact status for frontmatter. Returns None if shelf is empty."""
        total = len(self._records)
        if total == 0:
            return None
        confirmed = self.confirmed_count()
        return f"{total} tracked ({confirmed} confirmed) — use ResearchShelf to review"


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


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_shelf_list(records: list[CitationRecord]) -> str:
    """Format shelf contents as a compact markdown table."""
    if not records:
        return "Research shelf is empty."

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
    return "\n".join(lines)


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
            "For export: format name (bibtex, ris, json). "
            "For import: the JSON string to import. "
            "For list/clear: ignored (pass any value)."
        ),
    )] = "",
) -> str:
    """Manage the research shelf — a persistent tracker for inspected papers."""
    shelf = _get_shelf()

    if action == "list":
        records = shelf.list_all()
        return _format_shelf_list(records)

    elif action == "confirm":
        doi = query.strip()
        if not doi:
            return "Error: DOI is required for confirm action."
        if shelf.confirm(doi):
            return f"Confirmed: {doi}"
        return f"Error: DOI not found on shelf: {doi}"

    elif action == "remove":
        dois = [d.strip() for d in query.split(",") if d.strip()]
        if not dois:
            return "Error: At least one DOI is required for remove action."
        removed = shelf.remove(dois)
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
        if shelf.set_score(doi, value):
            return f"Score set to {value} for {doi}"
        return f"Error: DOI not found on shelf: {doi}"

    elif action == "note":
        parts = query.strip().split(None, 1)
        if len(parts) < 2:
            return "Error: note action requires 'DOI TEXT'."
        doi, text = parts
        if shelf.set_note(doi, text):
            return f"Note set for {doi}"
        return f"Error: DOI not found on shelf: {doi}"

    elif action == "export":
        fmt = query.strip().lower()
        if fmt == "bibtex":
            result = shelf.export_bibtex()
            return result if result else "Shelf is empty."
        elif fmt == "ris":
            result = shelf.export_ris()
            return result if result else "Shelf is empty."
        elif fmt == "json":
            return shelf.export_json()
        else:
            return f"Error: Unknown export format '{fmt}'. Use bibtex, ris, or json."

    elif action == "import":
        if not query.strip():
            return "Error: JSON data is required for import action."
        try:
            count = shelf.import_json(query)
            return f"Imported {count} record(s)."
        except (json.JSONDecodeError, TypeError) as e:
            return f"Error: Invalid JSON — {e}"

    elif action == "clear":
        count = shelf.clear()
        return f"Cleared {count} record(s) from shelf."

    else:
        return (
            f"Error: Unknown action '{action}'. "
            "Valid actions: list, confirm, remove, score, note, export, import, clear"
        )
