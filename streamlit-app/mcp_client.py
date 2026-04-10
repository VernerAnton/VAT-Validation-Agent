"""
Lightweight MCP client that speaks Streamable HTTP to remote MCP servers.

Each server runs on Railway with a POST /mcp endpoint. This client sends
JSON-RPC 2.0 messages following the MCP Streamable HTTP transport spec.
"""

import httpx
from typing import Any

_TIMEOUT = httpx.Timeout(60.0, connect=15.0)


def _jsonrpc(method: str, params: dict | None = None, id: int = 1) -> dict:
    """Build a JSON-RPC 2.0 request envelope."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": id}
    if params is not None:
        msg["params"] = params
    return msg


class McpClient:
    """Synchronous MCP client for Streamable HTTP (stateless) servers."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')
        self.endpoint = f"{self.base_url}/mcp"

    def _call(self, method: str, params: dict | None = None) -> Any:
        """Send a JSON-RPC request to the MCP server and return the result.

        Stateless servers need initialize + the actual call in the same
        logical interaction. But since each POST is independent and the
        server creates a fresh McpServer per request, we need to send
        initialize first, then make a second request for the actual call.

        Actually for stateless mode, the server handles each POST
        independently. We need to:
        1. POST initialize → get capabilities
        2. POST initialized notification (no response expected)
        3. POST the actual method call

        But for simple stateless servers, we can batch or just make
        separate calls. Let's send initialize + actual call as separate
        requests.
        """
        with httpx.Client(timeout=_TIMEOUT) as client:
            # Step 1: Initialize
            init_req = _jsonrpc("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "vat-agent-streamlit", "version": "1.0.0"},
            }, id=1)
            init_resp = client.post(
                self.endpoint,
                json=init_req,
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            )
            init_resp.raise_for_status()

            # Step 2: Send initialized notification
            notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            client.post(
                self.endpoint,
                json=notif,
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            )
            # Notification responses may be empty — that's fine

            # Step 3: Send the actual method call
            req = _jsonrpc(method, params, id=2)
            resp = client.post(
                self.endpoint,
                json=req,
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            )
            resp.raise_for_status()
            data = resp.json()

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
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(f"{self.base_url}/health")
            resp.raise_for_status()
            return resp.json()
