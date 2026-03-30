"""Tests for the MCP server (Phase 3).

Spec reference: Sections 7, 13, 17 (Phase 3)

Tests the JSON-RPC 2.0 MCP protocol layer. Each test creates a fresh
in-memory database and calls the server's dispatch logic directly
(no subprocess needed for unit tests).
"""

import json
import sqlite3

import pytest

from agent_bbs.mcp_server import MCPServer
from agent_bbs.schema import create_tables


@pytest.fixture()
def server():
    """Create an MCP server with a fresh in-memory database and registered agent."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    create_tables(conn)
    # Register the test agent so subscriptions / notifications work
    from agent_bbs.agents import register_agent
    register_agent(conn, agent_id="test-agent", display_name="Test Agent")
    srv = MCPServer(conn, agent_id="test-agent")
    yield srv
    conn.close()


def _call(server, method, params=None, id=1):
    """Send a JSON-RPC request and return the parsed response."""
    request = {"jsonrpc": "2.0", "method": method, "id": id}
    if params is not None:
        request["params"] = params
    return server.handle_request(request)


# ---------------------------------------------------------------------------
# Tool listing
# ---------------------------------------------------------------------------

class TestToolsListing:
    """All 6 tools must appear in tools/list response."""

    EXPECTED_TOOLS = {
        "bbs_post", "bbs_read", "bbs_search",
        "bbs_link", "bbs_subscribe", "bbs_notify",
    }

    def test_all_tools_listed(self, server):
        resp = _call(server, "tools/list")
        assert "error" not in resp
        tool_names = {t["name"] for t in resp["result"]["tools"]}
        assert self.EXPECTED_TOOLS == tool_names

    def test_tools_have_input_schema(self, server):
        resp = _call(server, "tools/list")
        for tool in resp["result"]["tools"]:
            assert "inputSchema" in tool, f"{tool['name']} missing inputSchema"
            assert tool["inputSchema"]["type"] == "object"


# ---------------------------------------------------------------------------
# Schema enum validation
# ---------------------------------------------------------------------------

class TestSchemaEnums:
    """Tool schemas must contain the exact spec enums."""

    ENTRY_TYPES = ["finding", "question", "synthesis", "contradiction", "task"]
    PERFORMATIVES = ["inform", "request", "propose", "confirm", "disconfirm",
                     "retract", "query", "ack", "decline"]
    LINK_TYPES = ["supports", "contradicts", "supersedes", "responds_to",
                  "derived_from", "depends_on", "same_as", "retracted_by"]

    def _get_tool(self, server, name):
        resp = _call(server, "tools/list")
        for t in resp["result"]["tools"]:
            if t["name"] == name:
                return t
        pytest.fail(f"Tool {name} not found")

    def test_bbs_post_entry_type_enum(self, server):
        tool = self._get_tool(server, "bbs_post")
        schema = tool["inputSchema"]
        assert sorted(schema["properties"]["entry_type"]["enum"]) == sorted(self.ENTRY_TYPES)

    def test_bbs_post_performative_enum(self, server):
        tool = self._get_tool(server, "bbs_post")
        schema = tool["inputSchema"]
        assert sorted(schema["properties"]["performative"]["enum"]) == sorted(self.PERFORMATIVES)

    def test_bbs_link_link_type_enum(self, server):
        tool = self._get_tool(server, "bbs_link")
        schema = tool["inputSchema"]
        assert sorted(schema["properties"]["link_type"]["enum"]) == sorted(self.LINK_TYPES)

    def test_bbs_read_direction_enum(self, server):
        tool = self._get_tool(server, "bbs_read")
        schema = tool["inputSchema"]
        assert sorted(schema["properties"]["direction"]["enum"]) == sorted(["inbound", "outbound", "both"])

    def test_bbs_search_direction_enum(self, server):
        tool = self._get_tool(server, "bbs_search")
        schema = tool["inputSchema"]
        assert sorted(schema["properties"]["direction"]["enum"]) == sorted(["inbound", "outbound", "both"])

    def test_bbs_post_has_idempotency_key(self, server):
        tool = self._get_tool(server, "bbs_post")
        assert "idempotency_key" in tool["inputSchema"]["properties"]

    def test_bbs_link_has_idempotency_key(self, server):
        tool = self._get_tool(server, "bbs_link")
        assert "idempotency_key" in tool["inputSchema"]["properties"]

    def test_bbs_read_has_graph_traversal_params(self, server):
        tool = self._get_tool(server, "bbs_read")
        props = tool["inputSchema"]["properties"]
        assert "hops" in props
        assert "link_types" in props
        assert "direction" in props

    def test_bbs_search_has_graph_traversal_params(self, server):
        tool = self._get_tool(server, "bbs_search")
        props = tool["inputSchema"]["properties"]
        assert "hops" in props
        assert "link_types" in props
        assert "direction" in props


# ---------------------------------------------------------------------------
# Round-trip: post then read
# ---------------------------------------------------------------------------

class TestRoundTrip:
    """Post via MCP, read via MCP, verify content matches."""

    def test_post_then_read_by_id(self, server):
        post_resp = _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "finding",
                "performative": "inform",
                "content": "Round-trip test data",
                "tags": ["test"],
            },
        })
        assert "error" not in post_resp
        post_result = json.loads(post_resp["result"]["content"][0]["text"])
        entry_id = post_result["id"]

        read_resp = _call(server, "tools/call", {
            "name": "bbs_read",
            "arguments": {"entry_ids": [entry_id]},
        })
        assert "error" not in read_resp
        read_result = json.loads(read_resp["result"]["content"][0]["text"])
        assert len(read_result) == 1
        assert read_result[0]["content"] == "Round-trip test data"
        assert read_result[0]["record_hash"] == post_result["record_hash"]

    def test_post_then_read_by_hash(self, server):
        post_resp = _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "finding",
                "performative": "inform",
                "content": "Hash lookup test",
            },
        })
        record_hash = json.loads(post_resp["result"]["content"][0]["text"])["record_hash"]

        read_resp = _call(server, "tools/call", {
            "name": "bbs_read",
            "arguments": {"record_hashes": [record_hash]},
        })
        read_result = json.loads(read_resp["result"]["content"][0]["text"])
        assert len(read_result) == 1
        assert read_result[0]["content"] == "Hash lookup test"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class TestMCPSearch:
    """Search returns results for posted content."""

    def test_search_finds_posted_entry(self, server):
        _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "finding",
                "performative": "inform",
                "content": "Quantum entanglement breakthrough",
                "tags": ["physics"],
            },
        })
        search_resp = _call(server, "tools/call", {
            "name": "bbs_search",
            "arguments": {"q": "quantum"},
        })
        results = json.loads(search_resp["result"]["content"][0]["text"])
        assert len(results) == 1
        assert "Quantum" in results[0]["content"]

    def test_search_with_filters(self, server):
        _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "finding",
                "performative": "inform",
                "content": "Alpha finding",
            },
        })
        _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "question",
                "performative": "query",
                "content": "Alpha question",
            },
        })
        search_resp = _call(server, "tools/call", {
            "name": "bbs_search",
            "arguments": {"q": "Alpha", "entry_type": "question"},
        })
        results = json.loads(search_resp["result"]["content"][0]["text"])
        assert len(results) == 1
        assert results[0]["entry_type"] == "question"


# ---------------------------------------------------------------------------
# Subscribe + Post + Notify end-to-end
# ---------------------------------------------------------------------------

class TestSubscribeNotifyFlow:
    """End-to-end: subscribe → post matching entry → notify returns it."""

    def test_full_notification_flow(self, server):
        # Subscribe
        sub_resp = _call(server, "tools/call", {
            "name": "bbs_subscribe",
            "arguments": {"filter_tags": ["ai"]},
        })
        assert "error" not in sub_resp

        # Post matching entry from a different "agent" — but since MCP server
        # uses a fixed agent_id, we need to post as a different author.
        # We'll directly insert a subscription for our test-agent, then post
        # from a different author via the operations layer.
        from agent_bbs.entries import post_entry
        post_entry(server._conn, author_id="other-agent", entry_type="finding",
                   performative="inform", content="AI research update",
                   tags=["ai"])

        # Notify
        notify_resp = _call(server, "tools/call", {
            "name": "bbs_notify",
            "arguments": {},
        })
        assert "error" not in notify_resp
        notifs = json.loads(notify_resp["result"]["content"][0]["text"])
        assert len(notifs) >= 1
        assert "content" not in notifs[0]  # metadata only
        assert notifs[0]["entry_type"] == "finding"


# ---------------------------------------------------------------------------
# Graph traversal via MCP
# ---------------------------------------------------------------------------

class TestMCPGraphTraversal:
    """Post entries with links, read with hops via MCP."""

    def test_read_with_hops(self, server):
        # Create chain: A → B → C
        a = _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "finding", "performative": "inform",
                "content": "Entry A",
            },
        })
        a_id = json.loads(a["result"]["content"][0]["text"])["id"]

        b = _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "finding", "performative": "inform",
                "content": "Entry B",
            },
        })
        b_id = json.loads(b["result"]["content"][0]["text"])["id"]

        c = _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "finding", "performative": "inform",
                "content": "Entry C",
            },
        })
        c_id = json.loads(c["result"]["content"][0]["text"])["id"]

        # Link A→B and B→C
        _call(server, "tools/call", {
            "name": "bbs_link",
            "arguments": {
                "source_entry_id": a_id, "target_entry_id": b_id,
                "link_type": "supports",
            },
        })
        _call(server, "tools/call", {
            "name": "bbs_link",
            "arguments": {
                "source_entry_id": b_id, "target_entry_id": c_id,
                "link_type": "supports",
            },
        })

        # Read A with hops=2 → should get A, B, C
        read_resp = _call(server, "tools/call", {
            "name": "bbs_read",
            "arguments": {"entry_ids": [a_id], "hops": 2},
        })
        results = json.loads(read_resp["result"]["content"][0]["text"])
        result_ids = {r["id"] for r in results}
        assert {a_id, b_id, c_id} == result_ids

    def test_read_with_direction_filter(self, server):
        a = _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "finding", "performative": "inform",
                "content": "Source",
            },
        })
        a_id = json.loads(a["result"]["content"][0]["text"])["id"]

        b = _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "finding", "performative": "inform",
                "content": "Target",
            },
        })
        b_id = json.loads(b["result"]["content"][0]["text"])["id"]

        _call(server, "tools/call", {
            "name": "bbs_link",
            "arguments": {
                "source_entry_id": a_id, "target_entry_id": b_id,
                "link_type": "supports",
            },
        })

        # Outbound from A → should reach B
        read_resp = _call(server, "tools/call", {
            "name": "bbs_read",
            "arguments": {
                "entry_ids": [a_id], "hops": 1, "direction": "outbound",
            },
        })
        results = json.loads(read_resp["result"]["content"][0]["text"])
        result_ids = {r["id"] for r in results}
        assert b_id in result_ids

        # Inbound from A → should NOT reach B
        read_resp2 = _call(server, "tools/call", {
            "name": "bbs_read",
            "arguments": {
                "entry_ids": [a_id], "hops": 1, "direction": "inbound",
            },
        })
        results2 = json.loads(read_resp2["result"]["content"][0]["text"])
        result_ids2 = {r["id"] for r in results2}
        assert b_id not in result_ids2


# ---------------------------------------------------------------------------
# Idempotency via MCP
# ---------------------------------------------------------------------------

class TestMCPIdempotency:
    """Idempotency keys work through the MCP layer."""

    def test_post_idempotency(self, server):
        args = {
            "entry_type": "finding", "performative": "inform",
            "content": "Idempotent entry", "idempotency_key": "mcp-key-1",
        }
        r1 = _call(server, "tools/call", {"name": "bbs_post", "arguments": args})
        r2 = _call(server, "tools/call", {"name": "bbs_post", "arguments": args})
        id1 = json.loads(r1["result"]["content"][0]["text"])["id"]
        id2 = json.loads(r2["result"]["content"][0]["text"])["id"]
        assert id1 == id2

    def test_link_idempotency(self, server):
        e1 = _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "finding", "performative": "inform",
                "content": "A",
            },
        })
        e2 = _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "finding", "performative": "inform",
                "content": "B",
            },
        })
        e1_id = json.loads(e1["result"]["content"][0]["text"])["id"]
        e2_id = json.loads(e2["result"]["content"][0]["text"])["id"]

        link_args = {
            "source_entry_id": e1_id, "target_entry_id": e2_id,
            "link_type": "supports", "idempotency_key": "link-key-1",
        }
        r1 = _call(server, "tools/call", {"name": "bbs_link", "arguments": link_args})
        r2 = _call(server, "tools/call", {"name": "bbs_link", "arguments": link_args})
        id1 = json.loads(r1["result"]["content"][0]["text"])["id"]
        id2 = json.loads(r2["result"]["content"][0]["text"])["id"]
        assert id1 == id2


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestMCPErrorHandling:
    """MCP returns proper error responses for invalid input."""

    def test_invalid_entry_type(self, server):
        resp = _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "bogus",
                "performative": "inform",
                "content": "bad",
            },
        })
        # Should return an error in the result (isError flag)
        assert resp["result"].get("isError") is True

    def test_invalid_performative(self, server):
        resp = _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "finding",
                "performative": "shout",
                "content": "bad",
            },
        })
        assert resp["result"].get("isError") is True

    def test_missing_required_field(self, server):
        resp = _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "finding",
                # missing performative and content
            },
        })
        assert resp["result"].get("isError") is True

    def test_invalid_link_type(self, server):
        e1 = _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "finding", "performative": "inform",
                "content": "A",
            },
        })
        e2 = _call(server, "tools/call", {
            "name": "bbs_post",
            "arguments": {
                "entry_type": "finding", "performative": "inform",
                "content": "B",
            },
        })
        e1_id = json.loads(e1["result"]["content"][0]["text"])["id"]
        e2_id = json.loads(e2["result"]["content"][0]["text"])["id"]

        resp = _call(server, "tools/call", {
            "name": "bbs_link",
            "arguments": {
                "source_entry_id": e1_id, "target_entry_id": e2_id,
                "link_type": "related_to",
            },
        })
        assert resp["result"].get("isError") is True

    def test_unknown_tool(self, server):
        resp = _call(server, "tools/call", {
            "name": "bbs_nonexistent",
            "arguments": {},
        })
        assert "error" in resp or resp["result"].get("isError") is True

    def test_unknown_method(self, server):
        resp = _call(server, "unknown/method")
        assert "error" in resp


# ---------------------------------------------------------------------------
# MCP protocol basics
# ---------------------------------------------------------------------------

class TestMCPProtocol:
    """JSON-RPC 2.0 protocol compliance."""

    def test_initialize(self, server):
        resp = _call(server, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.1"},
        })
        assert "error" not in resp
        assert resp["result"]["protocolVersion"] == "2024-11-05"
        assert "tools" in resp["result"]["capabilities"]

    def test_response_has_jsonrpc_field(self, server):
        resp = _call(server, "tools/list")
        assert resp["jsonrpc"] == "2.0"

    def test_response_has_matching_id(self, server):
        resp = _call(server, "tools/list", id=42)
        assert resp["id"] == 42
