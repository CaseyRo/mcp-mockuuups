"""The upload store is what makes local images renderable at all, and it is
served on an unauthenticated route — so the guards are the test."""

import base64

import pytest
from fastmcp.exceptions import ToolError

from mcp_mockuuups import uploads
from mcp_mockuuups.config import settings

PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32
WEBP = b"RIFF" + b"0000" + b"WEBP" + b"0" * 16


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    uploads._store.clear()
    monkeypatch.setattr(settings, "public_base_url", "https://mock.example.test")
    yield
    uploads._store.clear()


def test_sniffs_by_content_not_by_claim():
    assert uploads.sniff(PNG) == ("image/png", "png")
    assert uploads.sniff(JPEG) == ("image/jpeg", "jpg")
    assert uploads.sniff(WEBP) == ("image/webp", "webp")


def test_non_image_is_refused():
    with pytest.raises(ToolError, match="not a PNG"):
        uploads.sniff(b"<?php system($_GET['c']); ?>")


def test_store_returns_public_url_and_round_trips():
    url = uploads.store(PNG)
    assert url.startswith("https://mock.example.test/i/")
    assert url.endswith(".png")
    token = url.rsplit("/", 1)[1].rsplit(".", 1)[0]
    assert uploads.fetch(token) == (PNG, "image/png")


def test_tokens_are_unguessable_and_unique():
    a = uploads.store(PNG).rsplit("/", 1)[1]
    b = uploads.store(PNG).rsplit("/", 1)[1]
    assert a != b
    assert len(a.split(".")[0]) >= 40


def test_expired_upload_is_gone(monkeypatch):
    monkeypatch.setattr(settings, "upload_ttl_seconds", -1)
    token = uploads.store(PNG).rsplit("/", 1)[1].split(".")[0]
    assert uploads.fetch(token) is None


def test_unknown_token_is_none():
    assert uploads.fetch("nope") is None


def test_oversize_image_refused(monkeypatch):
    monkeypatch.setattr(settings, "upload_max_bytes", 10)
    with pytest.raises(ToolError, match="limit is"):
        uploads.store(PNG)


def test_upload_requires_public_base_url(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "")
    with pytest.raises(ToolError, match="PUBLIC_BASE_URL"):
        uploads.store(PNG)


def test_stats_reports_live_uploads():
    uploads.store(PNG)
    assert uploads.stats()["staged"] == 1
