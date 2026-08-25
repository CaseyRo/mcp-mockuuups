# mcp-mockuuups

MCP server for [Mockuuups Studio](https://mockuuups.studio/) — search ~5,300 device
and print mockups, then render a screenshot or your own image into them.

![WTDIB on an iPad Air](docs/demo/wtdib-ipad-air.jpg)

## Why this exists

Mockuuups ship [their own hosted MCP server](https://github.com/Mockuuups/mockuuups-mcp)
at `https://mcp.mockuuups.studio/mcp`. It exposes a single `generate_mockup` tool
that needs a mockup id you already know and an image you have already hosted
somewhere public.

This server wraps the underlying REST API instead, and closes the two gaps that
made the hosted one awkward in practice:

- **You can search.** The upstream catalog endpoint accepts no search parameters
  at all — `q`, `type`, `family` and `tag` are silently ignored and every request
  returns the same unfiltered page. The whole catalog is fetched once and searched
  locally, so "a tablet on a desk" or "poster" actually finds something.
- **You can upload.** Mockuuups renders from a URL only. Hand this server raw
  image bytes and it stages them under a short-lived unguessable link for the
  renderer to fetch, so a local design needs no bucket, no CDN and no hosting.

## Tools

| Tool | What it answers |
|------|-----------------|
| `search_mockups` | Which mockup should I use? Free-text search over the whole catalog, with device-word aliases ("tablet", "poster", "laptop") and family/type/tag filters. |
| `create_mockups` | Put this design into these mockups. Takes a `screenshot_url`, `image_url` or `image_base64`, renders across several mockups concurrently. |
| `get_renders` | Did those renders finish? Polls anything that outran the inline wait budget. |
| `account_status` | How many credits are left, and what can this plan actually do? |

### Rendering one design across devices

Scenes shot together share a tag, so the way to get a consistent look across
devices is to search one, then filter by its tag:

```
search_mockups(query="ipad", tag="update-august-2024-meeting-room")
create_mockups(
    mockup_ids=["Zkn1GMTfiAFX5ZOn", "Zkn2DsTfiAFX5ZPD", "Zkn15MTfiAFX5ZO_"],
    screenshot_url="https://www.wattedoeninberlijn.nl",
)
```

## Configuration

See [.env.example](.env.example). The two that matter:

- `MOCKUUUPS_API_KEY` — a developer key from
  [mockuuups.studio/developers](https://mockuuups.studio/developers/).
- `PUBLIC_BASE_URL` — this server's public origin. Uploads need it, because
  Mockuuups' renderer fetches the staged image back over the public internet.
  Screenshot and image-URL renders work without it.

## Plan limits worth knowing

The API bills in credits: **one render = 1 credit, +1 for a website screenshot,
+1 for hi-res**. Only successful renders are charged.

Two behaviours will bite you if you don't know them:

- **Omitting `size` means hi-res**, which hard-fails with `feature-not-available`
  on any plan without it. This server always sends `size` explicitly, capped by
  `MOCKUUUPS_MAX_SIZE` (default 1000, the Trial ceiling). Raise it when the
  account has the `hires` feature.
- **On plans with `cdn-temporary`, delivery links expire after ~24 hours.**
  Download anything worth keeping. `account_status` reports this.

## Development

```bash
uv sync
uv run pytest
uv run mcp-mockuuups          # stdio
TRANSPORT=http uv run mcp-mockuuups   # streamable-http on /mcp
```

## License

MIT
