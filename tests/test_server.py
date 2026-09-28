"""Tool behaviour through FastMCP's in-memory client."""

import base64
from types import SimpleNamespace

import pytest
from fastmcp import Client

from mcp_mockuuups import catalog, server, uploads
from mcp_mockuuups import client as api
from mcp_mockuuups.config import settings

from .conftest import CATALOG

PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _cdn(rid):
    return {
        "id": rid,
        "state": "pending",
        "cost": 2,
        "cdn": {
            "page": f"https://mockup.delivery/{rid}",
            "download": f"https://mockup.delivery/d/{rid}",
            "thumbnail": f"https://mockup.delivery/t/{rid}",
            "expiration": "2026-08-26T18:00:00.000Z",
        },
    }


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    catalog._cache = []
    catalog._fetched_at = 0.0
    uploads._store.clear()
    monkeypatch.setattr(settings, "public_base_url", "https://mock.example.test")
    monkeypatch.setattr(settings, "mockuuups_max_size", 1000)
    monkeypatch.setattr(settings, "render_wait_seconds", 0)

    async def fake_catalog():
        return [dict(m) for m in CATALOG]

    monkeypatch.setattr(api, "fetch_catalog", fake_catalog)
    yield
    catalog._cache = []


@pytest.fixture
def dispatched(monkeypatch):
    """Record every render dispatched, and report them as finished."""
    calls = []

    async def fake_create(mockup_id, contents, size, destination="cdn", mode="async"):
        calls.append(
            {"mockup": mockup_id, "contents": contents, "size": size, "mode": mode}
        )
        return _cdn(f"r-{mockup_id}")

    async def fake_get(render_id):
        return {**_cdn(render_id), "state": "success"}

    monkeypatch.setattr(api, "create_render", fake_create)
    monkeypatch.setattr(api, "get_render", fake_get)
    return calls


async def _call(name, args):
    async with Client(server.mcp) as c:
        return (await c.call_tool(name, args)).structured_content


# -- protocol surface ---------------------------------------------------------
# Guards the fastmcp upgrade: server imports, tools register, hints reach the wire.

EXPECTED_TOOLS = {"search_mockups", "create_mockups", "get_renders", "account_status"}


async def test_server_registers_its_tools():
    async with Client(server.mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert EXPECTED_TOOLS <= names, f"missing: {EXPECTED_TOOLS - names}"


async def test_read_only_tools_are_annotated_over_the_wire():
    async with Client(server.mcp) as c:
        tools = {t.name: t for t in await c.list_tools()}
    ann = tools["search_mockups"].annotations
    assert ann is not None
    assert ann.read_only_hint is True
    assert ann.destructive_hint is False
    # create_mockups spends credits: it must never advertise itself as read-only
    create = tools["create_mockups"].annotations
    assert not (create.read_only_hint or False)
    assert create.destructive_hint is False
    assert create.idempotent_hint is False
    assert create.open_world_hint is True


# -- search -------------------------------------------------------------------


async def test_search_finds_by_device_word():
    out = await _call("search_mockups", {"query": "tablet"})
    assert out["count"] >= 1
    assert out["catalog_size"] == len(CATALOG)


async def test_search_miss_reports_available_families():
    out = await _call("search_mockups", {"query": "zzzznotathing"})
    assert out["count"] == 0
    assert "iPad" in out["families"]


async def test_search_tag_filter_returns_the_shoot():
    out = await _call("search_mockups", {"tag": "shoot-a"})
    assert {m["id"] for m in out["mockups"]} == {"m-ipad", "m-tv", "m-mbp"}


# -- create -------------------------------------------------------------------


async def test_screenshot_render_across_devices(dispatched):
    out = await _call(
        "create_mockups",
        {
            "mockup_ids": ["m-ipad", "m-tv"],
            "screenshot_url": "https://example.test",
            "wait_seconds": 0,
        },
    )
    assert out["requested"] == 2
    assert len(dispatched) == 2
    assert all(c["contents"][0]["type"] == "screenshot" for c in dispatched)
    # size is never left to the API, which would default it to hi-res
    assert all(c["size"] == 1000 for c in dispatched)
    assert all(c["mode"] == "async" for c in dispatched)
    assert out["renders"][0]["page_url"].startswith("https://mockup.delivery/")


async def test_base64_image_is_staged_and_passed_as_url(dispatched):
    await _call(
        "create_mockups",
        {"mockup_ids": ["m-ipad"], "image_base64": base64.b64encode(PNG).decode()},
    )
    url = dispatched[0]["contents"][0]["url"]
    assert url.startswith("https://mock.example.test/i/")
    token = url.rsplit("/", 1)[1].split(".")[0]
    assert uploads.fetch(token) == (PNG, "image/png")


async def test_data_uri_prefix_is_tolerated(dispatched):
    payload = "data:image/png;base64," + base64.b64encode(PNG).decode()
    await _call("create_mockups", {"mockup_ids": ["m-ipad"], "image_base64": payload})
    assert dispatched[0]["contents"][0]["url"].endswith(".png")


async def test_multi_placement_mockup_gets_one_content_per_slot(dispatched):
    await _call(
        "create_mockups",
        {"mockup_ids": ["m-duo"], "image_url": "https://example.test/a.png"},
    )
    assert len(dispatched[0]["contents"]) == 2


async def test_exactly_one_source_required(dispatched):
    with pytest.raises(Exception, match="exactly one"):
        await _call("create_mockups", {"mockup_ids": ["m-ipad"]})
    with pytest.raises(Exception, match="exactly one"):
        await _call(
            "create_mockups",
            {
                "mockup_ids": ["m-ipad"],
                "image_url": "https://a.test/x.png",
                "screenshot_url": "https://b.test",
            },
        )


async def test_size_above_plan_cap_is_refused_before_spending_credits(dispatched):
    with pytest.raises(Exception, match="exceeds the configured cap"):
        await _call(
            "create_mockups",
            {
                "mockup_ids": ["m-ipad"],
                "image_url": "https://a.test/x.png",
                "size": 4000,
            },
        )
    assert dispatched == []


async def test_empty_mockup_ids_refused(dispatched):
    with pytest.raises(Exception, match="at least one mockup_id"):
        await _call(
            "create_mockups", {"mockup_ids": [], "screenshot_url": "https://a.test"}
        )


async def test_one_failed_render_does_not_sink_the_batch(monkeypatch):
    async def flaky(mockup_id, contents, size, destination="cdn", mode="async"):
        if mockup_id == "m-tv":
            raise RuntimeError("upstream exploded")
        return {**_cdn("r-ok"), "state": "success"}

    monkeypatch.setattr(api, "create_render", flaky)
    out = await _call(
        "create_mockups",
        {"mockup_ids": ["m-ipad", "m-tv"], "screenshot_url": "https://example.test"},
    )
    assert out["succeeded"] == 1 and out["failed"] == 1
    assert "upstream exploded" in out["renders"][1]["error"]


async def test_all_renders_failing_raises(monkeypatch):
    async def broken(mockup_id, contents, size, destination="cdn", mode="async"):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(api, "create_render", broken)
    with pytest.raises(Exception, match="Every render failed.*upstream exploded"):
        await _call(
            "create_mockups",
            {"mockup_ids": ["m-ipad"], "screenshot_url": "https://example.test"},
        )


# -- poll + account -----------------------------------------------------------


async def test_get_renders_reports_state(dispatched):
    out = await _call("get_renders", {"render_ids": ["r-1"]})
    assert out["succeeded"] == 1


async def test_account_status_surfaces_plan_limits(monkeypatch):
    async def fake_account():
        return {
            "account": {"name": "Casey does IT"},
            "usage": {"creditsUsed": 6, "creditsLeft": 44},
            "subscription": {
                "status": "Trial",
                "plan": "Trial",
                "features": ["screenshot", "cdn-temporary"],
            },
        }

    monkeypatch.setattr(api, "get_account", fake_account)
    out = await _call("account_status", {})
    assert out["credits_left"] == 44
    assert out["hi_res_available"] is False
    assert out["cdn_links_expire"] is True
    assert "no hi-res" in out["summary"]


# -- routes -------------------------------------------------------------------


async def test_upload_route_serves_then_404s_on_unknown():
    url = uploads.store(PNG)
    token = url.rsplit("/", 1)[1]
    resp = await server.serve_upload(SimpleNamespace(path_params={"filename": token}))
    assert resp.status_code == 200
    assert resp.media_type == "image/png"
    assert resp.body == PNG

    missing = await server.serve_upload(
        SimpleNamespace(path_params={"filename": "bogus.png"})
    )
    assert missing.status_code == 404


async def test_health_does_not_call_upstream():
    resp = await server.health_check(SimpleNamespace())
    assert resp.status_code == 200
    assert b"healthy" in resp.body


async def test_tool_call_writes_one_usage_line(capsys):
    await _call("search_mockups", {"query": "tablet"})
    lines = [ln for ln in capsys.readouterr().err.splitlines() if '"mcp_usage"' in ln]
    assert len(lines) == 1
    assert '"server": "mockuuups"' in lines[0]
    assert '"tool": "search_mockups"' in lines[0]
    assert '"outcome": "ok"' in lines[0]
