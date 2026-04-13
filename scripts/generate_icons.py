"""Generate tool/server icon SVGs from Noto fonts.

Icons are extracted as vector paths from Noto font families (all SIL OFL 1.1
licensed) and written as standalone SVG files to ``parkour_mcp/assets/icons/``.
The loader in ``parkour_mcp/__init__.py`` base64-encodes these at startup for
the MCP ``Icon`` spec, which requires ``https://`` or ``data:`` URIs.

Usage:
    just icons
    # or directly:
    uv run python3 scripts/generate_icons.py

``fonttools`` is declared in the ``dev`` dependency group; run ``uv sync`` to
install it if it's missing.

The script is idempotent — re-running it regenerates identical SVGs from the
same font + glyph inputs. To add a new tool icon, add a row to ``GLYPHS`` and
(if the font isn't already listed) a row to ``FONTS``.

Fonts are cached in ``scripts/fonts/`` (gitignored). First run downloads them;
subsequent runs reuse the cache.
"""

from __future__ import annotations

import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = REPO_ROOT / "scripts" / "fonts"
OUT_DIR = REPO_ROOT / "parkour_mcp" / "assets" / "icons"

# Font sources — filename → download URL. SIL OFL 1.1 licensed.
FONTS: dict[str, str] = {
    "NotoSansMono-Regular.ttf": (
        "https://cdn.jsdelivr.net/gh/notofonts/notofonts.github.io/fonts/"
        "NotoSansMono/unhinted/ttf/NotoSansMono-Regular.ttf"
    ),
    "NotoSansMath-Regular.ttf": (
        "https://cdn.jsdelivr.net/gh/notofonts/notofonts.github.io/fonts/"
        "NotoSansMath/unhinted/ttf/NotoSansMath-Regular.ttf"
    ),
    "NotoSansSymbols2-Regular.ttf": (
        "https://cdn.jsdelivr.net/gh/notofonts/notofonts.github.io/fonts/"
        "NotoSansSymbols2/unhinted/ttf/NotoSansSymbols2-Regular.ttf"
    ),
    # NotoEmoji (monochrome) is served from Google Fonts' gstatic CDN because
    # the notofonts.github.io tree only hosts the color variant for emoji.
    "NotoEmoji-Regular.ttf": (
        "https://fonts.gstatic.com/s/notoemoji/v47/"
        "bMrnmSyK7YY-MEu6aWjPDs-ar6uWaGWuob_10jwvS-FGJCMY.ttf"
    ),
}


@dataclass(frozen=True)
class Glyph:
    """A single glyph extraction job."""

    filename: str          # output filename stem (without .svg)
    codepoint: int         # Unicode codepoint
    font: str              # font filename (must be a key in FONTS)
    char: str              # literal char, for log/error readability
    description: str       # human-readable name


# Declarative glyph registry. Add rows here to introduce new icons.
# The loader in parkour_mcp/__init__.py references filenames via _ICON_FILES.
GLYPHS: list[Glyph] = [
    # Server-level icon
    Glyph("server",    0x222E, "NotoSansMath-Regular.ttf",     "∮", "CONTOUR INTEGRAL"),

    # Tool icons
    Glyph("search",    0x1F50D, "NotoSansSymbols2-Regular.ttf", "🔍", "MAGNIFYING GLASS"),
    Glyph("summarize", 0x03A3, "NotoSansMono-Regular.ttf",     "Σ", "GREEK CAPITAL SIGMA"),
    Glyph("sections",  0x00A7, "NotoSansMono-Regular.ttf",     "§", "SECTION SIGN"),
    Glyph("exact",     0x2316, "NotoSansSymbols2-Regular.ttf", "⌖", "POSITION INDICATOR"),
    Glyph("js",        0x26A1, "NotoSansSymbols2-Regular.ttf", "⚡", "HIGH VOLTAGE"),
    Glyph("arxiv",     0x03C7, "NotoSansMono-Regular.ttf",     "χ", "GREEK SMALL CHI"),
    Glyph("scholar",   0x2234, "NotoSansMono-Regular.ttf",     "∴", "THEREFORE"),
    Glyph("shelf",     0x229E, "NotoSansMath-Regular.ttf",     "⊞", "SQUARED PLUS"),
    Glyph("github",    0x2442, "NotoSansSymbols2-Regular.ttf", "⑂", "OCR FORK"),
    Glyph("ietf",      0x1F40C, "NotoEmoji-Regular.ttf",       "🐌", "SNAIL"),
    Glyph("packages",  0x2B21, "NotoSansMath-Regular.ttf",     "⬡", "WHITE HEXAGON"),
    Glyph("discourse", 0x1F4AC, "NotoEmoji-Regular.ttf",       "💬", "SPEECH BALLOON"),
    # "wiki" is Hawaiian for "quick" — Ward Cunningham named WikiWikiWeb
    # after the Wiki Wiki Shuttle at Honolulu airport. Shaka ("hang loose")
    # is a nod to those Hawaiian roots.
    Glyph("mediawiki", 0x1F919, "NotoEmoji-Regular.ttf",       "🤙", "CALL ME HAND (SHAKA)"),
]


def ensure_fonts() -> None:
    """Download missing fonts into FONTS_DIR; idempotent."""
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, url in FONTS.items():
        path = FONTS_DIR / filename
        if path.is_file() and path.stat().st_size > 1024:
            continue
        print(f"  downloading {filename} ...")
        try:
            urllib.request.urlretrieve(url, path)
        except Exception as exc:
            sys.exit(f"ERROR: failed to download {url}: {exc}")
        # Sanity check — a redirect page is a few hundred bytes of HTML.
        if path.stat().st_size < 1024:
            path.unlink()
            sys.exit(f"ERROR: {filename} download looks bogus (< 1 KiB)")


def extract_glyph(glyph: Glyph) -> str:
    """Return SVG markup for a glyph, extracted from its source font."""
    from fontTools.pens.boundsPen import BoundsPen
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.ttLib import TTFont

    font_path = FONTS_DIR / glyph.font
    font = TTFont(font_path)
    cmap = font.getBestCmap()
    if cmap is None:
        sys.exit(f"ERROR: no usable unicode cmap in {glyph.font}")
    if glyph.codepoint not in cmap:
        sys.exit(
            f"ERROR: U+{glyph.codepoint:04X} {glyph.char} ({glyph.description}) "
            f"not present in {glyph.font}"
        )

    name = cmap[glyph.codepoint]
    glyph_set = font.getGlyphSet()

    pen = SVGPathPen(glyph_set)
    glyph_set[name].draw(pen)
    path_data = pen.getCommands()

    bounds_pen = BoundsPen(glyph_set)
    glyph_set[name].draw(bounds_pen)
    if bounds_pen.bounds is None:
        sys.exit(f"ERROR: empty bounds for {glyph.description}")
    xmin, ymin, xmax, ymax = bounds_pen.bounds
    width = xmax - xmin
    height = ymax - ymin

    # 10% padding around the glyph bounds.
    pad = max(width, height) * 0.1
    vb_x = xmin - pad
    vb_y = ymin - pad
    vb_w = width + 2 * pad
    vb_h = height + 2 * pad

    # Font coords are Y-up; SVG is Y-down. Flip and re-translate so the path
    # renders right-side up inside the computed viewBox.
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{vb_x:.0f} {vb_y:.0f} {vb_w:.0f} {vb_h:.0f}" '
        f'width="48" height="48">\n'
        f'  <g transform="scale(1,-1) translate(0,{-(ymin + ymax):.0f})">\n'
        f'    <path d="{path_data}" fill="currentColor"/>\n'
        f"  </g>\n"
        f"</svg>\n"
    )


def main() -> None:
    print(f"Font cache: {FONTS_DIR}")
    print(f"Output:     {OUT_DIR}")
    print()

    print("Checking fonts ...")
    ensure_fonts()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Extracting glyphs ...")
    for glyph in GLYPHS:
        svg = extract_glyph(glyph)
        out_path = OUT_DIR / f"{glyph.filename}.svg"
        out_path.write_text(svg)
        print(
            f"  {glyph.filename:12s}  U+{glyph.codepoint:04X} {glyph.char}  "
            f"{glyph.description} ({glyph.font.split('-')[0]})"
        )

    print()
    print(f"Wrote {len(GLYPHS)} icons to {OUT_DIR.relative_to(REPO_ROOT)}/")


if __name__ == "__main__":
    main()
