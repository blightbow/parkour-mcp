"""Hand-assembled PDFs for the PDF decode tests.

A generated fixture rather than a committed paper: a few hundred bytes,
no redistribution question, and every feature the tests assert on is
placed deliberately.  The layout mirrors what pdf-inspector's heading
tiers react to: one large title, mid-sized numbered section headings,
and enough small body text that body is the dominant size.
"""

from __future__ import annotations

_BODY = [
    "Body line one of the introduction paragraph text.",
    "Body line two of the introduction paragraph text.",
    "See arXiv preprint arXiv:1801.07736 for the prior work.",
]
_BODY2 = [
    "Body line one of the method paragraph text.",
    "Body line two of the method paragraph text.",
    "Body line three of the method paragraph text.",
]

STAMP_TEXT = "arXiv:2101.00001v1  [cs.CL]  1 Jan 2021"


def _text(size: int, x: int, y: int, text: str) -> bytes:
    return f"BT /F1 {size} Tf {x} {y} Td ({text}) Tj ET\n".encode()


def build_pdf(*, stamp: str | None = "rotated", title: str = "Minimal Test Document") -> bytes:
    """Return a one-page PDF.

    *stamp* places the arXiv margin stamp: ``"rotated"`` sets it at 90
    degrees down the left margin as arXiv does, ``"flat"`` sets it as an
    ordinary horizontal line in a large face, and None omits it.
    """
    content = _text(24, 72, 720, title)
    content += _text(14, 72, 680, "1 Introduction")
    y = 660
    for line in _BODY:
        content += _text(10, 72, y, line)
        y -= 14
    content += _text(14, 72, y - 10, "2 Method")
    y -= 30
    for line in _BODY2:
        content += _text(10, 72, y, line)
        y -= 14
    if stamp == "rotated":
        content += f"BT /F1 20 Tf 0 1 -1 0 30 200 Tm ({STAMP_TEXT}) Tj ET\n".encode()
    elif stamp == "flat":
        content += _text(20, 72, y - 30, STAMP_TEXT)

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Title ({title}) /Author (Test Author) >>".encode(),
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (
        b"trailer\n<< /Size %d /Root 1 0 R /Info 6 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objs) + 1, xref)
    )
    return out
