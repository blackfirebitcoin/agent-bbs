"""Tests for the Subscribe operation (Phase 2).

Spec reference: Sections 4.4, 7.5
- Subscription creation
- OR within filter dimensions, AND across dimensions
- Empty arrays match everything for that dimension
- filter_directed catches entries in directed_to
"""

import pytest

from agent_bbs.agents import register_agent
from agent_bbs.entries import post_entry
from agent_bbs.subscriptions import create_subscription, _subscription_matches

import json


def _reg(db, agent_id):
    register_agent(db, agent_id=agent_id, display_name=agent_id)


def _make_sub_row(filter_tags=None, filter_types=None, filter_perfs=None,
                  filter_authors=None, filter_directed=True, agent_id="w"):
    """Create a fake subscription row dict for unit-testing _subscription_matches."""
    return {
        "agent_id": agent_id,
        "filter_tags": json.dumps(filter_tags or []),
        "filter_types": json.dumps(filter_types or []),
        "filter_perfs": json.dumps(filter_perfs or []),
        "filter_authors": json.dumps(filter_authors or []),
        "filter_directed": filter_directed,
        "id": 1,
    }


class TestSubscriptionCreation:
    """Basic subscription CRUD."""

    def test_create_returns_id(self, db_with_schema):
        _reg(db_with_schema, "watcher")
        result = create_subscription(
            db_with_schema, agent_id="watcher",
            filter_tags=["ai"], filter_types=["finding"],
        )
        assert "id" in result
        assert result["agent_id"] == "watcher"

    def test_subscription_stored_in_db(self, db_with_schema):
        _reg(db_with_schema, "watcher")
        create_subscription(
            db_with_schema, agent_id="watcher",
            filter_tags=["ai"], filter_types=["finding"],
        )
        row = db_with_schema.execute(
            "SELECT * FROM subscriptions WHERE agent_id = 'watcher'"
        ).fetchone()
        assert row is not None
        assert json.loads(row["filter_tags"]) == ["ai"]


class TestORWithinDimension:
    """OR semantics within each filter dimension."""

    def test_tag_or_match(self):
        """Entry with tag 'ai' matches filter_tags=['ai','bio']."""
        sub = _make_sub_row(filter_tags=["ai", "bio"])
        assert _subscription_matches(sub, {"ai"}, "finding", "inform", "x", set())

    def test_tag_or_no_match(self):
        """Entry with tag 'physics' does NOT match filter_tags=['ai','bio']."""
        sub = _make_sub_row(filter_tags=["ai", "bio"])
        assert not _subscription_matches(sub, {"physics"}, "finding", "inform", "x", set())

    def test_type_or_match(self):
        """Entry type 'question' matches filter_types=['finding','question']."""
        sub = _make_sub_row(filter_types=["finding", "question"])
        assert _subscription_matches(sub, set(), "question", "inform", "x", set())

    def test_perf_or_match(self):
        """Performative 'query' matches filter_perfs=['inform','query']."""
        sub = _make_sub_row(filter_perfs=["inform", "query"])
        assert _subscription_matches(sub, set(), "finding", "query", "x", set())

    def test_author_or_match(self):
        """Author 'bob' matches filter_authors=['alice','bob']."""
        sub = _make_sub_row(filter_authors=["alice", "bob"])
        assert _subscription_matches(sub, set(), "finding", "inform", "bob", set())


class TestANDAcrossDimensions:
    """AND semantics across filter dimensions."""

    def test_all_dimensions_must_match(self):
        """Tag matches but type doesn't → no match."""
        sub = _make_sub_row(filter_tags=["ai"], filter_types=["question"])
        # entry_type is "finding" but filter_types wants "question"
        assert not _subscription_matches(sub, {"ai"}, "finding", "inform", "x", set())

    def test_all_dimensions_match(self):
        """All dimensions match → match."""
        sub = _make_sub_row(
            filter_tags=["ai"],
            filter_types=["finding"],
            filter_perfs=["inform"],
            filter_authors=["alice"],
        )
        assert _subscription_matches(sub, {"ai"}, "finding", "inform", "alice", set())

    def test_one_dimension_fails(self):
        """Everything matches except author → no match."""
        sub = _make_sub_row(
            filter_tags=["ai"],
            filter_types=["finding"],
            filter_perfs=["inform"],
            filter_authors=["alice"],
        )
        assert not _subscription_matches(sub, {"ai"}, "finding", "inform", "bob", set())


class TestEmptyArraysMatchAll:
    """Empty filter arrays match everything for that dimension."""

    def test_empty_tags_matches_any_tag(self):
        sub = _make_sub_row(filter_tags=[], filter_types=["finding"])
        assert _subscription_matches(sub, {"anything"}, "finding", "inform", "x", set())

    def test_empty_types_matches_any_type(self):
        sub = _make_sub_row(filter_tags=["ai"], filter_types=[])
        assert _subscription_matches(sub, {"ai"}, "task", "request", "x", set())

    def test_all_empty_matches_everything(self):
        sub = _make_sub_row()
        assert _subscription_matches(sub, {"whatever"}, "task", "request", "anyone", set())


class TestFilterDirected:
    """filter_directed catches entries in directed_to list."""

    def test_directed_entry_triggers_notification(self, db_with_schema):
        """An entry directed to an agent should trigger a notification
        even without explicit subscription matching, if agent has a
        subscription with filter_directed=True."""
        _reg(db_with_schema, "watcher")
        _reg(db_with_schema, "poster")
        create_subscription(
            db_with_schema, agent_id="watcher",
            filter_tags=["ai"], filter_types=[], filter_perfs=[],
            filter_authors=[],
        )
        # Post entry directed to watcher with matching tag
        post_entry(db_with_schema, author_id="poster", entry_type="finding",
                   performative="inform", content="for you",
                   tags=["ai"], directed_to=["watcher"])
        notifs = db_with_schema.execute(
            "SELECT * FROM notification_queue WHERE agent_id = 'watcher'"
        ).fetchall()
        assert len(notifs) >= 1


class TestIntegrationSubscriptionNotification:
    """End-to-end: subscription + post → notification enqueued."""

    def test_multi_subscription_match(self, db_with_schema):
        _reg(db_with_schema, "w1")
        _reg(db_with_schema, "w2")
        create_subscription(db_with_schema, agent_id="w1",
                            filter_tags=["ai"])
        create_subscription(db_with_schema, agent_id="w2",
                            filter_types=["finding"])

        post_entry(db_with_schema, author_id="poster", entry_type="finding",
                   performative="inform", content="AI finding", tags=["ai"])

        n1 = db_with_schema.execute(
            "SELECT * FROM notification_queue WHERE agent_id = 'w1'"
        ).fetchall()
        n2 = db_with_schema.execute(
            "SELECT * FROM notification_queue WHERE agent_id = 'w2'"
        ).fetchall()
        assert len(n1) == 1
        assert len(n2) == 1
