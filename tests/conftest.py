import pytest

from mcp_mockuuups import catalog


def _mockup(mid, title, slug, family, kind="digital", tags=(), placements=1):
    place = {
        "id": f"p-{slug}",
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "family": family,
        "type": kind,
        "width": 100,
        "height": 200,
        "unit": "px",
    }
    return {
        "id": mid,
        "title": title,
        "thumbnail": f"https://example.test/{mid}.jpg",
        "width": 4000,
        "height": 3000,
        "placements": [place] * placements,
        "tags": [{"slug": t, "title": t} for t in tags],
    }


CATALOG = [
    _mockup(
        "m-ipad",
        "iPad Air mockup on a modern living room table",
        "ipad-air",
        "iPad",
        tags=("living-room", "shoot-a"),
    ),
    _mockup(
        "m-tv",
        "Television mockup in bright living room",
        "television",
        "TV",
        tags=("living-room", "shoot-a"),
    ),
    _mockup(
        "m-mbp",
        "MacBook Pro 14-inch mockup on a white table",
        "macbook-pro-14",
        "MacBook",
        tags=("shoot-a",),
    ),
    _mockup(
        "m-iphone",
        "iPhone 15 Pro mockup on a cozy sofa",
        "iphone-15-pro",
        "iPhone",
        tags=("sofa",),
    ),
    _mockup(
        "m-poster",
        "Poster mockup on a concrete wall",
        "a-format",
        "Paper",
        kind="print",
        tags=("wall",),
    ),
    _mockup(
        "m-duo", "Two-slot desk scene", "ipad-air", "iPad", tags=("desk",), placements=2
    ),
]


@pytest.fixture
def sample_catalog():
    rows = [dict(m) for m in CATALOG]
    for m in rows:
        m["_blob"] = catalog._blob(m)
    return rows
