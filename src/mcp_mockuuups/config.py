"""Configuration loaded from environment variables."""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Server transport
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000

    # Bearer token auth for the MCP endpoint
    mcp_api_key: SecretStr = SecretStr("")

    # Mockuuups Studio developer API
    mockuuups_api_key: SecretStr = SecretStr("")

    # The Trial plan renders at most 1000px; larger sizes need the plan's
    # "hires" feature and fail the whole render with feature-not-available.
    # Raise this once the account is on a paid plan.
    mockuuups_max_size: int = 1000

    # Public origin this server is reachable at. Mockuuups' renderer fetches
    # uploaded images back over the public internet, so uploads only work when
    # this is set to an externally-resolvable URL.
    public_base_url: str = ""

    # How long an uploaded image stays resolvable. Long enough for a render to
    # start (dispatch is sub-second), short enough that a leaked token is stale.
    upload_ttl_seconds: int = 900
    # Hard ceiling on a single uploaded image. Mockuuups reads it over the wire;
    # anything larger is a mistake, not a mockup.
    upload_max_bytes: int = 12 * 1024 * 1024

    # Catalog has no server-side search, so it is fetched whole and cached.
    catalog_ttl_seconds: int = 86_400

    # Bounded inline wait for renders. Clients reach this server through a
    # Cloudflare MCP portal with a hard ~60s upstream read timeout we cannot
    # change, and a screenshot render measured 17s. Dispatch is async and the
    # poll is capped well under the portal budget; anything still pending comes
    # back as a handle for get_renders.
    render_wait_seconds: int = 25

    model_config = {"env_prefix": "", "case_sensitive": False}

    @model_validator(mode="after")
    def require_api_key_for_http(self) -> Settings:
        if self.transport == "http" and not self.mcp_api_key.get_secret_value():
            raise ValueError(
                "MCP_API_KEY is required when TRANSPORT=http. "
                "Refusing to start an unauthenticated server."
            )
        return self


settings = Settings()
