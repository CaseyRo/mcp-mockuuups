"""Short-lived public hosting for images a caller uploads.

Mockuuups renders from a URL only: contents[].url is mandatory and there is no
upload endpoint, no base64 field and no data-URI support on any plan we can see.
So to render an image the caller holds locally, that image has to be fetchable
from the public internet for the length of one render.

This keeps it in memory under an unguessable token and serves it from a single
public route on this server, which is already publicly reachable through the
Cloudflare tunnel. No bucket, no volume, no second service.

Trust boundary: that route CANNOT be authenticated, because the thing fetching
it is Mockuuups' renderer, not our caller. What stands in for auth is a 256-bit
token, a short TTL, a size cap and an image-only content check. Nothing
sensitive should be rendered through a mockup service anyway.
"""

from __future__ import annotations

import secrets
import time

from fastmcp.exceptions import ToolError

from .config import settings

# Total across all live uploads. Bounds the blast radius of a caller looping
# on upload without ever rendering.
_MAX_TOTAL_BYTES = 64 * 1024 * 1024

# token -> (data, content_type, expires_at)
_store: dict[str, tuple[bytes, str, float]] = {}

_MAGIC: list[tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
]


def sniff(data: bytes) -> tuple[str, str]:
    """Identify the image by magic bytes. Returns (content_type, extension).

    The declared type is never trusted — this is what gets served back out on a
    public route, so it is decided by content, not by what the caller claimed.
    """
    for magic, ctype, ext in _MAGIC:
        if data.startswith(magic):
            return ctype, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise ToolError(
        "Uploaded data is not a PNG, JPEG, GIF or WebP image. "
        "Pass raw image bytes, base64-encoded."
    )


def _purge(now: float) -> None:
    for token in [t for t, (_, _, exp) in _store.items() if exp <= now]:
        _store.pop(token, None)


def store(data: bytes) -> str:
    """Hold an image and return the public URL Mockuuups should fetch."""
    if not settings.public_base_url:
        raise ToolError(
            "PUBLIC_BASE_URL is not set, so uploaded images cannot be given a "
            "URL for Mockuuups to fetch. Set it to this server's public origin, "
            "or pass image_url / screenshot_url instead."
        )
    if len(data) > settings.upload_max_bytes:
        raise ToolError(
            f"Image is {len(data) // 1024}KB; the limit is "
            f"{settings.upload_max_bytes // 1024}KB."
        )

    now = time.time()
    _purge(now)

    if sum(len(d) for d, _, _ in _store.values()) + len(data) > _MAX_TOTAL_BYTES:
        raise ToolError(
            "Too many images are already staged for rendering. Retry in a few minutes."
        )

    ctype, ext = sniff(data)
    token = secrets.token_urlsafe(32)
    _store[token] = (data, ctype, now + settings.upload_ttl_seconds)
    return f"{settings.public_base_url.rstrip('/')}/i/{token}.{ext}"


def fetch(token: str) -> tuple[bytes, str] | None:
    """Resolve a token for the public route. None when unknown or expired."""
    now = time.time()
    _purge(now)
    entry = _store.get(token)
    if entry is None:
        return None
    data, ctype, _ = entry
    return data, ctype


def stats() -> dict:
    _purge(time.time())
    return {
        "staged": len(_store),
        "bytes": sum(len(d) for d, _, _ in _store.values()),
    }
