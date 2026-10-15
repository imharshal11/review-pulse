"""Connection test for the n8n MCP server.

Connects with the MCP SDK (Streamable HTTP first, SSE as fallback), prints the
tools the server exposes and verifies the two the pulse pipeline needs.
No tool is ever called and the token is never printed.

Usage: python src/mcp_check.py
"""

import asyncio
import os
import sys
from pathlib import Path

# Use the OS trust store so corporate/self-signed roots are accepted.
import truststore
truststore.inject_into_ssl()

from dotenv import load_dotenv

from mcp import MCPError
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.types import PaginatedRequestParams

try:
    import httpx2 as httpx  # mcp >= 2 talks HTTP through its vendored httpx fork
except ModuleNotFoundError:  # older mcp releases use plain httpx
    import httpx

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
REQUIRED_TOOLS = ("append_to_doc", "create_email_draft")

CONNECT_TIMEOUT = 20.0   # seconds allowed to open the HTTP connection
READ_TIMEOUT = 60.0      # seconds allowed to hear back from n8n

# Set in main() so error text can be scrubbed; never printed.
TOKEN = ""

# Exceptions that mean "the server never answered".
_CONNECT_ERRORS = tuple(
    cls
    for cls in (
        getattr(httpx, name, None)
        for name in (
            "ConnectError",
            "ConnectTimeout",
            "NetworkError",
            "TimeoutException",
            "ProxyError",
        )
    )
    if isinstance(cls, type)
) + (ConnectionError, TimeoutError, OSError)


# ---------------------------------------------------------------------------
# ERROR HELPERS
# ---------------------------------------------------------------------------
def _clean(text):
    """Make sure the token can never leak into any printed message."""
    if TOKEN and TOKEN in text:
        return text.replace(TOKEN, "***")
    return text


def _walk(exc):
    """Yield an exception plus everything it was chained from."""
    seen = set()
    stack = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        stack.extend((current.__cause__, current.__context__))


def _http_status(exc, statuses):
    """Best-effort HTTP status behind a failure.

    The MCP SDK turns non-2xx responses into generic JSON-RPC errors, so the
    real status is taken from the exception chain when possible, otherwise
    from the statuses recorded while the transport was talking to n8n.
    """
    for error in _walk(exc):
        response = getattr(error, "response", None)
        code = getattr(response, "status_code", None)
        if not isinstance(code, int):
            code = getattr(error, "status_code", None)
        if isinstance(code, int) and code > 0:
            return code

    for code in statuses:
        if code in (401, 403, 404):
            return code

    for error in _walk(exc):
        if isinstance(error, MCPError):
            message = (error.message or "").lower()
            if message in ("not found", "session terminated"):
                return 404
            if any(word in message for word in ("unauthorized", "forbidden", "invalid token")):
                return 401
    return None


def _is_connection_error(exc):
    return any(isinstance(error, _CONNECT_ERRORS) for error in _walk(exc))


def _friendly_reason(exc, statuses):
    status = _http_status(exc, statuses)
    if status in (401, 403):
        return "Token mismatch between .env and n8n"
    if status == 404:
        return "Wrong URL or workflow not published"
    if _is_connection_error(exc):
        return "Cannot reach n8n"
    if isinstance(exc, MCPError):
        return f"MCP error: {_clean(exc.message)}"
    return f"{type(exc).__name__}: {_clean(str(exc))}"


def _reason_rank(reason):
    """How specific a failure reason is (lower wins when both transports fail)."""
    if reason.startswith("Token mismatch"):
        return 0
    if reason.startswith("Wrong URL"):
        return 1
    if reason.startswith("Cannot reach"):
        return 2
    return 3


# ---------------------------------------------------------------------------
# TRANSPORTS
# ---------------------------------------------------------------------------
def _record_status(statuses):
    """Response hook that remembers HTTP statuses (the SDK hides them)."""
    async def hook(response):
        statuses.append(response.status_code)
    return hook


async def _list_all_tools(streams):
    """Initialize the session and return every tool the server exposes."""
    read_stream, write_stream = streams
    async with ClientSession(
        read_stream, write_stream, read_timeout_seconds=READ_TIMEOUT
    ) as session:
        await session.initialize()
        tools = []
        cursor = None
        while True:
            params = PaginatedRequestParams(cursor=cursor) if cursor else None
            page = await session.list_tools(params=params)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                return tools


async def tools_via_streamable(url, headers, statuses):
    http_client = create_mcp_http_client(
        headers=headers,
        timeout=httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT),
    )
    http_client.event_hooks = {"response": [_record_status(statuses)]}
    async with http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            return await _list_all_tools(streams)


async def tools_via_sse(url, headers, statuses):
    def factory(headers=None, auth=None, timeout=None):
        client = create_mcp_http_client(headers=headers, auth=auth, timeout=timeout)
        client.event_hooks = {"response": [_record_status(statuses)]}
        return client

    async with sse_client(
        url,
        headers=headers,
        timeout=CONNECT_TIMEOUT,
        sse_read_timeout=READ_TIMEOUT,
        httpx_client_factory=factory,
    ) as streams:
        return await _list_all_tools(streams)


# ---------------------------------------------------------------------------
# CHECK
# ---------------------------------------------------------------------------
def _print_tools(tools):
    print(f"\nTools exposed by the server ({len(tools)}):")
    for tool in tools:
        schema = tool.input_schema or {}
        params = list(schema.get("properties") or {})
        print(f"  - {tool.name}")
        description = " ".join((tool.description or "").split())
        if description:
            print(f"      description: {_clean(description)}")
        print(f"      parameters: {', '.join(params) if params else '(none)'}")


def _validate_tools(tools):
    expected = set(REQUIRED_TOOLS)
    actual = {tool.name for tool in tools}
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if not missing and not unexpected:
        return True
    parts = []
    if missing:
        parts.append("missing: " + ", ".join(missing))
    if unexpected:
        parts.append("unexpected: " + ", ".join(unexpected))
    print("MCP CHECK FAILED: " + "; ".join(parts))
    return False


async def run_check(url, token):
    headers = {"Authorization": f"Bearer {token}"}
    transports = (
        ("Streamable HTTP", tools_via_streamable),
        ("SSE", tools_via_sse),
    )

    tools = None
    failures = []
    for name, runner in transports:
        statuses = []
        try:
            tools = await runner(url, headers, statuses)
        except Exception as exc:
            reason = _friendly_reason(exc, statuses)
            print(f"{name} transport failed: {reason}")
            failures.append(reason)
            if reason.startswith("Cannot reach"):
                print("Skipping SSE fallback - the server is unreachable either way.")
                break
            continue
        print(f"Connected via {name} transport.")
        break

    if tools is None:
        reason = min(failures, key=_reason_rank) if failures else "could not connect"
        print(f"MCP CHECK FAILED: {reason}")
        return 1

    _print_tools(tools)
    if not _validate_tools(tools):
        return 1
    print("MCP CHECK PASSED")
    return 0


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    global TOKEN
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    url = (os.getenv("N8N_MCP_URL") or "").strip()
    token = (os.getenv("N8N_MCP_TOKEN") or "").strip()

    missing = [
        name
        for name, value in (("N8N_MCP_URL", url), ("N8N_MCP_TOKEN", token))
        if not value
    ]
    if missing:
        print("MCP CHECK FAILED: missing " + ", ".join(missing) + " in .env")
        return 1

    TOKEN = token
    print(f"Testing n8n MCP endpoint: {url}")
    return asyncio.run(run_check(url, token))


if __name__ == "__main__":
    sys.exit(main())
