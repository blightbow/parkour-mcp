# Vulture whitelist — false positives only.
#
# Vulture parses this file and treats any referenced name as "used" during
# its dead-code scan. Only names that vulture genuinely cannot see through
# should live here; real findings stay exposed so 'just lint-deep' reports
# them.
#
# Not executable — running this file directly would raise NameError. It is
# loaded by vulture, which parses without importing.
#
# To regenerate the raw candidate list for triage:
#     uv run vulture parkour_mcp/ --make-whitelist
# Then copy only the false positives over.

# MCP resource handler registered via @mcp.resource("research://shelf").
# Vulture cannot see the decorator side-effect registration.
shelf_resource  # parkour_mcp/__init__.py:372

# markdownify MarkdownConverter subclass override, invoked via the base
# class's method dispatch when converting <img> elements.
_.convert_img  # parkour_mcp/markdown.py:14

# Test-only reset hook for the module-global _shelf singleton. Referenced
# by ~39 sites across the test suite (test_shelf.py, test_doi.py, etc.).
# Vulture is scanning parkour_mcp/ only, so it doesn't see the test usage.
_reset_shelf  # parkour_mcp/shelf.py:541

# FMEntries.tip_ledger class attribute — read by betamatter's
# build_frontmatter and FMEntries.set_tip via getattr(entries,
# "tip_ledger", ...). Vulture scans parkour_mcp/ only and cannot see
# the cross-package getattr consumption.
_.tip_ledger  # parkour_mcp/markdown.py#FMEntries

# MarkdownSection TypedDict field. Declaring a key in a TypedDict is not a
# use of it, and every consumer reads this one through the string literal
# .get("header_only") — in _build_section_list, _filter_markdown_by_sections,
# and _pipeline's header-only section note. Vulture resolves neither side.
header_only  # parkour_mcp/markdown.py#MarkdownSection

# Test-only reset hook for the module-global HF token and rate-limit state,
# called by the autouse _hf_state fixture in tests/conftest.py. Same shape as
# _reset_shelf above: vulture scans parkour_mcp/ only, so the test usage is
# invisible to it.
_reset_hf_state  # parkour_mcp/huggingface.py:166

# The remaining two test-only reset hooks, same shape as _reset_shelf and
# _reset_hf_state: _reset_repo_metadata_cache is called from test_github.py,
# _reset_cache from the autouse _stub_scorecard_for_github fixture in
# tests/conftest.py. Both invisible because vulture scans parkour_mcp/ only.
_reset_repo_metadata_cache  # parkour_mcp/github.py#_reset_repo_metadata_cache
_reset_cache  # parkour_mcp/scorecard.py#_reset_cache

# MCP resource handlers registered via @mcp.resource("kagi://...") inside
# main(). Identical decorator side-effect registration to shelf_resource
# above — nothing references the function names directly, by design.
kagi_regions_resource  # parkour_mcp/__init__.py#main
kagi_lenses_resource  # parkour_mcp/__init__.py#main

# Hermes plugin entrypoint. Declared in pyproject.toml under
# [project.entry-points."hermes_agent.plugins"] and called by the Hermes host
# at startup; vulture reads neither entry-point metadata nor the test suite,
# where tests/test_hermes_plugin.py drives it against a fake ctx.
register  # parkour_mcp/hermes_plugin.py#register

# Written so .text / .json() keep working after guarded_fetch's streaming
# context exits — it is httpx's own private cache attribute, read back inside
# httpx rather than here. Vulture sees the write with no local read.
_._content  # parkour_mcp/common.py#guarded_fetch

# htmd.Options fields. htmd is a compiled PyO3 extension, so these writes are
# consumed by Rust on the convert call and never read from Python. Vulture
# sees five assignments that nothing in this codebase reads back.
_.heading_style  # parkour_mcp/markdown.py#_build_htmd_options
_.skip_tags  # parkour_mcp/markdown.py#_build_htmd_options
_.image_placeholder  # parkour_mcp/markdown.py#_build_htmd_options
_.drop_empty_alt_images  # parkour_mcp/markdown.py#_build_htmd_options
_.drop_image_only_links  # parkour_mcp/markdown.py#_build_htmd_options

# FetchResponse mirrors the httpx response surface so the transport swap was
# invisible to callers; http_version is the one field only the tests read.
# They are the tests that matter: the Akamai and Cloudflare live guards both
# assert HTTP/2, which is the property the whole wreq migration turns on (two
# WAFs satisfied over one modern transport, rather than one placated by
# downgrading). Vulture does not scan tests, so it sees the write with no read.
http_version  # parkour_mcp/_transport.py#FetchResponse
