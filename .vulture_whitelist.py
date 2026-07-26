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
