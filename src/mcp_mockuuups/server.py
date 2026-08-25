"""MCP server for Mockuuups Studio.

Four tools: find a mockup, render into it, poll a slow render, check credits.

Two upstream facts drive the whole design:

* /v1/mockups ignores every search parameter, so the catalog is pulled whole
  and searched locally (catalog.py).
* A screenshot render takes ~17s synchronously, and clients reach this server
  through a Cloudflare MCP portal with a hard ~60s upstream cap. Renders are
  therefore dispatched with mode="async" (sub-second, CDN URLs allocated up
  front) and polled concurrently inside a bounded budget, so a set of devices
  costs about as much wall-clock as the slowest one.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import catalog, client, uploads
from .auth import BearerTokenVerifier
from .config import settings

try:
    __version__ = version("mcp-mockuuups")
except PackageNotFoundError:  # running from a source tree
    __version__ = "dev"

_start_time = datetime.now(UTC)

_auth = None
if settings.mcp_api_key.get_secret_value():
    _auth = BearerTokenVerifier(settings.mcp_api_key.get_secret_value())

mcp = FastMCP("mcp-mockuuups", auth=_auth)

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}


# -- envelopes ----------------------------------------------------------------


class Placement(BaseModel):
    slug: str
    title: str
    family: str
    type: str
    width: int = 0
    height: int = 0
    unit: str = "px"


class MockupSummary(BaseModel):
    id: str
    title: str
    thumbnail: str = ""
    width: int = 0
    height: int = 0
    placements: list[Placement] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class MockupSearchResult(BaseModel):
    summary: str
    count: int
    catalog_size: int
    mockups: list[MockupSummary]
    families: list[str] = Field(default_factory=list)
    types: list[str] = Field(default_factory=list)


class RenderResult(BaseModel):
    mockup_id: str
    render_id: str
    state: str  # pending | success | <upstream failure state>
    cost: int = 0
    page_url: str = ""
    download_url: str = ""
    thumbnail_url: str = ""
    expires_at: str = ""
    error: str = ""


class RenderBatchResult(BaseModel):
    summary: str
    requested: int
    succeeded: int
    pending: int
    failed: int
    credits_spent: int
    renders: list[RenderResult]


class AccountResult(BaseModel):
    summary: str
    account: str
    plan: str
    status: str
    credits_used: int
    credits_left: int
    features: list[str]
    max_render_size: int
    hi_res_available: bool
    screenshots_available: bool
    cdn_links_expire: bool
    uploads_configured: bool


# -- helpers ------------------------------------------------------------------


def _to_summary(m: dict) -> MockupSummary:
    return MockupSummary(
        id=m.get("id", ""),
        title=m.get("title", ""),
        thumbnail=m.get("thumbnail", ""),
        width=int(m.get("width") or 0),
        height=int(m.get("height") or 0),
        placements=[
            Placement(
                slug=p.get("slug", ""),
                title=p.get("title", ""),
                family=p.get("family", ""),
                type=p.get("type", ""),
                width=int(p.get("width") or 0),
                height=int(p.get("height") or 0),
                unit=p.get("unit", "px"),
            )
            for p in m.get("placements", [])
        ],
        tags=[t.get("slug", "") for t in m.get("tags", [])],
    )


def _decode_image(image_base64: str) -> bytes:
    """Decode a base64 image, tolerating a data: URI wrapper."""
    payload = image_base64.strip()
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolError(f"image_base64 is not valid base64: {exc}") from exc


def _resolve_content(
    screenshot_url: str | None,
    image_url: str | None,
    image_base64: str | None,
) -> dict:
    """Turn whichever source the caller gave into one Mockuuups content object."""
    given = [
        name
        for name, value in (
            ("screenshot_url", screenshot_url),
            ("image_url", image_url),
            ("image_base64", image_base64),
        )
        if value
    ]
    if len(given) != 1:
        raise ToolError(
            "Provide exactly one of screenshot_url, image_url or image_base64 "
            f"(got {given or 'none'})."
        )

    if screenshot_url:
        return {"type": "screenshot", "url": screenshot_url}
    if image_url:
        return {"type": "image", "url": image_url}
    return {"type": "image", "url": uploads.store(_decode_image(image_base64 or ""))}


def _render_from_payload(mockup_id: str, payload: dict) -> RenderResult:
    cdn = payload.get("cdn") or {}
    state = str(payload.get("state") or "unknown")
    return RenderResult(
        mockup_id=mockup_id,
        render_id=str(payload.get("id") or ""),
        state=state,
        cost=int(payload.get("cost") or 0),
        page_url=cdn.get("page", ""),
        download_url=cdn.get("download", ""),
        thumbnail_url=cdn.get("thumbnail", ""),
        expires_at=cdn.get("expiration", ""),
        error="" if state in ("pending", "success") else f"render state: {state}",
    )


async def _poll_until(results: list[RenderResult], deadline: float) -> None:
    """Poll pending renders in place until they settle or the budget runs out."""
    while time.monotonic() < deadline:
        pending = [r for r in results if r.state == "pending" and r.render_id]
        if not pending:
            return
        await asyncio.sleep(2)
        fetched = await asyncio.gather(
            *(client.get_render(r.render_id) for r in pending),
            return_exceptions=True,
        )
        for result, payload in zip(pending, fetched):
            if isinstance(payload, BaseException):
                continue
            updated = _render_from_payload(result.mockup_id, payload)
            result.state = updated.state
            result.cost = updated.cost or result.cost
            result.error = updated.error


def _batch(results: list[RenderResult], note: str = "") -> RenderBatchResult:
    succeeded = sum(1 for r in results if r.state == "success")
    pending = sum(1 for r in results if r.state == "pending")
    failed = len(results) - succeeded - pending
    bits = [f"{succeeded}/{len(results)} rendered"]
    if pending:
        bits.append(
            f"{pending} still rendering — poll get_renders with their render_id"
        )
    if failed:
        bits.append(f"{failed} failed")
    if note:
        bits.append(note)
    return RenderBatchResult(
        summary=". ".join(bits) + ".",
        requested=len(results),
        succeeded=succeeded,
        pending=pending,
        failed=failed,
        credits_spent=sum(r.cost for r in results),
        renders=results,
    )


# -- tools --------------------------------------------------------------------


@mcp.tool(
    tags={"mockups"},
    annotations=ToolAnnotations(title="Search mockups", **_READ_ONLY),
)
async def search_mockups(
    query: str = "",
    family: str | None = None,
    kind: Literal["digital", "print"] | None = None,
    tag: str | None = None,
    limit: int = 12,
) -> MockupSearchResult:
    """[mockuuups] Which mockup should I use? Searches all ~5300 Mockuuups
    scenes by device, scene and style.

    `query` is free text and understands everyday device words — "tablet",
    "laptop", "poster", "smartwatch" — as well as exact placement slugs like
    "ipad-air". Combine it with `family` (iPhone, iPad, MacBook, TV, Paper,
    Apple Watch, Samsung, Google, iMac, ...) or `kind` to narrow.

    `tag` is the strongest way to get one consistent look across several
    devices: scenes shot together share a tag, so filtering by a tag returned
    on a mockup you like gives you the rest of that shoot. Pass the returned
    `id` to create_mockups.
    """
    rows = await catalog.load()
    matches = catalog.search(
        rows, query=query, family=family, kind=kind, tag=tag, limit=limit
    )
    if matches:
        # Facets only earn their place when there is nothing to show; alongside
        # results they are just noise the caller has to read past.
        return MockupSearchResult(
            summary=f"{len(matches)} of {len(rows)} mockups match.",
            count=len(matches),
            catalog_size=len(rows),
            mockups=[_to_summary(m) for m in matches],
        )

    face = catalog.facets(rows)
    return MockupSearchResult(
        summary=(
            f"Nothing matched in {len(rows)} mockups. "
            f"Available families: {', '.join(face['families'])}."
        ),
        count=0,
        catalog_size=len(rows),
        mockups=[],
        families=face["families"],
        types=face["types"],
    )


@mcp.tool(tags={"mockups"}, annotations=ToolAnnotations(title="Create mockups"))
async def create_mockups(
    mockup_ids: list[str],
    screenshot_url: str | None = None,
    image_url: str | None = None,
    image_base64: str | None = None,
    size: int | None = None,
    wait_seconds: int | None = None,
) -> RenderBatchResult:
    """[mockuuups] Put one design into one or more mockups and render them.

    Give exactly one source:
    * `screenshot_url` — Mockuuups screenshots the live page itself. Best for
      websites; costs one extra credit per render.
    * `image_url` — any publicly reachable image.
    * `image_base64` — raw image bytes for a design that only exists locally.
      Mockuuups can only render from a URL, so the image is staged on this
      server under a short-lived unguessable link for the render to fetch.

    Pass several `mockup_ids` to render the same design across devices in one
    call; they run concurrently. Renders that outrun the wait budget come back
    as `pending` with a `render_id` for get_renders — the CDN links are already
    valid and will fill in once the render lands.

    Each render costs a credit, +1 for a screenshot, so check account_status
    before a large batch.
    """
    if not mockup_ids:
        raise ToolError("Pass at least one mockup_id (get them from search_mockups).")

    max_size = settings.mockuuups_max_size
    render_size = size or max_size
    if render_size > max_size:
        raise ToolError(
            f"size={render_size} exceeds the configured cap of {max_size}px. "
            "Hi-res renders need a paid Mockuuups plan; raise MOCKUUUPS_MAX_SIZE "
            "once the account has the hires feature."
        )

    content = _resolve_content(screenshot_url, image_url, image_base64)

    # Every placement in a mockup needs its own content entry, in order. All but
    # a couple of the ~5300 mockups have exactly one.
    # ponytail: a multi-placement mockup gets the same design in every slot;
    # add a per-placement contents argument if a real use case turns up.
    rows = await catalog.load()
    placements = {m.get("id"): len(m.get("placements", []) or [1]) for m in rows}

    dispatched = await asyncio.gather(
        *(
            client.create_render(
                mockup_id=mid,
                contents=[content] * max(placements.get(mid, 1), 1),
                size=render_size,
            )
            for mid in mockup_ids
        ),
        return_exceptions=True,
    )

    results: list[RenderResult] = []
    for mid, payload in zip(mockup_ids, dispatched):
        if isinstance(payload, BaseException):
            results.append(
                RenderResult(
                    mockup_id=mid, render_id="", state="failed", error=str(payload)
                )
            )
        else:
            results.append(_render_from_payload(mid, payload))

    budget = wait_seconds if wait_seconds is not None else settings.render_wait_seconds
    budget = max(0, min(budget, settings.render_wait_seconds))
    if budget:
        await _poll_until(results, time.monotonic() + budget)

    note = ""
    if any(r.expires_at for r in results):
        note = "CDN links expire — download anything worth keeping"
    return _batch(results, note)


@mcp.tool(
    tags={"mockups"},
    annotations=ToolAnnotations(title="Get renders", **_READ_ONLY),
)
async def get_renders(
    render_ids: list[str], wait_seconds: int = 0
) -> RenderBatchResult:
    """[mockuuups] Did those renders finish? Poll renders create_mockups
    returned as pending. With `wait_seconds` it long-polls until they settle or
    the budget runs out; with 0 it checks once and returns immediately."""
    if not render_ids:
        raise ToolError("Pass at least one render_id.")

    fetched = await asyncio.gather(
        *(client.get_render(rid) for rid in render_ids), return_exceptions=True
    )
    results: list[RenderResult] = []
    for rid, payload in zip(render_ids, fetched):
        if isinstance(payload, BaseException):
            results.append(
                RenderResult(
                    mockup_id="", render_id=rid, state="failed", error=str(payload)
                )
            )
        else:
            results.append(_render_from_payload("", payload))

    budget = max(0, min(wait_seconds, settings.render_wait_seconds))
    if budget:
        await _poll_until(results, time.monotonic() + budget)
    return _batch(results)


@mcp.tool(
    tags={"mockups"},
    annotations=ToolAnnotations(title="Account status", **_READ_ONLY),
)
async def account_status() -> AccountResult:
    """[mockuuups] How many credits are left, and what can this plan do?
    Reports the credit balance plus which features are actually available —
    hi-res, website screenshots, and whether CDN links expire. Worth checking
    before a batch: a plain render costs 1 credit and a screenshot costs 2."""
    data = await client.get_account()
    account = data.get("account") or {}
    usage = data.get("usage") or {}
    sub = data.get("subscription") or {}
    features = list(sub.get("features") or [])

    hires = "hires" in features
    used = int(usage.get("creditsUsed") or 0)
    left = int(usage.get("creditsLeft") or 0)
    plan = str(sub.get("plan") or "unknown")

    caveats = []
    if not hires:
        caveats.append(f"no hi-res (capped at {settings.mockuuups_max_size}px)")
    if "cdn-temporary" in features:
        caveats.append("CDN links expire after ~24h")
    if not settings.public_base_url:
        caveats.append("PUBLIC_BASE_URL unset, so image_base64 uploads are unavailable")

    summary = f"{plan} plan: {left} of {used + left} credits left."
    if caveats:
        summary += " " + "; ".join(caveats) + "."

    return AccountResult(
        summary=summary,
        account=str(account.get("name") or ""),
        plan=plan,
        status=str(sub.get("status") or ""),
        credits_used=used,
        credits_left=left,
        features=features,
        max_render_size=settings.mockuuups_max_size,
        hi_res_available=hires,
        screenshots_available="screenshot" in features,
        cdn_links_expire="cdn-temporary" in features,
        uploads_configured=bool(settings.public_base_url),
    )


# -- routes -------------------------------------------------------------------


@mcp.custom_route("/i/{filename}", methods=["GET"])
async def serve_upload(request: Request) -> Response:
    """Serve a staged upload so Mockuuups' renderer can fetch it.

    Deliberately unauthenticated — the fetcher is Mockuuups, not our caller.
    See uploads.py for what stands in for auth.
    """
    filename = request.path_params["filename"]
    token = filename.rsplit(".", 1)[0]
    found = uploads.fetch(token)
    if found is None:
        return Response(status_code=404)
    data, ctype = found
    return Response(
        content=data,
        media_type=ctype,
        headers={
            "cache-control": "private, max-age=60",
            "x-robots-tag": "noindex, nofollow",
        },
    )


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Public health endpoint. Does not call Mockuuups — process liveness only."""
    return JSONResponse(
        {
            "status": "healthy",
            "service": "mcp-mockuuups",
            "version": __version__,
            "uptime_seconds": int((datetime.now(UTC) - _start_time).total_seconds()),
            "uploads": uploads.stats(),
        }
    )


@mcp.custom_route("/healthz", methods=["GET"])
async def health_check_z(request: Request) -> JSONResponse:
    return await health_check(request)


def main() -> None:
    """Entry point for the mcp-mockuuups server."""
    if not settings.mockuuups_api_key.get_secret_value():
        sys.exit("Set MOCKUUUPS_API_KEY first (see .env.example).")
    if settings.transport == "http":
        mcp.run(
            transport="streamable-http",
            host=settings.host,
            port=settings.port,
            stateless_http=True,
            # fastmcp >=3.4.3 rejects non-localhost Host headers with 421 unless
            # allowed_hosts is set (the edge is the tunnel).
            allowed_hosts=["*"],
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
