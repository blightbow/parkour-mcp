"""Mount a recorded fixture as respx routes.

A fixture is the file ``parkour-mcp call ... --record FILE`` writes
(``parkour_mcp/_trace.py#write_fixture``): the responses one live call
received, with the wire-encoding headers removed.  Mounting it replays
those responses to the same URLs, so a live observation becomes an
offline test without transcribing XML or JSON by hand.

The generic path's test double issues through httpx, so a fixture recorded
from a wreq exchange replays through the same routes.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import respx

from parkour_mcp._trace import FIXTURE_VERSION


def mount_fixture(path: str | Path) -> list[respx.Route]:
    """Register one respx route per recorded exchange.  Call inside ``respx.mock``."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("version") != FIXTURE_VERSION:
        raise ValueError(
            f"{path}: fixture version {document.get('version')!r}, "
            f"expected {FIXTURE_VERSION}"
        )
    routes: list[respx.Route] = []
    for entry in document["exchanges"]:
        if "body_text" in entry:
            body = entry["body_text"].encode("utf-8")
        else:
            body = base64.b64decode(entry["body_base64"])
        route = respx.route(method=entry["method"], url=entry["url"]).mock(
            return_value=httpx.Response(
                entry["status"], headers=entry["headers"], content=body,
            )
        )
        routes.append(route)
    return routes
