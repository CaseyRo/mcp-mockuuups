"""The MCP surface over the wire, via an in-memory ``fastmcp.Client``.

Guards the fastmcp upgrade: the server imports, its tools register, and the
annotation hints reach the client. Read as snake_case, since CI runs with
FASTMCP_MCP_CAMELCASE_COMPAT=false. Tool results and telemetry live in test_server.py.
"""

from fastmcp import Client

from mcp_mockuuups import server

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
