"""Tests for parkour_mcp.shelf module."""

import json

import pytest

from parkour_mcp.shelf import (
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
    @pytest.mark.asyncio
    async def test_track_and_list(self, shelf, sample_record):
        await shelf.track(sample_record)
        records = await shelf.list_all()
        assert len(records) == 1
        assert records[0].doi == "10.48550/arXiv.1706.03762"
        assert records[0].title == "Attention Is All You Need"
        assert records[0].added is not None

    @pytest.mark.asyncio
    async def test_track_upsert_preserves_user_fields(self, shelf, sample_record):
        await shelf.track(sample_record)
        await shelf.set_score(sample_record.doi, 8)
        await shelf.confirm(sample_record.doi)
        await shelf.set_note(sample_record.doi, "Seminal transformer paper")

        # Re-track with updated metadata
        updated = CitationRecord(
            doi=sample_record.doi,
            title="Attention Is All You Need (v2)",
            authors=["Vaswani, Ashish"],
            source_tool="arxiv",
        )
        await shelf.track(updated)

        records = await shelf.list_all()
        assert len(records) == 1
        assert records[0].title == "Attention Is All You Need (v2)"
        assert records[0].score == 8
        assert records[0].confirmed is True
        assert records[0].notes == "Seminal transformer paper"

    @pytest.mark.asyncio
    async def test_remove_single(self, shelf, sample_record):
        await shelf.track(sample_record)
        removed = await shelf.remove([sample_record.doi])
        assert removed == [sample_record.doi]
        assert await shelf.list_all() == []

    @pytest.mark.asyncio
    async def test_remove_batch(self, shelf, sample_record, sample_record_2):
        await shelf.track(sample_record)
        await shelf.track(sample_record_2)
        removed = await shelf.remove([sample_record.doi, sample_record_2.doi])
        assert len(removed) == 2
        assert await shelf.list_all() == []

    @pytest.mark.asyncio
    async def test_remove_nonexistent(self, shelf):
        removed = await shelf.remove(["10.9999/fake"])
        assert removed == []

    @pytest.mark.asyncio
    async def test_score(self, shelf, sample_record):
        await shelf.track(sample_record)
        assert await shelf.set_score(sample_record.doi, 9)
        assert (await shelf.list_all())[0].score == 9

    @pytest.mark.asyncio
    async def test_score_nonexistent(self, shelf):
        assert await shelf.set_score("10.9999/fake", 5) is False

    @pytest.mark.asyncio
    async def test_confirm(self, shelf, sample_record):
        await shelf.track(sample_record)
        assert await shelf.confirm(sample_record.doi)
        assert (await shelf.list_all())[0].confirmed is True

    @pytest.mark.asyncio
    async def test_note(self, shelf, sample_record):
        await shelf.track(sample_record)
        assert await shelf.set_note(sample_record.doi, "Important paper")
        assert (await shelf.list_all())[0].notes == "Important paper"

    @pytest.mark.asyncio
    async def test_clear(self, shelf, sample_record, sample_record_2):
        await shelf.track(sample_record)
        await shelf.track(sample_record_2)
        count = await shelf.clear()
        assert count == 2
        assert await shelf.list_all() == []

    @pytest.mark.asyncio
    async def test_count_and_confirmed_count(self, shelf, sample_record, sample_record_2):
        await shelf.track(sample_record)
        await shelf.track(sample_record_2)
        await shelf.confirm(sample_record.doi)
        records = await shelf.list_all()
        assert len(records) == 2
        assert sum(1 for r in records if r.confirmed) == 1


# ---------------------------------------------------------------------------
# Cross-DOI deduplication
# ---------------------------------------------------------------------------

class TestShelfDedup:
    @pytest.mark.asyncio
    async def test_arxiv_then_journal_dedup(self, shelf):
        """arXiv entry should merge when journal DOI arrives via S2."""
        await shelf.track(CitationRecord(
            doi="10.48550/arXiv.2411.08909",
            title="LC-PLM",
            authors=["Author A"],
            source_tool="arxiv",
        ))
        assert len(await shelf.list_all()) == 1

        # S2 arrives with journal DOI + arXiv alt
        await shelf.track(CitationRecord(
            doi="10.1101/2024.10.29.620988",
            title="LC-PLM: Long-context Protein Language Modeling",
            authors=["Author A", "Author B"],
            alt_dois=["10.48550/arXiv.2411.08909"],
            source_tool="semantic_scholar",
        ))
        assert len(await shelf.list_all()) == 1
        rec = (await shelf.list_all())[0]
        # Journal DOI becomes primary (not a preprint DOI)
        assert rec.doi == "10.1101/2024.10.29.620988"
        assert "10.48550/arXiv.2411.08909" in rec.alt_dois

    @pytest.mark.asyncio
    async def test_journal_then_arxiv_dedup(self, shelf):
        """bioRxiv entry should merge when arXiv DOI arrives, bioRxiv keeps primary."""
        await shelf.track(CitationRecord(
            doi="10.1101/2024.10.29.620988",
            title="LC-PLM",
            source_tool="semantic_scholar",
        ))
        # arXiv arrives with bioRxiv DOI as alt
        await shelf.track(CitationRecord(
            doi="10.48550/arXiv.2411.08909",
            title="LC-PLM",
            alt_dois=["10.1101/2024.10.29.620988"],
            source_tool="arxiv",
        ))
        assert len(await shelf.list_all()) == 1
        rec = (await shelf.list_all())[0]
        # bioRxiv DOI has higher priority than arXiv DOI
        assert rec.doi == "10.1101/2024.10.29.620988"
        assert "10.48550/arXiv.2411.08909" in rec.alt_dois

    @pytest.mark.asyncio
    async def test_real_journal_doi_preferred(self, shelf):
        """A real journal DOI should always win over preprint DOIs."""
        await shelf.track(CitationRecord(
            doi="10.48550/arXiv.1706.03762",
            title="Attention Is All You Need",
            source_tool="arxiv",
        ))
        await shelf.track(CitationRecord(
            doi="10.5555/3295222.3295349",
            title="Attention is All you Need",
            alt_dois=["10.48550/arXiv.1706.03762"],
            source_tool="semantic_scholar",
        ))
        assert len(await shelf.list_all()) == 1
        rec = (await shelf.list_all())[0]
        assert rec.doi == "10.5555/3295222.3295349"
        assert "10.48550/arXiv.1706.03762" in rec.alt_dois

    @pytest.mark.asyncio
    async def test_dedup_preserves_user_fields(self, shelf):
        """Merge should preserve score/confirmed/notes from existing entry."""
        await shelf.track(CitationRecord(
            doi="10.48550/arXiv.1706.03762",
            title="Attention Is All You Need",
            source_tool="arxiv",
        ))
        await shelf.set_score("10.48550/arXiv.1706.03762", 9)
        await shelf.confirm("10.48550/arXiv.1706.03762")
        await shelf.set_note("10.48550/arXiv.1706.03762", "Foundational paper")

        await shelf.track(CitationRecord(
            doi="10.5555/3295222.3295349",
            title="Attention is All you Need",
            alt_dois=["10.48550/arXiv.1706.03762"],
            source_tool="semantic_scholar",
        ))
        rec = (await shelf.list_all())[0]
        assert rec.score == 9
        assert rec.confirmed is True
        assert rec.notes == "Foundational paper"

    @pytest.mark.asyncio
    async def test_no_false_dedup(self, shelf):
        """Papers with no overlapping DOIs should not merge."""
        await shelf.track(CitationRecord(doi="10.1234/a", title="Paper A"))
        await shelf.track(CitationRecord(doi="10.1234/b", title="Paper B"))
        assert len(await shelf.list_all()) == 2

    @pytest.mark.asyncio
    async def test_resolve_doi_via_alt(self, shelf):
        """Operations by alt DOI should resolve to the correct record."""
        await shelf.track(CitationRecord(
            doi="10.5555/3295222.3295349",
            title="Attention",
            alt_dois=["10.48550/arXiv.1706.03762"],
        ))
        assert await shelf.set_score("10.48550/arXiv.1706.03762", 8)
        assert (await shelf.list_all())[0].score == 8


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestShelfPersistence:
    @pytest.mark.asyncio
    async def test_export_import_simulates_session_restore(self, sample_record):
        """Export from one shelf, import into a fresh one — simulates agent memory restore."""
        shelf1 = ResearchShelf()
        await shelf1.track(sample_record)
        await shelf1.set_score(sample_record.doi, 7)
        exported = await shelf1.export_json()

        shelf2 = ResearchShelf()
        new, updated = await shelf2.import_json(exported)
        assert new == 1
        assert updated == 0
        records = await shelf2.list_all()
        assert len(records) == 1
        assert records[0].score == 7

    @pytest.mark.asyncio
    async def test_fresh_shelf_is_empty(self):
        shelf = ResearchShelf()
        assert await shelf.list_all() == []


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

    @pytest.mark.asyncio
    async def test_shelf_export_bibtex(self, shelf, sample_record, sample_record_2):
        await shelf.track(sample_record)
        await shelf.track(sample_record_2)
        result = await shelf.export_bibtex()
        assert "vaswani2017" in result or "Vaswani" in result
        assert "bender2021" in result

    @pytest.mark.asyncio
    async def test_empty_shelf_bibtex(self, shelf):
        assert await shelf.export_bibtex() == ""


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

    @pytest.mark.asyncio
    async def test_shelf_export_ris(self, shelf, sample_record):
        await shelf.track(sample_record)
        result = await shelf.export_ris()
        assert "TY  - GEN" in result
        assert "ER  - " in result


# ---------------------------------------------------------------------------
# JSON import/export
# ---------------------------------------------------------------------------

class TestJsonRoundtrip:
    @pytest.mark.asyncio
    async def test_export_import_roundtrip(self, shelf, sample_record, sample_record_2):
        await shelf.track(sample_record)
        await shelf.track(sample_record_2)
        await shelf.set_score(sample_record.doi, 8)
        await shelf.confirm(sample_record_2.doi)

        exported = await shelf.export_json()

        # Import into fresh shelf
        shelf2 = ResearchShelf()
        new, updated = await shelf2.import_json(exported)
        assert new == 2
        assert updated == 0

        records = {r.doi: r for r in await shelf2.list_all()}
        assert records[sample_record.doi].score == 8
        assert records[sample_record_2.doi].confirmed is True

    @pytest.mark.asyncio
    async def test_import_merges_preserves_local(self, shelf, sample_record):
        await shelf.track(sample_record)
        await shelf.set_score(sample_record.doi, 5)

        # Import same DOI with updated title
        import_data = json.dumps({
            sample_record.doi: {
                "doi": sample_record.doi,
                "title": "Updated Title",
                "authors": ["Vaswani, Ashish"],
            }
        })
        await shelf.import_json(import_data)

        records = await shelf.list_all()
        assert records[0].title == "Updated Title"
        assert records[0].score == 5  # preserved


# ---------------------------------------------------------------------------
# Status line and formatting
# ---------------------------------------------------------------------------

class TestStatusLine:
    @pytest.mark.asyncio
    async def test_empty_shelf(self, shelf):
        assert await shelf.status_line() is None

    @pytest.mark.asyncio
    async def test_nonempty_shelf(self, shelf, sample_record, sample_record_2):
        await shelf.track(sample_record)
        await shelf.track(sample_record_2)
        await shelf.confirm(sample_record.doi)
        status = await shelf.status_line()
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
        await shelf.track(CitationRecord(
            doi="10.1234/test", title="Test Paper", authors=["Author, Test"],
        ))
        result = await research_shelf("list")
        assert "Test Paper" in result
        assert "10.1234/test" in result

    @pytest.mark.asyncio
    async def test_confirm(self):
        shelf = _get_shelf()
        await shelf.track(CitationRecord(doi="10.1234/test", title="Test"))
        result = await research_shelf("confirm", "10.1234/test")
        assert "Confirmed" in result

    @pytest.mark.asyncio
    async def test_confirm_nonexistent(self):
        result = await research_shelf("confirm", "10.9999/fake")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_remove(self):
        shelf = _get_shelf()
        await shelf.track(CitationRecord(doi="10.1234/a", title="A"))
        await shelf.track(CitationRecord(doi="10.1234/b", title="B"))
        result = await research_shelf("remove", "10.1234/a, 10.1234/b")
        assert "Removed 2" in result

    @pytest.mark.asyncio
    async def test_score(self):
        shelf = _get_shelf()
        await shelf.track(CitationRecord(doi="10.1234/test", title="Test"))
        result = await research_shelf("score", "10.1234/test 8")
        assert "Score set to 8" in result

    @pytest.mark.asyncio
    async def test_score_invalid(self):
        shelf = _get_shelf()
        await shelf.track(CitationRecord(doi="10.1234/test", title="Test"))
        result = await research_shelf("score", "10.1234/test abc")
        assert "integer" in result

    @pytest.mark.asyncio
    async def test_note(self):
        shelf = _get_shelf()
        await shelf.track(CitationRecord(doi="10.1234/test", title="Test"))
        result = await research_shelf("note", "10.1234/test Very important finding")
        assert "Note set" in result

    @pytest.mark.asyncio
    async def test_export_bibtex(self):
        shelf = _get_shelf()
        await shelf.track(CitationRecord(
            doi="10.1234/test", title="Test Paper",
            authors=["Author, Test"], year=2025,
        ))
        result = await research_shelf("export", "bibtex")
        assert "@misc" in result
        assert "Test Paper" in result

    @pytest.mark.asyncio
    async def test_export_ris(self):
        shelf = _get_shelf()
        await shelf.track(CitationRecord(
            doi="10.1234/test", title="Test Paper",
            authors=["Author, Test"], year=2025,
        ))
        result = await research_shelf("export", "ris")
        assert "TY  - GEN" in result

    @pytest.mark.asyncio
    async def test_export_json(self):
        shelf = _get_shelf()
        await shelf.track(CitationRecord(doi="10.1234/test", title="Test"))
        result = await research_shelf("export", "json")
        data = json.loads(result)
        # New structured format: {"active": {...}, "retracted": {...}}
        assert "active" in data
        assert "retracted" in data
        assert "10.1234/test" in data["active"]

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
        await shelf.track(CitationRecord(doi="10.1234/a", title="A"))
        await shelf.track(CitationRecord(doi="10.1234/b", title="B"))
        result = await research_shelf("clear")
        assert "Cleared 2" in result

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        result = await research_shelf("invalid")
        assert "Unknown action" in result


# ---------------------------------------------------------------------------
# Retraction bucket partitioning
# ---------------------------------------------------------------------------

@pytest.fixture
def retraction_notice():
    """Normalized retraction dict matching fetch_crossref_metadata shape."""
    return {
        "notice_doi": "10.1016/s0140-6736(20)31324-6",
        "date": "2020-06-05",
        "source": "retraction-watch",
        "label": "Retraction",
    }


class TestShelfRetractionPartition:
    @pytest.mark.asyncio
    async def test_new_retracted_routed_to_retracted_bucket(
        self, shelf, sample_record, retraction_notice,
    ):
        """A freshly-seen retracted paper lands in the retracted bucket,
        not the active bucket, and emits a shelving note."""
        sample_record.retraction = retraction_notice
        result = await shelf.track(sample_record)

        active = await shelf.list_all(section="active")
        retracted = await shelf.list_all(section="retracted")
        assert len(active) == 0
        assert len(retracted) == 1
        assert retracted[0].retraction == retraction_notice
        assert result.shelf_note is not None
        assert "retracted shelf bucket" in result.shelf_note
        assert "not added to active citations" in result.shelf_note

    @pytest.mark.asyncio
    async def test_active_entry_moved_to_retracted_on_reflagging(
        self, shelf, sample_record, retraction_notice,
    ):
        """An entry that lives in the active bucket migrates to the
        retracted bucket when re-inspected with retraction flag."""
        sample_record.score = 7
        sample_record.confirmed = True
        sample_record.notes = "cited in intro"
        await shelf.track(sample_record)

        reflagged = CitationRecord(
            doi=sample_record.doi,
            title=sample_record.title,
            retraction=retraction_notice,
        )
        result = await shelf.track(reflagged)

        active = await shelf.list_all(section="active")
        retracted = await shelf.list_all(section="retracted")
        assert len(active) == 0
        assert len(retracted) == 1
        # User-managed fields preserved through the move
        assert retracted[0].score == 7
        assert retracted[0].confirmed is True
        assert retracted[0].notes == "cited in intro"
        assert retracted[0].retraction == retraction_notice
        assert result.shelf_note is not None
        assert "moved to retracted bucket" in result.shelf_note

    @pytest.mark.asyncio
    async def test_re_inspecting_retracted_entry_does_not_resurrect(
        self, shelf, sample_record, retraction_notice,
    ):
        """A non-flagged re-inspection of a known-retracted paper does NOT
        move it back to the active bucket (retraction status is sticky)."""
        sample_record.retraction = retraction_notice
        await shelf.track(sample_record)

        # Re-inspect via a non-flagged record (e.g. arXiv path where
        # CrossRef enrichment happened to fail or find no flag).
        revisit = CitationRecord(
            doi=sample_record.doi,
            title=sample_record.title,
            retraction=None,
        )
        result = await shelf.track(revisit)

        active = await shelf.list_all(section="active")
        retracted = await shelf.list_all(section="retracted")
        assert len(active) == 0
        assert len(retracted) == 1
        assert retracted[0].retraction == retraction_notice
        # No notable event — the paper was already retracted
        assert result.shelf_note is None

    @pytest.mark.asyncio
    async def test_version_linked_alt_doi_pulls_into_retracted(
        self, shelf, retraction_notice,
    ):
        """When a non-flagged record shares an alt_doi with an existing
        retracted entry, it merges into the retracted bucket rather than
        creating a duplicate active entry."""
        # First: retracted journal DOI arrives with preprint as alt.
        journal = CitationRecord(
            doi="10.1056/nejmoa2007764",
            title="Hydroxychloroquine",
            alt_dois=["10.48550/arxiv.2004.00000"],
            retraction=retraction_notice,
        )
        await shelf.track(journal)

        # Later: the arXiv preprint gets inspected independently via
        # arxiv.py, with no retraction flag (the arXiv path uses the
        # arXiv DOI as primary; CrossRef may not flag it).
        preprint = CitationRecord(
            doi="10.48550/arxiv.2004.00000",
            title="Hydroxychloroquine (arXiv)",
            retraction=None,
        )
        result = await shelf.track(preprint)

        active = await shelf.list_all(section="active")
        retracted = await shelf.list_all(section="retracted")
        assert len(active) == 0
        assert len(retracted) == 1
        # Retraction flag preserved through the merge
        assert retracted[0].retraction == retraction_notice
        assert result.shelf_note is None  # merge, no new event

    @pytest.mark.asyncio
    async def test_retracted_shelf_reported_in_status_line(
        self, shelf, sample_record, sample_record_2, retraction_notice,
    ):
        """Status line includes retracted count when non-zero."""
        await shelf.track(sample_record)
        sample_record_2.retraction = retraction_notice
        await shelf.track(sample_record_2)
        status = await shelf.status_line()
        assert "1 tracked" in status
        assert "1 retracted" in status

    @pytest.mark.asyncio
    async def test_counts_returns_both_buckets(
        self, shelf, sample_record, sample_record_2, retraction_notice,
    ):
        sample_record_2.retraction = retraction_notice
        await shelf.track(sample_record)
        await shelf.track(sample_record_2)
        active, retracted = await shelf.counts()
        assert active == 1
        assert retracted == 1

    @pytest.mark.asyncio
    async def test_list_all_sections(
        self, shelf, sample_record, sample_record_2, retraction_notice,
    ):
        await shelf.track(sample_record)
        sample_record_2.retraction = retraction_notice
        await shelf.track(sample_record_2)
        assert len(await shelf.list_all(section="active")) == 1
        assert len(await shelf.list_all(section="retracted")) == 1
        assert len(await shelf.list_all(section="all")) == 2
        # Default is active
        assert len(await shelf.list_all()) == 1

    @pytest.mark.asyncio
    async def test_format_shelf_list_retracted_bucket(self, retraction_notice):
        rec = CitationRecord(
            doi="10.1234/retracted",
            title="Bad Paper",
            retraction=retraction_notice,
        )
        result = _format_shelf_list([rec], bucket="retracted")
        assert "Bad Paper" in result
        assert "2020-06-05" in result
        assert "10.1016/s0140-6736(20)31324-6" in result
        assert "retraction-watch" in result

    def test_format_shelf_list_active_shows_hidden_footer(self, sample_record):
        result = _format_shelf_list(
            [sample_record], bucket="active", other_bucket_count=3,
        )
        assert "3 retracted entries hidden" in result

    @pytest.mark.asyncio
    async def test_remove_resolves_retracted_bucket(
        self, shelf, sample_record, retraction_notice,
    ):
        sample_record.retraction = retraction_notice
        await shelf.track(sample_record)
        removed = await shelf.remove([sample_record.doi])
        assert removed == [sample_record.doi]
        assert len(await shelf.list_all(section="retracted")) == 0

    @pytest.mark.asyncio
    async def test_set_score_works_on_retracted_bucket(
        self, shelf, sample_record, retraction_notice,
    ):
        """Retracted papers can still carry scores/notes for
        agent-managed tracking (e.g. studying retractions)."""
        sample_record.retraction = retraction_notice
        await shelf.track(sample_record)
        assert await shelf.set_score(sample_record.doi, 9)
        retracted = await shelf.list_all(section="retracted")
        assert retracted[0].score == 9

    @pytest.mark.asyncio
    async def test_export_bibtex_excludes_retracted_by_default(
        self, shelf, sample_record, sample_record_2, retraction_notice,
    ):
        await shelf.track(sample_record)
        sample_record_2.retraction = retraction_notice
        await shelf.track(sample_record_2)
        result = await shelf.export_bibtex()
        assert "Attention Is All You Need" in result
        assert "Parrots" not in result  # retracted, hidden
        assert "RETRACTED" not in result

    @pytest.mark.asyncio
    async def test_export_bibtex_includes_retracted_when_flagged(
        self, shelf, sample_record, sample_record_2, retraction_notice,
    ):
        await shelf.track(sample_record)
        sample_record_2.retraction = retraction_notice
        await shelf.track(sample_record_2)
        result = await shelf.export_bibtex(include_retracted=True)
        assert "Attention Is All You Need" in result
        assert "Parrots" in result
        assert "RETRACTED" in result
        assert "10.1016/s0140-6736(20)31324-6" in result
        assert "note = {" in result

    @pytest.mark.asyncio
    async def test_export_ris_includes_retracted_when_flagged(
        self, shelf, sample_record_2, retraction_notice,
    ):
        sample_record_2.retraction = retraction_notice
        await shelf.track(sample_record_2)
        result = await shelf.export_ris(include_retracted=True)
        assert "N1  - RETRACTED" in result
        assert "10.1016/s0140-6736(20)31324-6" in result

    @pytest.mark.asyncio
    async def test_export_json_has_both_buckets(
        self, shelf, sample_record, sample_record_2, retraction_notice,
    ):
        await shelf.track(sample_record)
        sample_record_2.retraction = retraction_notice
        await shelf.track(sample_record_2)
        payload = json.loads(await shelf.export_json())
        assert sample_record.doi in payload["active"]
        assert sample_record_2.doi in payload["retracted"]
        assert payload["retracted"][sample_record_2.doi]["retraction"] \
            == retraction_notice

    @pytest.mark.asyncio
    async def test_import_json_round_trips_retracted(
        self, shelf, sample_record, sample_record_2, retraction_notice,
    ):
        """Structured export → import preserves bucket placement."""
        await shelf.track(sample_record)
        sample_record_2.retraction = retraction_notice
        await shelf.track(sample_record_2)
        payload = await shelf.export_json()

        fresh = ResearchShelf()
        new_count, _ = await fresh.import_json(payload)
        assert new_count == 2
        assert len(await fresh.list_all(section="active")) == 1
        assert len(await fresh.list_all(section="retracted")) == 1

    @pytest.mark.asyncio
    async def test_import_legacy_flat_format_still_works(self, shelf):
        """Backward compatibility: old flat-dict exports still import."""
        legacy = json.dumps({
            "10.1234/legacy": {
                "doi": "10.1234/legacy",
                "title": "Legacy Export",
            }
        })
        new_count, _ = await shelf.import_json(legacy)
        assert new_count == 1
        assert len(await shelf.list_all()) == 1

    @pytest.mark.asyncio
    async def test_import_tolerates_unknown_fields(self, shelf):
        """Exports from other tool versions may carry fields this version
        doesn't know. Unknown keys must be silently dropped, not crash the
        import with TypeError from CitationRecord(**rec_dict).
        """
        payload = json.dumps({
            "10.1234/future": {
                "doi": "10.1234/future",
                "title": "Exported by a future version",
                "authors": ["Future, A"],
                # Fields that don't exist on the current CitationRecord —
                # previously raised TypeError, now silently dropped.
                "citation_apa": "Future, A. (2099). Exported by a future version.",
                "arxiv_version": "v42",
                "totally_made_up_future_field": {"nested": "data"},
            }
        })
        new_count, _ = await shelf.import_json(payload)
        assert new_count == 1
        records = await shelf.list_all()
        assert len(records) == 1
        assert records[0].doi == "10.1234/future"
        assert records[0].title == "Exported by a future version"


class TestResearchShelfToolRetraction:
    @pytest.fixture(autouse=True)
    def _use_fresh_shelf(self):
        _reset_shelf()
        yield
        _reset_shelf()

    @pytest.mark.asyncio
    async def test_list_retracted_section(self, retraction_notice):
        shelf = _get_shelf()
        await shelf.track(CitationRecord(
            doi="10.1234/retracted",
            title="Retracted Paper",
            retraction=retraction_notice,
        ))
        result = await research_shelf("list", "retracted")
        assert "Retracted Paper" in result
        assert "2020-06-05" in result

    @pytest.mark.asyncio
    async def test_list_all_section(self, retraction_notice):
        shelf = _get_shelf()
        await shelf.track(CitationRecord(doi="10.1234/active", title="Active"))
        await shelf.track(CitationRecord(
            doi="10.1234/retracted",
            title="Retracted",
            retraction=retraction_notice,
        ))
        result = await research_shelf("list", "all")
        assert "## Active" in result
        assert "## Retracted" in result
        assert "Active" in result
        assert "Retracted" in result

    @pytest.mark.asyncio
    async def test_list_default_shows_hidden_count(self, retraction_notice):
        shelf = _get_shelf()
        await shelf.track(CitationRecord(doi="10.1234/active", title="Active"))
        await shelf.track(CitationRecord(
            doi="10.1234/retracted",
            title="Bad",
            retraction=retraction_notice,
        ))
        result = await research_shelf("list", "")
        assert "Active" in result
        assert "Bad" not in result  # hidden
        assert "1 retracted entries hidden" in result

    @pytest.mark.asyncio
    async def test_list_invalid_section(self):
        result = await research_shelf("list", "bogus")
        assert "Unknown section" in result

    @pytest.mark.asyncio
    async def test_export_bibtex_with_retracted_flag(self, retraction_notice):
        shelf = _get_shelf()
        await shelf.track(CitationRecord(doi="10.1234/ok", title="OK"))
        await shelf.track(CitationRecord(
            doi="10.1234/bad",
            title="Bad",
            retraction=retraction_notice,
            authors=["X, Y"],
            year=2020,
        ))
        # Default: excludes retracted
        plain = await research_shelf("export", "bibtex")
        assert "OK" in plain
        assert "Bad" not in plain
        # With flag: includes retracted with note field
        with_ret = await research_shelf("export", "bibtex with_retracted")
        assert "OK" in with_ret
        assert "Bad" in with_ret
        assert "RETRACTED" in with_ret

    @pytest.mark.asyncio
    async def test_export_empty(self):
        result = await research_shelf("export", "bibtex")
        assert "empty" in result.lower()
