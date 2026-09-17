"""The hand-rolled MCP client: handshake, session handling, and both reply formats.

This is the layer that cannot be exercised against the real endpoint from CI,
so the protocol handling is pinned here instead.
"""

from __future__ import annotations

import json

import pytest

from tracker.mcp_client import McpError, McpSession, _parse_payload, call_tool


class FakeResponse:
    def __init__(self, body: str = "", status_code: int = 200, headers: dict | None = None):
        self.text = body
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/json"}


def rpc(result: dict, id_: int = 1) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id_, "result": result})


@pytest.fixture
def transport(monkeypatch):
    """Capture requests and reply with a scripted queue of responses."""
    state = {"calls": [], "replies": []}

    def fake_post(url, json=None, headers=None, timeout=None):
        state["calls"].append({"url": url, "body": json, "headers": headers})
        return state["replies"].pop(0) if state["replies"] else FakeResponse(rpc({}))

    monkeypatch.setattr("requests.post", fake_post)
    return state


# ---------------------------------------------------------------- reply formats


def test_a_plain_json_reply_is_parsed():
    response = FakeResponse(rpc({"ok": True}))

    assert _parse_payload(response)["result"] == {"ok": True}


def test_an_sse_reply_is_parsed():
    body = "event: message\ndata: " + rpc({"ok": True}) + "\n\n"
    response = FakeResponse(body, headers={"Content-Type": "text/event-stream"})

    assert _parse_payload(response)["result"] == {"ok": True}


def test_an_sse_reply_is_recognised_without_the_content_type():
    """Some servers stream without labelling it; sniff the body instead."""
    body = "event: message\ndata: " + rpc({"ok": True}) + "\n"

    assert _parse_payload(FakeResponse(body))["result"] == {"ok": True}


def test_sse_keepalive_lines_are_skipped():
    body = "event: ping\ndata: \n\nevent: message\ndata: " + rpc({"ok": True}) + "\n\n"
    response = FakeResponse(body, headers={"Content-Type": "text/event-stream"})

    assert _parse_payload(response)["result"] == {"ok": True}


def test_an_unparseable_body_says_so():
    with pytest.raises(McpError, match="不是 JSON"):
        _parse_payload(FakeResponse("<html>502 Bad Gateway</html>"))


# ---------------------------------------------------------------- session


def test_initialize_then_notify(transport):
    transport["replies"] = [
        FakeResponse(rpc({"protocolVersion": "2025-06-18"}), headers={"Content-Type": "application/json"}),
        FakeResponse(""),
    ]
    McpSession("https://mcp.example.com").initialize()

    methods = [call["body"]["method"] for call in transport["calls"]]
    assert methods == ["initialize", "notifications/initialized"]
    assert "id" not in transport["calls"][1]["body"], "a notification carries no id"


def test_the_session_id_is_echoed_on_later_calls(transport):
    transport["replies"] = [
        FakeResponse(rpc({}), headers={"Content-Type": "application/json", "Mcp-Session-Id": "sess-42"}),
        FakeResponse(""),
        FakeResponse(rpc({"content": [{"type": "text", "text": "{}"}]})),
    ]
    session = McpSession("https://mcp.example.com")
    session.initialize()
    session.call_tool("search-flight", {})

    assert transport["calls"][0]["headers"].get("Mcp-Session-Id") is None, "none to send yet"
    assert transport["calls"][-1]["headers"]["Mcp-Session-Id"] == "sess-42"


def test_request_ids_increment(transport):
    transport["replies"] = [FakeResponse(rpc({})), FakeResponse(""), FakeResponse(rpc({"structuredContent": {"a": 1}}))]
    session = McpSession("https://mcp.example.com")
    session.initialize()
    session.call_tool("t", {})

    ids = [c["body"]["id"] for c in transport["calls"] if "id" in c["body"]]
    assert ids == sorted(set(ids)) and len(ids) == 2


# ---------------------------------------------------------------- tool results


def test_structured_content_is_preferred(transport):
    transport["replies"] = [
        FakeResponse(rpc({"structuredContent": {"itineraries": [1]}, "content": [{"type": "text", "text": "ignored"}]}))
    ]
    session = McpSession("https://mcp.example.com")

    assert session.call_tool("t", {}) == {"itineraries": [1]}


def test_json_inside_text_content_is_decoded(transport):
    transport["replies"] = [FakeResponse(rpc({"content": [{"type": "text", "text": '{"currency": "TWD"}'}]}))]
    session = McpSession("https://mcp.example.com")

    assert session.call_tool("t", {}) == {"currency": "TWD"}


def test_prose_content_is_returned_as_text(transport):
    transport["replies"] = [FakeResponse(rpc({"content": [{"type": "text", "text": "no flights found"}]}))]
    session = McpSession("https://mcp.example.com")

    assert session.call_tool("t", {}) == "no flights found"


def test_a_tool_error_flag_raises(transport):
    transport["replies"] = [
        FakeResponse(rpc({"isError": True, "content": [{"type": "text", "text": "bad airport code"}]}))
    ]
    session = McpSession("https://mcp.example.com")

    with pytest.raises(McpError, match="bad airport code"):
        session.call_tool("t", {})


def test_a_jsonrpc_error_raises(transport):
    transport["replies"] = [
        FakeResponse(json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no such tool"}}))
    ]
    session = McpSession("https://mcp.example.com")

    with pytest.raises(McpError, match="no such tool"):
        session.call_tool("t", {})


def test_an_http_error_includes_the_body(transport):
    transport["replies"] = [FakeResponse("rate limited", status_code=429)]
    session = McpSession("https://mcp.example.com")

    with pytest.raises(McpError, match="429"):
        session.call_tool("t", {})


def test_a_connection_failure_is_wrapped(monkeypatch):
    import requests

    def boom(*args, **kwargs):
        raise requests.ConnectionError("dns failure")

    monkeypatch.setattr("requests.post", boom)

    with pytest.raises(McpError, match="失敗"):
        McpSession("https://mcp.example.com").initialize()


def test_call_tool_helper_does_the_whole_dance(transport):
    transport["replies"] = [
        FakeResponse(rpc({}), headers={"Content-Type": "application/json", "Mcp-Session-Id": "s1"}),
        FakeResponse(""),
        FakeResponse(rpc({"structuredContent": {"ok": 1}})),
    ]

    assert call_tool("https://mcp.example.com", "search-flight", {"flyFrom": "TPE"}) == {"ok": 1}
    assert [c["body"]["method"] for c in transport["calls"]] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert transport["calls"][-1]["body"]["params"]["arguments"] == {"flyFrom": "TPE"}
