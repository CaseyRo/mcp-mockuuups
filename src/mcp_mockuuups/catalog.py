"""In-process mockup catalog with local search.

The upstream /v1/mockups endpoint accepts no search or filter parameters —
`q`, `search`, `type`, `family` and `tag` are all silently ignored and every
request returns the same unfiltered first page. What it does honour is `limit`,
and `limit=6000` returns the entire catalog (5314 mockups, ~3.3MB) in a single
response. So the whole thing is fetched once, cached, and searched here.

ponytail: a dict + linear scan over ~5k rows. Measured well under 20ms, which
is nothing next to a 17s render. Reach for an index only if the catalog grows
an order of magnitude.
"""

from __future__ import annotations

import asyncio
import time

from . import client
from .config import settings

# Device words a caller is likely to use, mapped to the families Mockuuups
# actually names. Without this, "tablet" and "poster" match nothing.
_ALIASES: dict[str, set[str]] = {
    "phone": {"iPhone", "Google", "Samsung", "HTC"},
    "smartphone": {"iPhone", "Google", "Samsung", "HTC"},
    "mobile": {"iPhone", "Google", "Samsung", "HTC"},
    "android": {"Google", "Samsung", "HTC"},
    "pixel": {"Google"},
    "galaxy": {"Samsung"},
    "tablet": {"iPad", "Microsoft Surface"},
    "laptop": {"MacBook", "Microsoft Surface", "Dell", "Chromebook"},
    "notebook": {"MacBook", "Microsoft Surface", "Dell", "Chromebook"},
    "macbook": {"MacBook"},
    "desktop": {"iMac", "Apple Display", "Dell"},
    "computer": {"iMac", "Apple Display", "Dell", "MacBook"},
    "monitor": {"Apple Display", "Dell"},
    "display": {"Apple Display", "Dell"},
    "imac": {"iMac"},
    "watch": {"Apple Watch"},
    "smartwatch": {"Apple Watch"},
    "tv": {"TV"},
    "television": {"TV"},
    "poster": {"Paper"},
    "print": {"Paper", "Book"},
    "paper": {"Paper"},
    "flyer": {"Paper"},
    "card": {"Paper"},
    "book": {"Book"},
    "surface": {"Microsoft Surface"},
}

_cache: list[dict] = []
_fetched_at: float = 0.0
_lock = asyncio.Lock()


def _blob(m: dict) -> str:
    parts = [m.get("title", "")]
    for t in m.get("tags", []):
        parts.append(t.get("slug", ""))
        parts.append(t.get("title", ""))
    for p in m.get("placements", []):
        parts += [
            p.get("slug", ""),
            p.get("title", ""),
            p.get("family", ""),
            p.get("type", ""),
        ]
    return " ".join(parts).lower().replace("-", " ")


async def load() -> list[dict]:
    """Return the catalog, fetching it once per TTL."""
    global _cache, _fetched_at
    async with _lock:
        if _cache and (time.time() - _fetched_at) < settings.catalog_ttl_seconds:
            return _cache
        mockups = await client.fetch_catalog()
        for m in mockups:
            m["_blob"] = _blob(m)
        _cache = mockups
        _fetched_at = time.time()
        return _cache


def _families(m: dict) -> set[str]:
    return {p.get("family", "") for p in m.get("placements", [])}


def _types(m: dict) -> set[str]:
    return {p.get("type", "") for p in m.get("placements", [])}


def _score(m: dict, tokens: list[str], alias_families: set[str]) -> int:
    blob = m["_blob"]
    title = m.get("title", "").lower()
    score = 0
    for tok in tokens:
        if tok in title:
            score += 3
        elif tok in blob:
            score += 1
    if _families(m) & alias_families:
        score += 4
    return score


def search(
    catalog: list[dict],
    query: str = "",
    family: str | None = None,
    kind: str | None = None,
    tag: str | None = None,
    limit: int = 12,
) -> list[dict]:
    """Rank the catalog against a free-text query plus optional hard filters."""
    rows = catalog

    if family:
        want = family.strip().lower()
        rows = [m for m in rows if any(f.lower() == want for f in _families(m))]
    if kind:
        want = kind.strip().lower()
        rows = [m for m in rows if want in {t.lower() for t in _types(m)}]
    if tag:
        want = tag.strip().lower()
        rows = [
            m
            for m in rows
            if any(t.get("slug", "").lower() == want for t in m.get("tags", []))
        ]

    q = query.strip().lower().replace("-", " ")
    if not q:
        return rows[:limit]

    tokens = [t for t in q.split() if t]
    alias_families: set[str] = set()
    for tok in tokens:
        alias_families |= _ALIASES.get(tok, set())

    # An exact placement slug ("ipad-air") is an unambiguous intent — honour it
    # before any fuzzy ranking.
    slug = query.strip().lower()
    exact = [
        m
        for m in rows
        if any(p.get("slug", "") == slug for p in m.get("placements", []))
    ]
    if exact:
        return exact[:limit]

    scored = [(s, m) for m in rows if (s := _score(m, tokens, alias_families)) > 0]
    scored.sort(key=lambda pair: (-pair[0], pair[1].get("title", "")))
    return [m for _, m in scored[:limit]]


def facets(catalog: list[dict]) -> dict[str, list[str]]:
    """Available families and types, so a caller can filter without guessing."""
    fams: dict[str, int] = {}
    kinds: set[str] = set()
    for m in catalog:
        for f in _families(m):
            fams[f] = fams.get(f, 0) + 1
        kinds |= _types(m)
    return {
        "families": [f for f, _ in sorted(fams.items(), key=lambda kv: -kv[1])],
        "types": sorted(k for k in kinds if k),
    }
