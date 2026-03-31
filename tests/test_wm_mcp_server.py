"""Tests for the Working Memory MCP server.

Tests the JSON-RPC 2.0 MCP protocol layer over working memory operations.
Each test creates fresh in-memory databases and calls the server's dispatch
logic directly (no subprocess needed for unit tests).
"""

import json
import sqlite3

import pytest

from agent_bbs.agents import register_agent
from agent_bbs.entries import post_entry
from agent_bbs.schema import create_tables as create_bbs_tables
from agent_bbs.subscriptions import create_subscription
from agent_runtime.config import generate_default_config
from agent_runtime.mcp_server import WMServer, _ensure_metadata_table
from agent_runtime.working_memory import create_working_memory_tables


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def connections():
    """Create fresh in-memory BBS and WM databases."""
    bbs_conn = sqlite3.connect(":memory:", check_same_thread=False)
    bbs_conn.execute("PRAGMA foreign_keys = ON")
    bbs_conn.row_factory = sqlite3.Row
    create_bbs_tables(bbs_conn)
    register_agent(bbs_conn, agent_id="test-agent", display_name="Test Agent")

    wm_conn = sqlite3.connect(":memory:", check_same_thread=False)
    wm_conn.row_factory = sqlite3.Row
    create_working_memory_tables(wm_conn)
    _ensure_metadata_table(wm_conn)

    yield bbs_conn, wm_conn

    bbs_conn.close()
    wm_conn.close()


@pytest.fixture()
def config():
    """Create a test agent config."""
    cfg = generate_default_config(agent_id="test-agent", display_name="Test Agent")
    cfg.watch_tags = ["test", "research"]
    return cfg


@pytest.fixture()
def server(connections, config):
    """Create a WMServer with fresh databases."""
    bbs_conn, wm_conn = connections
    return WMServer(bbs_conn, wm_conn, config)


def _call(server, method, params=None, id=1):
    """Send a JSON-RPC request and return the parsed response."""
    request = {"jsonrpc": "2.0", "method": method, "id": id}
    if params is not None:
        request["params"] = params
    return server.handle_request(request)


def _post_bbs_entry(bbs_conn, *, tags=None, content="Test entry", entry_type="finding"):
    """Helper to post an entry to the BBS."""
    return post_entry(
        bbs_conn,
        author_id="other-agent",
        entry_type=entry_type,
        performative="inform",
        content=content,
        tags=tags or ["test"],
    )


# ---------------------------------------------------------------------------
# Tool listing
# ---------------------------------------------------------------------------

class TestToolsListing:
    """All 7 tools must appear in tools/list response."""

    EXPECTED_TOOLS = {
        "wm_tick", "wm_bootstrap", "wm_get_summaries", "wm_upsert_summary",
        "wm_record_action", "wm_get_recent_actions", "wm_status",
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

    def test_tool_count(self, server):
        resp = _call(server, "tools/list")
        assert len(resp["result"]["tools"]) == 7


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------

class TestProtocol:
    """JSON-RPC 2.0 compliance."""

    def test_initialize(self, server):
        resp = _call(server, "initialize")
        result = resp["result"]
        assert result["serverInfo"]["name"] == "agent-wm"
        assert result["serverInfo"]["version"] == "2.0.0a1"
        assert "tools" in result["capabilities"]

    def test_response_has_jsonrpc_field(self, server):
        resp = _call(server, "initialize", id=42)
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 42

    def test_unknown_method(self, server):
        resp = _call(server, "nonexistent/method")
        assert "error" in resp
        assert resp["error"]["code"] == -32601

    def test_notifications_initialized(self, server):
        resp = _call(server, "notifications/initialized")
        assert resp["result"] == {}

    def test_unknown_tool(self, server):
        resp = _call(server, "tools/call", {"name": "wm_nonexistent", "arguments": {}})
        content = resp["result"]["content"][0]["text"]
        assert "Unknown tool" in content
        assert resp["result"]["isError"] is True


# ---------------------------------------------------------------------------
# wm_bootstrap
# ---------------------------------------------------------------------------

class TestBootstrap:
    """Cold-start bootstrap round-trip."""

    def test_bootstrap_empty_bbs(self, server):
        resp = _call(server, "tools/call", {"name": "wm_bootstrap", "arguments": {}})
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["entries_fetched"] == 0
        assert result["clusters_created"] == 0
        assert "test" in result["tags_searched"]

    def test_bootstrap_with_entries(self, server, connections):
        bbs_conn, _ = connections
        _post_bbs_entry(bbs_conn, tags=["test"], content="Test finding about things")
        _post_bbs_entry(bbs_conn, tags=["research"], content="Research finding")

        resp = _call(server, "tools/call", {"name": "wm_bootstrap", "arguments": {}})
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["entries_fetched"] >= 1
        assert result["clusters_created"] >= 1

    def test_bootstrap_sets_last_bootstrap_time(self, server, connections):
        _, wm_conn = connections
        _call(server, "tools/call", {"name": "wm_bootstrap", "arguments": {}})
        from agent_runtime.mcp_server import _get_metadata
        ts = _get_metadata(wm_conn, "last_bootstrap_time")
        assert ts is not None


# ---------------------------------------------------------------------------
# wm_tick
# ---------------------------------------------------------------------------

class TestTick:
    """Notification processing tick."""

    def test_tick_empty_inbox(self, server):
        resp = _call(server, "tools/call", {"name": "wm_tick", "arguments": {}})
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["status"] == "empty"

    def test_tick_with_notifications(self, server, connections):
        bbs_conn, _ = connections
        # Create subscription so notifications are generated
        create_subscription(
            bbs_conn, agent_id="test-agent",
            filter_tags=["test"], filter_directed=True,
        )
        # Post an entry that matches the subscription
        _post_bbs_entry(bbs_conn, tags=["test"], content="Something important")

        resp = _call(server, "tools/call", {"name": "wm_tick", "arguments": {}})
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result.get("type") == "context_assembly_request"
        assert result["notification_count"] >= 1
        assert len(result["fetch_entry_ids"]) >= 1

    def test_tick_advances_last_seen(self, server, connections):
        bbs_conn, wm_conn = connections
        create_subscription(
            bbs_conn, agent_id="test-agent",
            filter_tags=["test"], filter_directed=True,
        )
        _post_bbs_entry(bbs_conn, tags=["test"], content="Entry one")

        # First tick
        _call(server, "tools/call", {"name": "wm_tick", "arguments": {}})
        from agent_runtime.mcp_server import _get_metadata
        last_seen_1 = _get_metadata(wm_conn, "last_seen_entry_id")

        # Post more entries
        _post_bbs_entry(bbs_conn, tags=["test"], content="Entry two")

        # Second tick
        _call(server, "tools/call", {"name": "wm_tick", "arguments": {}})
        last_seen_2 = _get_metadata(wm_conn, "last_seen_entry_id")

        # last_seen should have advanced (or stayed same if no new notifs picked up)
        assert last_seen_1 is not None
        if last_seen_2 is not None:
            assert int(last_seen_2) >= int(last_seen_1)


# ---------------------------------------------------------------------------
# wm_upsert_summary
# ---------------------------------------------------------------------------

class TestUpsertSummary:
    """Create and update thread summaries."""

    def test_create_summary(self, server):
        resp = _call(server, "tools/call", {"name": "wm_upsert_summary", "arguments": {
            "cluster_tag": "quantization",
            "summary_text": "Research on quantization methods",
            "entry_ids": [1, 2, 3],
        }})
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["summary_id"] > 0
        assert result["cluster_tag"] == "quantization"
        assert result["entry_count"] == 3

    def test_update_summary(self, server):
        # Create
        _call(server, "tools/call", {"name": "wm_upsert_summary", "arguments": {
            "cluster_tag": "quantization",
            "summary_text": "Initial summary",
            "entry_ids": [1, 2],
        }})
        # Update
        resp = _call(server, "tools/call", {"name": "wm_upsert_summary", "arguments": {
            "cluster_tag": "quantization",
            "summary_text": "Updated summary with more detail",
            "entry_ids": [3, 4],
        }})
        result = json.loads(resp["result"]["content"][0]["text"])
        # Same summary_id, merged entry_ids
        assert result["entry_count"] == 2  # new entry_ids count

    def test_upsert_missing_fields(self, server):
        resp = _call(server, "tools/call", {"name": "wm_upsert_summary", "arguments": {
            "cluster_tag": "test",
        }})
        assert resp["result"]["isError"] is True
        assert "required" in resp["result"]["content"][0]["text"].lower() or \
               "validation" in resp["result"]["content"][0]["text"].lower()


# ---------------------------------------------------------------------------
# wm_get_summaries
# ---------------------------------------------------------------------------

class TestGetSummaries:
    """Retrieve summaries with and without FTS."""

    def test_get_summaries_empty(self, server):
        resp = _call(server, "tools/call", {"name": "wm_get_summaries", "arguments": {}})
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result == []

    def test_get_summaries_returns_created(self, server):
        # Create a summary
        _call(server, "tools/call", {"name": "wm_upsert_summary", "arguments": {
            "cluster_tag": "quantization",
            "summary_text": "Research on GPTQ and AWQ methods",
            "entry_ids": [1, 2],
        }})
        resp = _call(server, "tools/call", {"name": "wm_get_summaries", "arguments": {}})
        result = json.loads(resp["result"]["content"][0]["text"])
        assert len(result) == 1
        assert result[0]["cluster_tag"] == "quantization"
        assert "relevance" in result[0]

    def test_get_summaries_fts_query(self, server):
        _call(server, "tools/call", {"name": "wm_upsert_summary", "arguments": {
            "cluster_tag": "quantization",
            "summary_text": "Research on GPTQ and AWQ quantization methods",
            "entry_ids": [1],
        }})
        _call(server, "tools/call", {"name": "wm_upsert_summary", "arguments": {
            "cluster_tag": "spacemolt",
            "summary_text": "SpaceMolt game strategy for Voidborn pilots",
            "entry_ids": [2],
        }})

        # Search for quantization
        resp = _call(server, "tools/call", {"name": "wm_get_summaries", "arguments": {
            "query": "GPTQ",
        }})
        result = json.loads(resp["result"]["content"][0]["text"])
        assert len(result) >= 1
        assert any("quantization" in s["cluster_tag"] for s in result)


# ---------------------------------------------------------------------------
# wm_record_action + wm_get_recent_actions
# ---------------------------------------------------------------------------

class TestActions:
    """Action recording and retrieval round-trip."""

    def test_record_action(self, server):
        resp = _call(server, "tools/call", {"name": "wm_record_action", "arguments": {
            "action_type": "post",
            "bbs_entry_id": 42,
            "payload": {"content": "test post"},
        }})
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["action_id"] > 0

    def test_get_recent_actions(self, server):
        _call(server, "tools/call", {"name": "wm_record_action", "arguments": {
            "action_type": "post", "bbs_entry_id": 1,
        }})
        _call(server, "tools/call", {"name": "wm_record_action", "arguments": {
            "action_type": "search", "payload": {"query": "quantization"},
        }})

        resp = _call(server, "tools/call", {"name": "wm_get_recent_actions", "arguments": {}})
        result = json.loads(resp["result"]["content"][0]["text"])
        assert len(result) == 2

    def test_filter_actions_by_type(self, server):
        _call(server, "tools/call", {"name": "wm_record_action", "arguments": {
            "action_type": "post",
        }})
        _call(server, "tools/call", {"name": "wm_record_action", "arguments": {
            "action_type": "search",
        }})

        resp = _call(server, "tools/call", {"name": "wm_get_recent_actions", "arguments": {
            "action_type": "post",
        }})
        result = json.loads(resp["result"]["content"][0]["text"])
        assert len(result) == 1
        assert result[0]["action_type"] == "post"

    def test_invalid_action_type(self, server):
        resp = _call(server, "tools/call", {"name": "wm_record_action", "arguments": {
            "action_type": "invalid_type",
        }})
        assert resp["result"]["isError"] is True

    def test_missing_action_type(self, server):
        resp = _call(server, "tools/call", {"name": "wm_record_action", "arguments": {}})
        assert resp["result"]["isError"] is True


# ---------------------------------------------------------------------------
# wm_status
# ---------------------------------------------------------------------------

class TestStatus:
    """Status dashboard."""

    def test_status_empty(self, server):
        resp = _call(server, "tools/call", {"name": "wm_status", "arguments": {}})
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["agent_id"] == "test-agent"
        assert result["pending_notifications"] == 0
        assert result["summary_count"] == 0
        assert result["action_count"] == 0
        assert result["event_count"] == 0
        assert result["last_seen_entry_id"] is None
        assert result["last_bootstrap_time"] is None

    def test_status_after_activity(self, server):
        # Create a summary and an action
        _call(server, "tools/call", {"name": "wm_upsert_summary", "arguments": {
            "cluster_tag": "test",
            "summary_text": "test summary",
            "entry_ids": [1],
        }})
        _call(server, "tools/call", {"name": "wm_record_action", "arguments": {
            "action_type": "post",
        }})

        resp = _call(server, "tools/call", {"name": "wm_status", "arguments": {}})
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["summary_count"] == 1
        assert result["action_count"] == 1
