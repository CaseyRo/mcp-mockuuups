"""Thin async client over the Mockuuups Studio REST API.

Upstream shape (reverse-engineered 2026-08-25 — the published API reference at
mockuuups.studio/api/docs/ sits behind a login wall):

    GET  /v1/account       plan, features, credit balance
    GET  /v1/mockups       the whole catalog; see catalog.py for why
    POST /v1/renders       {mockup, size, mode, destination, contents[]}
    GET  /v1/renders/{id}  poll an async render

Errors come back as {"error": {"code", "message", "detail"}}.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastmcp.exceptions import ToolError

from .config import settings

# The catalog is a single ~3.3MB response and renders can sit for a while;
# connect fast, read patiently.
_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)

_client: httpx.AsyncClient | None = None


def _api() -> httpx.AsyncClient:
    global _client
    if _client is None:
        key = settings.mockuuups_api_key.get_secret_value()
        if not key:
            raise ToolError(
                "MOCKUUUPS_API_KEY is not set. Get a developer key at "
                "https://mockuuups.studio/developers/ and set it in the environment."
            )
        _client = httpx.AsyncClient(
            base_url="https://api.mockuuups.studio",
            headers={"authorization": f"Bearer {key}"},
            timeout=_TIMEOUT,
        )
    return _client


def _explain(status: int, payload: Any) -> str:
    """Turn an upstream error body into something the caller can act on."""
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    code = err.get("code", "")
    detail = err.get("detail", "")
    message = err.get("message", "") or f"HTTP {status}"

    if code == "feature-not-available" and detail == "hires":
        return (
            f"Mockuuups rejected the render size: hi-res is not on this plan "
            f"(current cap MOCKUUUPS_MAX_SIZE={settings.mockuuups_max_size}px). "
            "Note the API defaults to hi-res when size is omitted, so size is "
            "always sent explicitly — this means the plan changed or the cap was raised."
        )
    if status == 402 or code == "no-credits":
        return (
            "Mockuuups is out of API credits. Check account_status(); the balance "
            "resets when the subscription renews."
        )
    if code == "mockup-not-found":
        return "No mockup with that id. Use search_mockups to get a valid id."
    if detail:
        return f"{message} ({detail})"
    return message


async def _request(method: str, path: str, **kw: Any) -> Any:
    try:
        resp = await _api().request(method, path, **kw)
    except httpx.TimeoutException as exc:
        raise ToolError(f"Mockuuups API timed out on {method} {path}.") from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"Could not reach the Mockuuups API: {exc}") from exc

    if resp.status_code >= 400:
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        raise ToolError(_explain(resp.status_code, payload))

    return resp.json()


async def get_account() -> dict:
    return await _request("GET", "/v1/account")


async def fetch_catalog() -> list[dict]:
    """Pull every mockup in one call. See catalog.py for the rationale."""
    data = await _request("GET", "/v1/mockups", params={"limit": 6000})
    return data.get("mockups", [])


async def create_render(
    mockup_id: str,
    contents: list[dict],
    size: int,
    destination: str = "cdn",
    mode: str = "async",
) -> dict:
    """Dispatch a render. Async mode returns in well under a second with the
    CDN URLs already allocated and state="pending"."""
    return await _request(
        "POST",
        "/v1/renders",
        json={
            "mockup": mockup_id,
            # Never omit: the API treats a missing size as hi-res, which hard-fails
            # on any plan without that feature.
            "size": size,
            "mode": mode,
            "destination": destination,
            "contents": contents,
        },
    )


async def get_render(render_id: str) -> dict:
    return await _request("GET", f"/v1/renders/{render_id}")
