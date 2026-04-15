"""
Lightweight MCP client that speaks Streamable HTTP to remote MCP servers.

Each server runs on Railway with a POST /mcp endpoint. This client sends
JSON-RPC 2.0 messages following the MCP Streamable HTTP transport spec.

Because the servers run in stateless mode (`sessionIdGenerator: undefined`
and a fresh `McpServer` per POST), we skip the `initialize` +
`notifications/initialized` handshake: those requests hit a server instance
that is immediately discarded and have zero effect on the next request. We
send the actual method call directly, which cuts each tool/resource call from
three HTTP requests to one.
"""

import json
import httpx
from typing import Any

_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _jsonrpc(method: str, params: dict | None = None, id: int = 1) -> dict:
    """Build a JSON-RPC 2.0 request envelope."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": id}
    if params is not None:
        msg["params"] = params
    return msg


def _parse_sse(text: str) -> dict:
    """Return the first JSON-RPC response object found in an SSE stream."""
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                try:
                    obj = json.loads(payload)
                    if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                        return obj
                except json.JSONDecodeError:
                    pass
    raise ValueError(f"No JSON-RPC result found in SSE stream: {text!r}")


def _parse_response(resp: httpx.Response) -> dict:
    """Parse an MCP server response handling both JSON and SSE content types."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        return _parse_sse(resp.text)
    return resp.json()


class McpClient:
    """Synchronous MCP client for Streamable HTTP (stateless) servers.

    Keeps a single `httpx.Client` alive for the lifetime of the instance so
    that repeated calls reuse the same TCP/TLS connection (keep-alive),
    which matters when validating 185 countries in a single scan.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')
        self.endpoint = f"{self.base_url}/mcp"
        self._client = httpx.Client(timeout=_TIMEOUT)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "McpClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        # Best-effort cleanup if the caller forgets to close us.
        self.close()

    def _call(self, method: str, params: dict | None = None) -> Any:
        """Send a JSON-RPC request to the MCP server and return the result.

        The servers we talk to run in stateless Streamable HTTP mode: each
        POST spins up a fresh `McpServer` instance that is discarded after
        the response is written. Sending `initialize` and
        `notifications/initialized` on prior POSTs has no effect on the
        server that will handle the actual call, so we skip that handshake
        entirely — one request per logical call.
        """
        req = _jsonrpc(method, params, id=1)
        resp = self._client.post(
            self.endpoint,
            json=req,
            headers=_MCP_HEADERS,
        )
        resp.raise_for_status()
        data = _parse_response(resp)

        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result")

    # ─── High-level helpers ──────────────────────────────────────────────

    def list_tools(self) -> list[dict]:
        result = self._call("tools/list")
        return result.get("tools", []) if result else []

    def call_tool(self, name: str, arguments: dict | None = None) -> str:
        """Call an MCP tool and return its text content."""
        result = self._call("tools/call", {"name": name, "arguments": arguments or {}})
        if result and "content" in result:
            texts = [c["text"] for c in result["content"] if c.get("type") == "text"]
            return "\n".join(texts)
        return str(result)

    def list_resources(self) -> list[dict]:
        result = self._call("resources/list")
        return result.get("resources", []) if result else []

    def read_resource(self, uri: str) -> str:
        """Read an MCP resource and return its text content."""
        result = self._call("resources/read", {"uri": uri})
        if result and "contents" in result:
            texts = [c.get("text", "") for c in result["contents"]]
            return "\n".join(texts)
        return str(result)

    def health_check(self) -> dict:
        """Check the server health via REST endpoint."""
        resp = self._client.get(f"{self.base_url}/health")
        resp.raise_for_status()
        return resp.json()
