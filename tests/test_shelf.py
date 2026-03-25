"""Tests for kagi_research_mcp.shelf module."""

import json

import pytest

from kagi_research_mcp.shelf import (
    CitationRecord,
    ResearchShelf,
    _get_shelf,
    _reset_shelf,
    record_to_bibtex,
    record_to_ris,
    research_shelf,
    _format_shelf_list,
)


@pytest.fixture
def shelf():
    """Create a fresh in-memory shelf."""
    return ResearchShelf()


@pytest.fixture
def sample_record():
    return CitationRecord(
        doi="10.48550/arXiv.1706.03762",
        title="Attention Is All You Need",
        authors=["Vaswani, Ashish", "Shazeer, Noam"],
        year=2017,
        venue="NeurIPS",
        source_tool="arxiv",
        citation_apa="Vaswani, A. et al. (2017). Attention is all you need.",
    )


@pytest.fixture
def sample_record_2():
    return CitationRecord(
        doi="10.1145/3442188.3445922",
        title="On the Dangers of Stochastic Parrots",
        authors=["Bender, Emily M.", "Gebru, Timnit"],
        year=2021,
        venue="FAccT",
        source_tool="semantic_scholar",
        bibtex="@article{bender2021, author={Bender and Gebru}, title={Parrots}}",
    )


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------

class TestShelfCrud:
    def test_track_and_list(self, shelf, sample_record):
        shelf.track(sample_record)
        records = shelf.list_all()
        assert len(records) == 1
        assert records[0].doi == "10.48550/arXiv.1706.03762"
        assert records[0].title == "Attention Is All You Need"
        assert records[0].added is not None

    def test_track_upsert_preserves_user_fields(self, shelf, sample_record):
        shelf.track(sample_record)
        shelf.set_score(sample_record.doi, 8)
        shelf.confirm(sample_record.doi)
        shelf.set_note(sample_record.doi, "Seminal transformer paper")

        # Re-track with updated metadata
        updated = CitationRecord(
            doi=sample_record.doi,
            title="Attention Is All You Need (v2)",
            authors=["Vaswani, Ashish"],
            source_tool="arxiv",
        )
        shelf.track(updated)

        records = shelf.list_all()
        assert len(records) == 1
        assert records[0].title == "Attention Is All You Need (v2)"
        assert records[0].score == 8
        assert records[0].confirmed is True
        assert records[0].notes == "Seminal transformer paper"

    def test_remove_single(self, shelf, sample_record):
        shelf.track(sample_record)
        removed = shelf.remove([sample_record.doi])
        assert removed == [sample_record.doi]
        assert shelf.list_all() == []

    def test_remove_batch(self, shelf, sample_record, sample_record_2):
        shelf.track(sample_record)
        shelf.track(sample_record_2)
        removed = shelf.remove([sample_record.doi, sample_record_2.doi])
        assert len(removed) == 2
        assert shelf.list_all() == []

    def test_remove_nonexistent(self, shelf):
        removed = shelf.remove(["10.9999/fake"])
        assert removed == []

    def test_score(self, shelf, sample_record):
        shelf.track(sample_record)
        assert shelf.set_score(sample_record.doi, 9)
        assert shelf.list_all()[0].score == 9

    def test_score_nonexistent(self, shelf):
        assert shelf.set_score("10.9999/fake", 5) is False

    def test_confirm(self, shelf, sample_record):
        shelf.track(sample_record)
        assert shelf.confirm(sample_record.doi)
        assert shelf.list_all()[0].confirmed is True

    def test_note(self, shelf, sample_record):
        shelf.track(sample_record)
        assert shelf.set_note(sample_record.doi, "Important paper")
        assert shelf.list_all()[0].notes == "Important paper"

    def test_clear(self, shelf, sample_record, sample_record_2):
        shelf.track(sample_record)
        shelf.track(sample_record_2)
        count = shelf.clear()
        assert count == 2
        assert shelf.list_all() == []

    def test_count_and_confirmed_count(self, shelf, sample_record, sample_record_2):
        shelf.track(sample_record)
        shelf.track(sample_record_2)
        shelf.confirm(sample_record.doi)
        assert shelf.count() == 2
        assert shelf.confirmed_count() == 1


# ---------------------------------------------------------------------------
# Cross-DOI deduplication
# ---------------------------------------------------------------------------

class TestShelfDedup:
    def test_arxiv_then_journal_dedup(self, shelf):
        """arXiv entry should merge when journal DOI arrives via S2."""
        shelf.track(CitationRecord(
            doi="10.48550/arXiv.2411.08909",
            title="LC-PLM",
            authors=["Author A"],
            source_tool="arxiv",
        ))
        assert len(shelf.list_all()) == 1

        # S2 arrives with journal DOI + arXiv alt
        shelf.track(CitationRecord(
            doi="10.1101/2024.10.29.620988",
            title="LC-PLM: Long-context Protein Language Modeling",
            authors=["Author A", "Author B"],
            alt_dois=["10.48550/arXiv.2411.08909"],
            source_tool="semantic_scholar",
        ))
        assert len(shelf.list_all()) == 1
        rec = shelf.list_all()[0]
        # Journal DOI becomes primary (not a preprint DOI)
        assert rec.doi == "10.1101/2024.10.29.620988"
        assert "10.48550/arXiv.2411.08909" in rec.alt_dois

    def test_journal_then_arxiv_dedup(self, shelf):
        """bioRxiv entry should merge when arXiv DOI arrives, bioRxiv keeps primary."""
        shelf.track(CitationRecord(
            doi="10.1101/2024.10.29.620988",
            title="LC-PLM",
            source_tool="semantic_scholar",
        ))
        # arXiv arrives with bioRxiv DOI as alt
        shelf.track(CitationRecord(
            doi="10.48550/arXiv.2411.08909",
            title="LC-PLM",
            alt_dois=["10.1101/2024.10.29.620988"],
            source_tool="arxiv",
        ))
        assert len(shelf.list_all()) == 1
        rec = shelf.list_all()[0]
        # bioRxiv DOI has higher priority than arXiv DOI
        assert rec.doi == "10.1101/2024.10.29.620988"
        assert "10.48550/arXiv.2411.08909" in rec.alt_dois

    def test_real_journal_doi_preferred(self, shelf):
        """A real journal DOI should always win over preprint DOIs."""
        shelf.track(CitationRecord(
            doi="10.48550/arXiv.1706.03762",
            title="Attention Is All You Need",
            source_tool="arxiv",
        ))
        shelf.track(CitationRecord(
            doi="10.5555/3295222.3295349",
            title="Attention is All you Need",
            alt_dois=["10.48550/arXiv.1706.03762"],
            source_tool="semantic_scholar",
        ))
        assert len(shelf.list_all()) == 1
        rec = shelf.list_all()[0]
        assert rec.doi == "10.5555/3295222.3295349"
        assert "10.48550/arXiv.1706.03762" in rec.alt_dois

    def test_dedup_preserves_user_fields(self, shelf):
        """Merge should preserve score/confirmed/notes from existing entry."""
        shelf.track(CitationRecord(
            doi="10.48550/arXiv.1706.03762",
            title="Attention Is All You Need",
            source_tool="arxiv",
        ))
        shelf.set_score("10.48550/arXiv.1706.03762", 9)
        shelf.confirm("10.48550/arXiv.1706.03762")
        shelf.set_note("10.48550/arXiv.1706.03762", "Foundational paper")

        shelf.track(CitationRecord(
            doi="10.5555/3295222.3295349",
            title="Attention is All you Need",
            alt_dois=["10.48550/arXiv.1706.03762"],
            source_tool="semantic_scholar",
        ))
        rec = shelf.list_all()[0]
        assert rec.score == 9
        assert rec.confirmed is True
        assert rec.notes == "Foundational paper"

    def test_no_false_dedup(self, shelf):
        """Papers with no overlapping DOIs should not merge."""
        shelf.track(CitationRecord(doi="10.1234/a", title="Paper A"))
        shelf.track(CitationRecord(doi="10.1234/b", title="Paper B"))
        assert len(shelf.list_all()) == 2

    def test_resolve_doi_via_alt(self, shelf):
        """Operations by alt DOI should resolve to the correct record."""
        shelf.track(CitationRecord(
            doi="10.5555/3295222.3295349",
            title="Attention",
            alt_dois=["10.48550/arXiv.1706.03762"],
        ))
        assert shelf.set_score("10.48550/arXiv.1706.03762", 8)
        assert shelf.list_all()[0].score == 8


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestShelfPersistence:
    def test_export_import_simulates_session_restore(self, sample_record):
        """Export from one shelf, import into a fresh one — simulates agent memory restore."""
        shelf1 = ResearchShelf()
        shelf1.track(sample_record)
        shelf1.set_score(sample_record.doi, 7)
        exported = shelf1.export_json()

        shelf2 = ResearchShelf()
        new, updated = shelf2.import_json(exported)
        assert new == 1
        assert updated == 0
        records = shelf2.list_all()
        assert len(records) == 1
        assert records[0].score == 7

    def test_fresh_shelf_is_empty(self):
        shelf = ResearchShelf()
        assert shelf.list_all() == []
        assert shelf.count() == 0


# ---------------------------------------------------------------------------
# Export formats
# ---------------------------------------------------------------------------

class TestBibtexExport:
    def test_uses_existing_bibtex(self, sample_record_2):
        result = record_to_bibtex(sample_record_2)
        assert "@article{bender2021" in result

    def test_generates_bibtex(self, sample_record):
        result = record_to_bibtex(sample_record)
        assert "@misc{vaswani2017" in result
        assert "Vaswani, Ashish and Shazeer, Noam" in result
        assert "doi = {10.48550/arXiv.1706.03762}" in result

    def test_shelf_export_bibtex(self, shelf, sample_record, sample_record_2):
        shelf.track(sample_record)
        shelf.track(sample_record_2)
        result = shelf.export_bibtex()
        assert "vaswani2017" in result or "Vaswani" in result
        assert "bender2021" in result

    def test_empty_shelf_bibtex(self, shelf):
        assert shelf.export_bibtex() == ""


class TestRisExport:
    def test_generates_ris(self, sample_record):
        result = record_to_ris(sample_record)
        assert "TY  - GEN" in result
        assert "AU  - Vaswani, Ashish" in result
        assert "AU  - Shazeer, Noam" in result
        assert "TI  - Attention Is All You Need" in result
        assert "PY  - 2017" in result
        assert "DO  - 10.48550/arXiv.1706.03762" in result
        assert "ER  - " in result

    def test_shelf_export_ris(self, shelf, sample_record):
        shelf.track(sample_record)
        result = shelf.export_ris()
        assert "TY  - GEN" in result
        assert "ER  - " in result


# ---------------------------------------------------------------------------
# JSON import/export
# ---------------------------------------------------------------------------

class TestJsonRoundtrip:
    def test_export_import_roundtrip(self, shelf, sample_record, sample_record_2):
        shelf.track(sample_record)
        shelf.track(sample_record_2)
        shelf.set_score(sample_record.doi, 8)
        shelf.confirm(sample_record_2.doi)

        exported = shelf.export_json()

        # Import into fresh shelf
        shelf2 = ResearchShelf()
        new, updated = shelf2.import_json(exported)
        assert new == 2
        assert updated == 0

        records = {r.doi: r for r in shelf2.list_all()}
        assert records[sample_record.doi].score == 8
        assert records[sample_record_2.doi].confirmed is True

    def test_import_merges_preserves_local(self, shelf, sample_record):
        shelf.track(sample_record)
        shelf.set_score(sample_record.doi, 5)

        # Import same DOI with updated title
        import_data = json.dumps({
            sample_record.doi: {
                "doi": sample_record.doi,
                "title": "Updated Title",
                "authors": ["Vaswani, Ashish"],
            }
        })
        shelf.import_json(import_data)

        records = shelf.list_all()
        assert records[0].title == "Updated Title"
        assert records[0].score == 5  # preserved


# ---------------------------------------------------------------------------
# Status line and formatting
# ---------------------------------------------------------------------------

class TestStatusLine:
    def test_empty_shelf(self, shelf):
        assert shelf.status_line() is None

    def test_nonempty_shelf(self, shelf, sample_record, sample_record_2):
        shelf.track(sample_record)
        shelf.track(sample_record_2)
        shelf.confirm(sample_record.doi)
        status = shelf.status_line()
        assert "2 tracked" in status
        assert "1 confirmed" in status
        assert "ResearchShelf" in status

    def test_format_shelf_list_empty(self):
        assert "empty" in _format_shelf_list([])

    def test_format_shelf_list_table(self, sample_record):
        result = _format_shelf_list([sample_record])
        assert "Attention Is All You Need" in result
        assert "10.48550/arXiv.1706.03762" in result
        assert "arxiv" in result


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------

class TestResearchShelfTool:
    @pytest.fixture(autouse=True)
    def _use_fresh_shelf(self):
        """Reset the global shelf for each test."""
        _reset_shelf()
        yield
        _reset_shelf()

    @pytest.mark.asyncio
    async def test_list_empty(self):
        result = await research_shelf("list")
        assert "empty" in result

    @pytest.mark.asyncio
    async def test_track_and_list(self):
        shelf = _get_shelf()
        shelf.track(CitationRecord(
            doi="10.1234/test", title="Test Paper", authors=["Author, Test"],
        ))
        result = await research_shelf("list")
        assert "Test Paper" in result
        assert "10.1234/test" in result

    @pytest.mark.asyncio
    async def test_confirm(self):
        shelf = _get_shelf()
        shelf.track(CitationRecord(doi="10.1234/test", title="Test"))
        result = await research_shelf("confirm", "10.1234/test")
        assert "Confirmed" in result

    @pytest.mark.asyncio
    async def test_confirm_nonexistent(self):
        result = await research_shelf("confirm", "10.9999/fake")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_remove(self):
        shelf = _get_shelf()
        shelf.track(CitationRecord(doi="10.1234/a", title="A"))
        shelf.track(CitationRecord(doi="10.1234/b", title="B"))
        result = await research_shelf("remove", "10.1234/a, 10.1234/b")
        assert "Removed 2" in result

    @pytest.mark.asyncio
    async def test_score(self):
        shelf = _get_shelf()
        shelf.track(CitationRecord(doi="10.1234/test", title="Test"))
        result = await research_shelf("score", "10.1234/test 8")
        assert "Score set to 8" in result

    @pytest.mark.asyncio
    async def test_score_invalid(self):
        shelf = _get_shelf()
        shelf.track(CitationRecord(doi="10.1234/test", title="Test"))
        result = await research_shelf("score", "10.1234/test abc")
        assert "integer" in result

    @pytest.mark.asyncio
    async def test_note(self):
        shelf = _get_shelf()
        shelf.track(CitationRecord(doi="10.1234/test", title="Test"))
        result = await research_shelf("note", "10.1234/test Very important finding")
        assert "Note set" in result

    @pytest.mark.asyncio
    async def test_export_bibtex(self):
        shelf = _get_shelf()
        shelf.track(CitationRecord(
            doi="10.1234/test", title="Test Paper",
            authors=["Author, Test"], year=2025,
        ))
        result = await research_shelf("export", "bibtex")
        assert "@misc" in result
        assert "Test Paper" in result

    @pytest.mark.asyncio
    async def test_export_ris(self):
        shelf = _get_shelf()
        shelf.track(CitationRecord(
            doi="10.1234/test", title="Test Paper",
            authors=["Author, Test"], year=2025,
        ))
        result = await research_shelf("export", "ris")
        assert "TY  - GEN" in result

    @pytest.mark.asyncio
    async def test_export_json(self):
        shelf = _get_shelf()
        shelf.track(CitationRecord(doi="10.1234/test", title="Test"))
        result = await research_shelf("export", "json")
        data = json.loads(result)
        assert "10.1234/test" in data

    @pytest.mark.asyncio
    async def test_import_json(self):
        import_data = json.dumps({
            "10.1234/imported": {
                "doi": "10.1234/imported",
                "title": "Imported Paper",
                "authors": ["Author, A"],
            }
        })
        result = await research_shelf("import", import_data)
        assert "1 new" in result
        list_result = await research_shelf("list")
        assert "Imported Paper" in list_result

    @pytest.mark.asyncio
    async def test_clear(self):
        shelf = _get_shelf()
        shelf.track(CitationRecord(doi="10.1234/a", title="A"))
        shelf.track(CitationRecord(doi="10.1234/b", title="B"))
        result = await research_shelf("clear")
        assert "Cleared 2" in result

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        result = await research_shelf("invalid")
        assert "Unknown action" in result

    @pytest.mark.asyncio
    async def test_export_empty(self):
        result = await research_shelf("export", "bibtex")
        assert "empty" in result.lower()
