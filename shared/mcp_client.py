"""
Lightweight MCP client — Streamable HTTP (stateless) transport.

Provides:
  AsyncMcpClient  — for FastAPI (uses httpx.AsyncClient)
  SyncMcpClient   — for the worker process (uses httpx.Client)

Both skip the initialize/notifications handshake because the Railway MCP
servers run in stateless mode (a fresh McpServer per POST).
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
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": id}
    if params is not None:
        msg["params"] = params
    return msg


def _parse_sse(text: str) -> dict:
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
    if "text/event-stream" in resp.headers.get("content-type", ""):
        return _parse_sse(resp.text)
    return resp.json()


def _extract_text(result: Any) -> str:
    if result and "content" in result:
        texts = [c["text"] for c in result["content"] if c.get("type") == "text"]
        return "\n".join(texts)
    return str(result)


# ─── Async client (FastAPI) ───────────────────────────────────────────────────

class AsyncMcpClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient):
        self.endpoint = base_url.rstrip("/") + "/mcp"
        self._base = base_url.rstrip("/")
        self._client = client

    async def _call(self, method: str, params: dict | None = None) -> Any:
        req = _jsonrpc(method, params)
        resp = await self._client.post(self.endpoint, json=req, headers=_MCP_HEADERS)
        resp.raise_for_status()
        data = _parse_response(resp)
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result")

    async def call_tool(self, name: str, arguments: dict | None = None) -> str:
        result = await self._call("tools/call", {"name": name, "arguments": arguments or {}})
        return _extract_text(result)

    async def read_resource(self, uri: str) -> str:
        result = await self._call("resources/read", {"uri": uri})
        if result and "contents" in result:
            return "\n".join(c.get("text", "") for c in result["contents"])
        return str(result)

    async def health_check(self) -> dict:
        resp = await self._client.get(f"{self._base}/health")
        resp.raise_for_status()
        return resp.json()


# ─── Sync client (worker) ─────────────────────────────────────────────────────

class SyncMcpClient:
    """Synchronous MCP client for use in the worker process."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.endpoint = f"{self.base_url}/mcp"
        self._client = httpx.Client(timeout=_TIMEOUT)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "SyncMcpClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def _call(self, method: str, params: dict | None = None) -> Any:
        req = _jsonrpc(method, params)
        resp = self._client.post(self.endpoint, json=req, headers=_MCP_HEADERS)
        resp.raise_for_status()
        data = _parse_response(resp)
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result")

    def call_tool(self, name: str, arguments: dict | None = None) -> str:
        result = self._call("tools/call", {"name": name, "arguments": arguments or {}})
        return _extract_text(result)

    def read_resource(self, uri: str) -> str:
        result = self._call("resources/read", {"uri": uri})
        if result and "contents" in result:
            return "\n".join(c.get("text", "") for c in result["contents"])
        return str(result)

    def health_check(self) -> dict:
        resp = self._client.get(f"{self.base_url}/health")
        resp.raise_for_status()
        return resp.json()
