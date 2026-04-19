# News fragments

This directory holds user-facing release-note fragments for the next
release. `towncrier build` consumes them at release time and prepends an
assembled section to `CHANGELOG.md` at the
`<!-- towncrier release notes start -->` marker.

## Fragment naming

Use the **orphan** form so rebases never conflict on filenames:

```
+<slug>.<type>.md
```

The leading `+` tells towncrier the fragment is not tied to an issue
number. The slug is free-form (short descriptive) and `<type>` is one of
the registered types below. Example:

```
+paginate-toc-by-slice.feature.md
+tantivy-parse-warnings.bugfix.md
+claude-desktop-manifest-bug.bugfix.md
```

## Types

| Type        | Section heading in CHANGELOG.md |
|-------------|---------------------------------|
| `feature`   | `### Added`                     |
| `changed`   | `### Changed`                   |
| `bugfix`    | `### Fixed`                     |
| `removal`   | `### Removed`                   |
| `security`  | `### Security`                  |
| `doc`       | `### Documentation`             |
| `misc`      | `### Miscellaneous`             |

## Content guidance

Write for the tool's *caller*, not the commit's author. The goal is the
same narrative quality we enforce via `Why:` commit trailers: the user
reads this to learn what changed from their perspective, not to audit
the diff.

One fragment per user-facing change. Multi-line is fine; towncrier wraps
at 79 columns by default. Don't include the `### Heading` yourself,
towncrier adds it based on the fragment type.

Good:

```
`web_fetch_sections` TOC is now paginated via a `slice` parameter
(#8). The previous 100-section cap silently hid entries on long
documents: RFC 9110 has 311 sections, so the TOC dump ran out at
§8.6 and callers had no way to discover §17 Security Considerations.
```

Not useful:

```
Added pagination to web_fetch_sections.
```

## Issue references

Use `#N` inline; towncrier rewrites these into full GitHub links via
`issue_format` in `pyproject.toml`. Named anchors like `(closes #8)` in
the commit message don't need to appear in the fragment, the fragment
is free-form prose.

## Preview

```
uv run towncrier build --draft --version NEXT
```

This renders what the next CHANGELOG.md entry would look like without
consuming fragments or touching any files.
