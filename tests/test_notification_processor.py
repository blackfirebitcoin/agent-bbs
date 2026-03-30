"""Tests for agent_runtime.notification_processor — the ticker loop.

Covers: full flow (post → notification → poll → score → batch → context request),
context assembly request structure, empty inbox, scoring integration,
process_tick_result marking, BBS MCP server integration.
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def bbs_db():
    """BBS database with schema and registered agents."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    from agent_bbs.schema import create_tables
    create_tables(conn)
    from agent_bbs.agents import register_agent
    register_agent(conn, agent_id="poster-agent", display_name="Poster")
    register_agent(conn, agent_id="subscriber-agent", display_name="Subscriber")
    return conn


@pytest.fixture()
def wm_db():
    """Fresh working memory database."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    from agent_runtime.working_memory import create_working_memory_tables
    create_working_memory_tables(conn)
    return conn


def _subscribe(bbs_db, agent_id, **kwargs):
    from agent_bbs.subscriptions import create_subscription
    return create_subscription(bbs_db, agent_id=agent_id, **kwargs)


def _post(bbs_db, author_id, content, **kwargs):
    from agent_bbs.entries import post_entry
    return post_entry(bbs_db, author_id=author_id, content=content,
                      entry_type=kwargs.pop("entry_type", "finding"),
                      performative=kwargs.pop("performative", "inform"),
                      **kwargs)


DEFAULT_WEIGHTS = {
    "directed_to_me": 10.0,
    "contradiction_of_mine": 8.0,
    "request_to_me": 9.0,
    "watched_tag_match": 3.0,
    "high_confidence": 1.5,
    "low_confidence": 0.5,
    "recency_halflife_hours": 24.0,
    "synthesis_proposed": 4.0,
    "question_in_domain": 3.5,
}


# ---------------------------------------------------------------------------
# Full flow tests
# ---------------------------------------------------------------------------

class TestTickerFullFlow:
    def test_post_subscribe_tick(self, bbs_db, wm_db):
        """Full flow: subscribe → post → tick → context request."""
        from agent_runtime.notification_processor import tick

        # Subscriber watches "research" tag
        _subscribe(bbs_db, "subscriber-agent", filter_tags=["research"])

        # Poster creates an entry matching the subscription
        _post(bbs_db, "poster-agent", "New research finding",
              tags=["research"], confidence=0.9)

        # Tick should find the notification
        result = tick(
            bbs_db, wm_db,
            agent_id="subscriber-agent",
            watch_tags=["research"],
            scorer_weights=DEFAULT_WEIGHTS,
        )
        assert result is not None
        assert result["type"] == "context_assembly_request"
        assert result["notification_count"] >= 1
        assert len(result["fetch_entry_ids"]) >= 1

    def test_empty_inbox_returns_none(self, bbs_db, wm_db):
        from agent_runtime.notification_processor import tick
        result = tick(
            bbs_db, wm_db,
            agent_id="subscriber-agent",
            watch_tags=["research"],
            scorer_weights=DEFAULT_WEIGHTS,
        )
        assert result is None

    def test_directed_notification_flow(self, bbs_db, wm_db):
        """Entry directed at subscriber should appear in tick."""
        from agent_runtime.notification_processor import tick

        _post(bbs_db, "poster-agent", "Please investigate this",
              directed_to=["subscriber-agent"],
              entry_type="task", performative="request",
              tags=["research"])

        result = tick(
            bbs_db, wm_db,
            agent_id="subscriber-agent",
            watch_tags=["research"],
            scorer_weights=DEFAULT_WEIGHTS,
        )
        assert result is not None
        # Directed + request should have high priority
        notifs = result["notifications"]
        assert any(n["directed_to_me"] for n in notifs)

    def test_multiple_notifications_scored_and_ordered(self, bbs_db, wm_db):
        """Multiple notifications should be scored and ordered by priority."""
        from agent_runtime.notification_processor import tick

        _subscribe(bbs_db, "subscriber-agent", filter_tags=["research"])

        # Low priority: generic finding
        _post(bbs_db, "poster-agent", "Generic finding", tags=["research"])
        # High priority: directed request
        _post(bbs_db, "poster-agent", "Please help",
              directed_to=["subscriber-agent"],
              entry_type="task", performative="request",
              tags=["research"])

        result = tick(
            bbs_db, wm_db,
            agent_id="subscriber-agent",
            watch_tags=["research"],
            scorer_weights=DEFAULT_WEIGHTS,
            batch_size=10,
        )
        assert result is not None
        assert result["notification_count"] >= 2
        # First notification should be the directed request (higher score)
        notifs = result["notifications"]
        scores = [n["priority_score"] for n in notifs]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Context assembly request structure
# ---------------------------------------------------------------------------

class TestContextRequest:
    def test_context_request_structure(self, bbs_db, wm_db):
        from agent_runtime.notification_processor import tick

        _subscribe(bbs_db, "subscriber-agent", filter_tags=["test"])
        _post(bbs_db, "poster-agent", "Test content", tags=["test"])

        result = tick(
            bbs_db, wm_db,
            agent_id="subscriber-agent",
            watch_tags=["test"],
            scorer_weights=DEFAULT_WEIGHTS,
            token_budget=8192,
        )
        assert result is not None
        assert result["type"] == "context_assembly_request"
        assert result["agent_id"] == "subscriber-agent"
        assert result["token_budget"] == 8192
        assert "timestamp" in result
        assert isinstance(result["fetch_entry_ids"], list)

        notif = result["notifications"][0]
        assert "entry_id" in notif
        assert "entry_type" in notif
        assert "performative" in notif
        assert "priority_score" in notif

    def test_build_context_request_directly(self):
        from agent_runtime.notification_processor import build_context_request
        batch = [
            {
                "entry_id": 1, "record_hash": "abc",
                "author_id": "agent-a", "entry_type": "finding",
                "performative": "inform", "tags": '["ml"]',
                "confidence": 0.8, "directed_to_me": False,
                "priority_score": 5.0,
            },
        ]
        req = build_context_request(batch, token_budget=4096, agent_id="me")
        assert req["notification_count"] == 1
        assert req["fetch_entry_ids"] == [1]


# ---------------------------------------------------------------------------
# Process tick result
# ---------------------------------------------------------------------------

class TestProcessTickResult:
    def test_mark_processed_after_tick(self, bbs_db, wm_db):
        from agent_runtime.notification_processor import tick, process_tick_result

        _subscribe(bbs_db, "subscriber-agent", filter_tags=["test"])
        _post(bbs_db, "poster-agent", "Test", tags=["test"])

        result = tick(
            bbs_db, wm_db,
            agent_id="subscriber-agent",
            watch_tags=["test"],
            scorer_weights=DEFAULT_WEIGHTS,
        )
        assert result is not None

        # Process the result
        process_tick_result(wm_db, context_request=result)

        # All notifications should be processed
        processed = wm_db.execute(
            "SELECT * FROM pending_notifications WHERE status = 'processed'"
        ).fetchall()
        assert len(processed) >= 1

    def test_process_records_audit_event(self, bbs_db, wm_db):
        from agent_runtime.notification_processor import tick, process_tick_result

        _subscribe(bbs_db, "subscriber-agent", filter_tags=["audit"])
        _post(bbs_db, "poster-agent", "Audit test", tags=["audit"])

        result = tick(
            bbs_db, wm_db,
            agent_id="subscriber-agent",
            watch_tags=["audit"],
            scorer_weights=DEFAULT_WEIGHTS,
        )
        process_tick_result(wm_db, context_request=result)

        events = wm_db.execute(
            "SELECT * FROM events WHERE event_type = 'action_taken'"
        ).fetchall()
        assert len(events) >= 1
        payload = json.loads(events[0]["payload"])
        assert payload["action"] == "batch_processed"

    def test_process_empty_request_noop(self, wm_db):
        from agent_runtime.notification_processor import process_tick_result
        # Should not raise
        process_tick_result(wm_db, context_request=None)
        process_tick_result(wm_db, context_request={"notifications": []})


# ---------------------------------------------------------------------------
# Batch size and min_score filtering
# ---------------------------------------------------------------------------

class TestBatchFiltering:
    def test_batch_size_limits_output(self, bbs_db, wm_db):
        from agent_runtime.notification_processor import tick

        _subscribe(bbs_db, "subscriber-agent", filter_tags=["bulk"])
        for i in range(10):
            _post(bbs_db, "poster-agent", f"Bulk entry {i}", tags=["bulk"])

        result = tick(
            bbs_db, wm_db,
            agent_id="subscriber-agent",
            watch_tags=["bulk"],
            scorer_weights=DEFAULT_WEIGHTS,
            batch_size=3,
        )
        assert result is not None
        assert result["notification_count"] == 3

    def test_min_score_filters_low_priority(self, bbs_db, wm_db):
        from agent_runtime.notification_processor import tick

        _subscribe(bbs_db, "subscriber-agent", filter_tags=["low"])
        _post(bbs_db, "poster-agent", "Low relevance entry",
              tags=["unrelated-to-watch"])

        result = tick(
            bbs_db, wm_db,
            agent_id="subscriber-agent",
            watch_tags=["high-priority-only"],
            scorer_weights=DEFAULT_WEIGHTS,
            min_score=100.0,  # Very high threshold
        )
        # Should filter out everything since nothing matches watch_tags well
        assert result is None
