"""Tests for scripts/check_doc_citations.py."""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "check_doc_citations.py"
_spec = importlib.util.spec_from_file_location("check_doc_citations", _SCRIPT)
assert _spec is not None and _spec.loader is not None
citations = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(citations)


_SOURCE = '''
CONSTANT = 1
annotated: int = 2


class Cache:
    size = 8

    def stats(self):
        pass


def main():
    async def handler():
        pass
'''


class TestIterCitations:
    def test_whole_span_citation_is_found(self):
        found = list(citations.iter_citations("See `common.py#guarded_fetch` here."))
        assert found == [(1, "common.py", "guarded_fetch")]

    def test_span_with_prose_around_the_citation_is_not_one(self):
        assert list(citations.iter_citations("`call common.py#x first`")) == []

    def test_fenced_blocks_are_skipped(self):
        text = "```\n`a.py#Fenced`\n```\n`b.py#Live`"
        assert list(citations.iter_citations(text)) == [(4, "b.py", "Live")]

    def test_other_extensions_are_not_citations(self):
        assert list(citations.iter_citations("`pyproject.toml#tool`")) == []


class TestPythonSymbolExists:
    @pytest.mark.parametrize(
        "fragment",
        ["CONSTANT", "annotated", "Cache", "main", "Cache.stats", "Cache.size", "handler"],
    )
    def test_resolves(self, fragment):
        assert citations.python_symbol_exists(_SOURCE, fragment)

    @pytest.mark.parametrize(
        "fragment",
        ["missing", "Cache.missing", "main.handler", "CONSTANT.real"],
    )
    def test_does_not_resolve(self, fragment):
        # A dotted path walks class bodies only; a nested function is
        # reachable by its bare name, not through the enclosing function.
        assert not citations.python_symbol_exists(_SOURCE, fragment)


class TestMarkdownSlugs:
    @pytest.mark.parametrize(
        ("heading", "slug"),
        [
            ("First-time setup", "first-time-setup"),
            ("`html_to_markdown` on megapages", "html_to_markdown-on-megapages"),
            ("Upgrade: 2.0 — breaking", "upgrade-20--breaking"),
            ("application/pdf support", "applicationpdf-support"),
        ],
    )
    def test_github_slug(self, heading, slug):
        assert citations.github_slug(heading) == slug

    def test_duplicate_headings_get_numbered_suffixes(self):
        text = "# Notes\n## Notes\n### Notes\n"
        assert citations.markdown_slugs(text) == {"notes", "notes-1", "notes-2"}

    def test_headings_in_fences_are_ignored(self):
        assert citations.markdown_slugs("```\n# not a heading\n```\n") == set()


class TestCheckCitation:
    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        package = tmp_path / "parkour_mcp"
        package.mkdir()
        (package / "common.py").write_text("def guarded_fetch():\n    pass\n")
        (tmp_path / "guide.md").write_text("# Getting started\n")
        monkeypatch.setattr(citations, "ROOT", tmp_path)
        monkeypatch.setattr(citations, "PACKAGE", package)
        return tmp_path

    def test_shorthand_path_resolves_under_the_package(self, repo):
        assert citations.check_citation("common.py", "guarded_fetch", repo / "x.md") is None

    def test_missing_symbol_is_reported_with_the_resolved_file(self, repo):
        reason = citations.check_citation("common.py", "gone", repo / "x.md")
        assert reason == "no symbol 'gone' in parkour_mcp/common.py"

    def test_heading_slug_is_checked(self, repo):
        assert citations.check_citation("guide.md", "getting-started", repo / "x.md") is None
        assert citations.check_citation("guide.md", "setup", repo / "x.md")

    def test_another_projects_path_is_skipped(self, repo):
        assert citations.check_citation("src/oauth.rs.py", "Oauth", repo / "x.md") is None
