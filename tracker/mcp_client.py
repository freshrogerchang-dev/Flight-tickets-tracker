"""A minimal synchronous MCP client, just enough to call one tool.

Written by hand rather than pulled from the official SDK because that SDK is
async and would drag an event loop into what is otherwise a plain script -- and
because one more dependency is one more thing that can fail to install inside a
scheduled job. Everything here is JSON-RPC over a single HTTP endpoint.

Only the streamable-HTTP transport is implemented, and only the handshake plus
``tools/call``. Responses arrive either as plain JSON or as a one-event SSE
stream, so both are accepted.
"""

from __future__ import annotations

import json
from typing import Any

import requests

PROTOCOL_VERSION = "2025-06-18"
TIMEOUT_SECONDS = 90


class McpError(RuntimeError):
    """The MCP endpoint could not be reached, or answered with an error."""


def _parse_payload(response: requests.Response) -> dict:
    """Read a JSON-RPC message out of either a JSON body or an SSE stream."""
    text = response.text or ""
    content_type = response.headers.get("Content-Type", "")

    if "text/event-stream" in content_type or text.lstrip().startswith("event:"):
        # Server-sent events: the message sits in one or more `data:` lines.
        for line in text.splitlines():
            if line.startswith("data:"):
                chunk = line[len("data:") :].strip()
                if not chunk:
                    continue
                try:
                    message = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict) and ("result" in message or "error" in message):
                    return message
        raise McpError(f"SSE 回應裡沒有可用的訊息: {text[:300]}")

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpError(f"回應不是 JSON（HTTP {response.status_code}）: {text[:300]}") from exc


class McpSession:
    """One short-lived session against a streamable-HTTP MCP server."""

    def __init__(self, url: str, *, token: str | None = None, timeout: int = TIMEOUT_SECONDS):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._id = 0
        self._session_id: str | None = None
        self._headers = {
            "Content-Type": "application/json",
            # Servers may answer either way; say we understand both.
            "Accept": "application/json, text/event-stream",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _post(self, body: dict, *, expect_reply: bool = True) -> dict | None:
        headers = dict(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        try:
            response = requests.post(self.url, json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise McpError(f"連線 {self.url} 失敗: {exc}") from exc

        # The server assigns a session on initialize; carry it on every later call.
        assigned = response.headers.get("Mcp-Session-Id") or response.headers.get("mcp-session-id")
        if assigned:
            self._session_id = assigned

        if not expect_reply:
            return None

        if response.status_code >= 400:
            raise McpError(f"HTTP {response.status_code}: {(response.text or '')[:300]}")

        message = _parse_payload(response)
        if "error" in message:
            detail = message["error"]
            raise McpError(f"MCP 錯誤: {detail.get('message', detail)}")
        return message.get("result", {})

    def initialize(self) -> dict:
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "flight-tickets-tracker", "version": "0.1.0"},
                },
            }
        )
        # Required by the spec before any other request; servers may reject
        # tool calls that arrive before it.
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_reply=False)
        return result or {}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool and return its payload, decoded from whatever shape it used."""
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        ) or {}

        if result.get("isError"):
            raise McpError(f"工具 {name} 回報錯誤: {_first_text(result)[:300]}")

        # Newer servers return a parsed object alongside the text rendering.
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and structured:
            return structured

        text = _first_text(result)
        if not text:
            raise McpError(f"工具 {name} 沒有回傳內容")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Some tools answer in prose; hand it back for the caller to judge.
            return text


def _first_text(result: dict) -> str:
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
            return str(item["text"])
    return ""


def call_tool(url: str, name: str, arguments: dict[str, Any], *, token: str | None = None) -> Any:
    """Open a session, call one tool, and return its payload."""
    session = McpSession(url, token=token)
    session.initialize()
    return session.call_tool(name, arguments)
