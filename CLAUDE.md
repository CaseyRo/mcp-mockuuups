# CLAUDE.md

MCP server wrapping the Mockuuups Studio REST API. Part of the CDIT MCP fleet —
fleet conventions live in the `cdit` OpenSpec store; this file covers what is
specific to this repo.

## The upstream API, as it actually behaves

The published reference at `mockuuups.studio/api/docs/` is behind a login wall.
What follows was verified against the live API on 2026-08-25 and is the reason
several design choices look the way they do.

```
GET  /v1/account       plan, features, credit balance
GET  /v1/mockups       the catalog
GET  /v1/mockups/{id}  one mockup
POST /v1/renders       {mockup, size, mode, destination, contents[]}
GET  /v1/renders/{id}  poll an async render
```

Four behaviours that are load-bearing here:

1. **The catalog endpoint has no search.** `q`, `search`, `type`, `family` and
   `tag` are accepted and silently ignored — every call returns the same
   unfiltered page. `limit` does work, and `limit=6000` returns all 5314
   mockups (~3.3MB) in one response. Hence `catalog.py`: fetch once, cache,
   search locally.
2. **Omitting `size` means hi-res**, which fails the whole render with
   `feature-not-available` on any plan lacking that feature. `client.py` always
   sends `size` explicitly. Never make it optional upstream.
3. **`contents[].url` is mandatory.** There is no upload endpoint, no base64
   field, no data-URI support. Rendering a locally-held image *requires*
   hosting it somewhere Mockuuups can fetch — that is what `uploads.py` is for.
4. **`contents` must have one entry per placement, in order.** All but two of
   the 5314 mockups have exactly one placement.

Errors come back as `{"error": {"code", "message", "detail"}}`; `client._explain`
turns the ones that matter into actionable messages.

## Why renders are async

A synchronous screenshot render measured **17.3s**. Clients reach this server
through a Cloudflare MCP portal with a hard **~60s** upstream read timeout that
we cannot change, so a handful of sequential sync renders gets severed
mid-flight (the same trap `mcp-bildsprache` documents in its config).

`mode="async"` returns in ~0.6s *with the CDN URLs already allocated*, so
`create_mockups` dispatches every render concurrently, polls inside a bounded
budget (`render_wait_seconds`, default 25s), and hands back a `render_id` for
anything still running. Do not "simplify" this back to sync.

## The upload route is deliberately unauthenticated

`GET /i/{token}.{ext}` serves staged uploads. It cannot require our bearer
token, because the client fetching it is Mockuuups' renderer. Standing in for
auth: a 256-bit token, a short TTL, a size cap, and magic-byte sniffing so only
real images are ever served back. Uploads live in memory and die with the
process — that is intentional, do not add a volume.

`PUBLIC_BASE_URL` must be the public origin. A tailnet address will stage fine
and then fail at render time, because Mockuuups cannot reach it.

## Credits

One render = 1 credit, +1 for a website screenshot, +1 for hi-res. Only
successful renders are charged. `account_status` reports the balance; check it
before a large batch. On plans with `cdn-temporary`, delivery links expire after
~24h — download anything worth keeping.

## Testing

`uv run pytest` — no network. The catalog, upload store and every tool are
covered with fakes; `tests/conftest.py` holds a miniature catalog.
