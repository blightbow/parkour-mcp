"""Tests for parkour_mcp/pdf.py: PDF decoding and the noise repairs."""

import re
from types import SimpleNamespace

import pytest

from parkour_mcp.detection import _detect_pdf_url
from parkour_mcp.pdf import (
    PdfDecodeError,
    _repair_markdown,
    _rotated_run_texts,
    pdf_to_markdown,
)
from tests._pdf_fixture import STAMP_TEXT, build_pdf


class TestPdfToMarkdown:
    def test_metadata_and_headings(self):
        doc = pdf_to_markdown(build_pdf(stamp=None))
        assert doc.title == "Minimal Test Document"
        assert doc.author == "Test Author"
        assert doc.page_count == 1
        assert "# Minimal Test Document" in doc.markdown
        # Heading levels are tiers of the document's font-size distribution,
        # so the tests pin the heading shape and not the depth.
        assert re.search(r"^#{1,6} 1 Introduction$", doc.markdown, re.MULTILINE)
        assert re.search(r"^#{1,6} 2 Method$", doc.markdown, re.MULTILINE)

    def test_body_text_is_prose_not_headings(self):
        doc = pdf_to_markdown(build_pdf(stamp=None))
        for line in doc.markdown.splitlines():
            if "Body line" in line:
                assert not line.startswith("#"), line

    def test_rotated_stamp_is_dropped(self):
        doc = pdf_to_markdown(build_pdf(stamp="rotated"))
        assert "arXiv:2101.00001v1" not in doc.markdown

    def test_flat_stamp_is_dropped_by_shape(self):
        doc = pdf_to_markdown(build_pdf(stamp="flat"))
        assert "arXiv:2101.00001v1" not in doc.markdown

    def test_citation_in_running_text_survives(self):
        doc = pdf_to_markdown(build_pdf(stamp="rotated"))
        assert "arXiv:1801.07736" in doc.markdown

    def test_title_falls_back_to_first_heading(self):
        # An empty /Title leaves the decoder with nothing; the first
        # heading is the next best name for the document.
        data = build_pdf(stamp=None, title="Fallback Heading Title").replace(
            b"/Title (Fallback Heading Title)", b"/Title ()"
        )
        doc = pdf_to_markdown(data)
        assert doc.title == "Fallback Heading Title"

    def test_garbage_raises_decode_error(self):
        with pytest.raises(PdfDecodeError):
            pdf_to_markdown(b"%PDF-1.4\nthis is not a pdf\n")


class TestRepairMarkdown:
    def test_overlong_heading_is_demoted_to_prose(self):
        long = ("Provided proper attribution is provided, Google hereby grants " * 3).strip()
        out = _repair_markdown(f"# Title\n\n#### {long}\n\nBody.\n", set())
        assert f"#### {long}" not in out
        assert long in out

    def test_equation_heading_is_demoted(self):
        md = "# Title\n\n##### MultiHead(Q,K,V) = Concat(head<sub>1</sub>)\n\nBody.\n"
        out = _repair_markdown(md, set())
        assert "##### MultiHead" not in out
        assert "MultiHead(Q,K,V) = Concat" in out

    def test_rotated_run_dropped_as_heading_and_as_prose(self):
        rotated = {"DRAFT COPY"}
        md = "# Title\n\n## DRAFT   COPY\n\nBody.\n\nDRAFT COPY\n\nMore.\n"
        out = _repair_markdown(md, rotated)
        assert "DRAFT" not in out
        assert "Body." in out and "More." in out

    def test_stamp_regex_matches_heading_and_plain_forms(self):
        md = f"# Title\n\n## {' '.join(STAMP_TEXT.split())}\n\n{' '.join(STAMP_TEXT.split())}\n\nBody.\n"
        out = _repair_markdown(md, set())
        assert "arXiv:2101.00001v1" not in out
        assert "Body." in out


class TestRotatedRunTexts:
    def test_runs_on_a_turned_page_are_kept(self):
        positioned = SimpleNamespace(
            page_rotations=[SimpleNamespace(page=2, rotation=90.0)],
            items=[
                SimpleNamespace(page=1, rotation=90.0, text="margin  stamp"),
                SimpleNamespace(page=2, rotation=90.0, text="landscape table cell"),
                SimpleNamespace(page=1, rotation=0.0, text="upright body"),
                SimpleNamespace(page=1, rotation=90.0, text="   "),
            ],
        )
        assert _rotated_run_texts(positioned) == {"margin stamp"}


class TestDetectPdfUrl:
    @pytest.mark.parametrize("url", [
        "https://example.com/paper.pdf",
        "https://example.com/dir/REPORT.PDF?download=1",
        "https://arxiv.org/pdf/2101.00001v1",
        "https://export.arxiv.org/pdf/2101.00001",
    ])
    def test_pdf_shaped(self, url):
        assert _detect_pdf_url(url)

    @pytest.mark.parametrize("url", [
        "https://example.com/paper.pdf.html",
        "https://arxiv.org/abs/2101.00001",
        "https://example.com/pdf/viewer",
        "https://example.com/page?file=x.pdf",
    ])
    def test_not_pdf_shaped(self, url):
        assert not _detect_pdf_url(url)
