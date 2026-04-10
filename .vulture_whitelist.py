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
