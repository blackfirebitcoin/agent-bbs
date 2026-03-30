"""Tests for agent_runtime.working_memory — schema, priority scoring, relevance decay.

Covers: table creation, event recording, pending notification storage,
scoring, batching, thread summaries, FTS on summaries, actions audit,
compaction, relevance decay computation.
"""

import json
import sqlite3
from datetime import datetime, timezone, timedelta

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def wm_db():
    """Fresh in-memory working memory database."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    from agent_runtime.working_memory import create_working_memory_tables
    create_working_memory_tables(conn)
    yield conn
    conn.close()


def _make_notification(**overrides):
    """Create a notification dict with sensible defaults."""
    base = {
        "notification_id": 1,
        "entry_id": 100,
        "record_hash": "abc123",
        "author_id": "other-agent",
        "entry_type": "finding",
        "performative": "inform",
        "tags": '["research", "ml"]',
        "confidence": 0.8,
        "directed_to_me": False,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

class TestWorkingMemorySchema:
    def test_tables_created(self, wm_db):
        tables = [r[0] for r in wm_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        assert "events" in tables
        assert "pending_notifications" in tables
        assert "thread_summaries" in tables
        assert "agent_actions" in tables

    def test_fts_table_created(self, wm_db):
        tables = [r[0] for r in wm_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%fts%'"
        ).fetchall()]
        assert any("summaries_fts" in t for t in tables)

    def test_event_type_check_constraint(self, wm_db):
        with pytest.raises(sqlite3.IntegrityError):
            wm_db.execute(
                "INSERT INTO events (event_type, source, created_at) VALUES (?, ?, ?)",
                ("invalid_type", "test", "2025-01-01T00:00:00Z"),
            )

    def test_notification_status_check(self, wm_db):
        with pytest.raises(sqlite3.IntegrityError):
            wm_db.execute(
                "INSERT INTO pending_notifications "
                "(entry_id, author_id, entry_type, performative, created_at, received_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, "a", "finding", "inform", "2025-01-01T00:00:00Z", "2025-01-01T00:00:00Z", "invalid"),
            )

    def test_action_type_check(self, wm_db):
        with pytest.raises(sqlite3.IntegrityError):
            wm_db.execute(
                "INSERT INTO agent_actions (action_type, created_at) VALUES (?, ?)",
                ("invalid_action", "2025-01-01T00:00:00Z"),
            )


# ---------------------------------------------------------------------------
# Event recording
# ---------------------------------------------------------------------------

class TestEvents:
    def test_record_event(self, wm_db):
        from agent_runtime.working_memory import record_event
        eid = record_event(
            wm_db, event_type="notification_received",
            source="bbs_notification", entry_id=42,
        )
        assert eid > 0
        row = wm_db.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
        assert row["event_type"] == "notification_received"
        assert row["entry_id"] == 42

    def test_record_event_with_payload(self, wm_db):
        from agent_runtime.working_memory import record_event
        eid = record_event(
            wm_db, event_type="system_event", source="test",
            payload={"key": "value"},
        )
        row = wm_db.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
        assert json.loads(row["payload"]) == {"key": "value"}


# ---------------------------------------------------------------------------
# Pending notifications
# ---------------------------------------------------------------------------

class TestPendingNotifications:
    def test_store_notification(self, wm_db):
        from agent_runtime.working_memory import store_pending_notification
        notif = _make_notification()
        lid = store_pending_notification(wm_db, notification=notif)
        assert lid > 0
        row = wm_db.execute("SELECT * FROM pending_notifications WHERE id = ?", (lid,)).fetchone()
        assert row["entry_id"] == 100
        assert row["status"] == "pending"

    def test_duplicate_bbs_notification_ignored(self, wm_db):
        from agent_runtime.working_memory import store_pending_notification
        notif = _make_notification(notification_id=42)
        lid1 = store_pending_notification(wm_db, notification=notif)
        lid2 = store_pending_notification(wm_db, notification=notif)
        # Second insert ignored (INSERT OR IGNORE)
        count = wm_db.execute("SELECT count(*) FROM pending_notifications").fetchone()[0]
        assert count == 1

    def test_store_notification_with_list_tags(self, wm_db):
        from agent_runtime.working_memory import store_pending_notification
        notif = _make_notification(tags=["research", "ml"])
        lid = store_pending_notification(wm_db, notification=notif)
        row = wm_db.execute("SELECT tags FROM pending_notifications WHERE id = ?", (lid,)).fetchone()
        assert json.loads(row["tags"]) == ["research", "ml"]


# ---------------------------------------------------------------------------
# Priority scoring
# ---------------------------------------------------------------------------

class TestPriorityScoring:
    def test_directed_to_me_highest(self):
        from agent_runtime.working_memory import compute_priority_score
        weights = {"directed_to_me": 10.0, "recency_halflife_hours": 24.0}
        notif = _make_notification(directed_to_me=True)
        score = compute_priority_score(notif, agent_id="me", watch_tags=[], weights=weights)
        assert score > 5.0

    def test_request_to_me_adds_both_weights(self):
        from agent_runtime.working_memory import compute_priority_score
        weights = {
            "directed_to_me": 10.0, "request_to_me": 9.0,
            "recency_halflife_hours": 24.0,
        }
        notif = _make_notification(directed_to_me=True, performative="request")
        score = compute_priority_score(notif, agent_id="me", watch_tags=[], weights=weights)
        # Should include both directed_to_me and request_to_me
        assert score > 15.0

    def test_watched_tag_match(self):
        from agent_runtime.working_memory import compute_priority_score
        weights = {"watched_tag_match": 3.0, "recency_halflife_hours": 24.0}
        notif = _make_notification(tags='["research", "ml"]')
        score = compute_priority_score(
            notif, agent_id="me", watch_tags=["research", "ml"], weights=weights
        )
        assert score > 0

    def test_no_match_zero_score(self):
        from agent_runtime.working_memory import compute_priority_score
        weights = {"recency_halflife_hours": 24.0}
        notif = _make_notification(
            directed_to_me=False, tags='["unrelated"]',
            performative="inform", entry_type="finding",
        )
        score = compute_priority_score(notif, agent_id="me", watch_tags=[], weights=weights)
        assert score == 0.0

    def test_high_confidence_multiplier(self):
        from agent_runtime.working_memory import compute_priority_score
        weights = {
            "watched_tag_match": 3.0, "high_confidence": 2.0,
            "recency_halflife_hours": 24.0,
        }
        notif_high = _make_notification(confidence=0.9, tags='["research"]')
        notif_low = _make_notification(confidence=0.3, tags='["research"]')
        score_high = compute_priority_score(
            notif_high, agent_id="me", watch_tags=["research"], weights=weights
        )
        score_low = compute_priority_score(
            notif_low, agent_id="me", watch_tags=["research"], weights=weights
        )
        assert score_high > score_low

    def test_disconfirm_scores_high(self):
        from agent_runtime.working_memory import compute_priority_score
        weights = {"contradiction_of_mine": 8.0, "recency_halflife_hours": 24.0}
        notif = _make_notification(performative="disconfirm")
        score = compute_priority_score(notif, agent_id="me", watch_tags=[], weights=weights)
        assert score > 5.0

    def test_synthesis_proposed_scores(self):
        from agent_runtime.working_memory import compute_priority_score
        weights = {"synthesis_proposed": 4.0, "recency_halflife_hours": 24.0}
        notif = _make_notification(entry_type="synthesis", performative="propose")
        score = compute_priority_score(notif, agent_id="me", watch_tags=[], weights=weights)
        assert score > 0

    def test_question_in_domain(self):
        from agent_runtime.working_memory import compute_priority_score
        weights = {"question_in_domain": 3.5, "recency_halflife_hours": 24.0}
        notif = _make_notification(entry_type="question", tags='["research"]')
        score = compute_priority_score(
            notif, agent_id="me", watch_tags=["research"], weights=weights
        )
        assert score > 0

    def test_time_decay_reduces_old_score(self):
        from agent_runtime.working_memory import compute_priority_score
        weights = {"directed_to_me": 10.0, "recency_halflife_hours": 1.0}  # 1 hour halflife
        recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        old = (datetime.now(timezone.utc) - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        notif_recent = _make_notification(directed_to_me=True, created_at=recent)
        notif_old = _make_notification(directed_to_me=True, created_at=old)
        score_recent = compute_priority_score(notif_recent, agent_id="me", watch_tags=[], weights=weights)
        score_old = compute_priority_score(notif_old, agent_id="me", watch_tags=[], weights=weights)
        assert score_recent > score_old


# ---------------------------------------------------------------------------
# Scoring + batching in DB
# ---------------------------------------------------------------------------

class TestScoringAndBatching:
    def test_score_pending_notifications(self, wm_db):
        from agent_runtime.working_memory import (
            store_pending_notification, score_pending_notifications,
        )
        for i in range(3):
            store_pending_notification(wm_db, notification=_make_notification(
                notification_id=i + 1, entry_id=100 + i,
            ))
        count = score_pending_notifications(
            wm_db, agent_id="me", watch_tags=["research"],
            weights={"watched_tag_match": 3.0, "recency_halflife_hours": 24.0},
        )
        assert count == 3
        scored = wm_db.execute(
            "SELECT * FROM pending_notifications WHERE status = 'scored'"
        ).fetchall()
        assert len(scored) == 3

    def test_get_scored_batch_ordered_by_priority(self, wm_db):
        from agent_runtime.working_memory import (
            store_pending_notification, score_pending_notifications, get_scored_batch,
        )
        # Store notifications with different priority signals
        store_pending_notification(wm_db, notification=_make_notification(
            notification_id=1, entry_id=101, directed_to_me=True,
        ))
        store_pending_notification(wm_db, notification=_make_notification(
            notification_id=2, entry_id=102, directed_to_me=False,
        ))
        score_pending_notifications(
            wm_db, agent_id="me", watch_tags=["research"],
            weights={"directed_to_me": 10.0, "watched_tag_match": 3.0, "recency_halflife_hours": 24.0},
        )
        batch = get_scored_batch(wm_db, limit=10)
        assert len(batch) == 2
        # First should be the directed one (higher score)
        assert batch[0]["entry_id"] == 101
        # Both should now be 'batched'
        statuses = [r["status"] for r in wm_db.execute(
            "SELECT status FROM pending_notifications"
        ).fetchall()]
        assert all(s == "batched" for s in statuses)

    def test_mark_processed(self, wm_db):
        from agent_runtime.working_memory import (
            store_pending_notification, score_pending_notifications,
            get_scored_batch, mark_notifications_processed,
        )
        store_pending_notification(wm_db, notification=_make_notification(
            notification_id=1, entry_id=101,
        ))
        score_pending_notifications(
            wm_db, agent_id="me", watch_tags=[],
            weights={"recency_halflife_hours": 24.0},
        )
        batch = get_scored_batch(wm_db, limit=10)
        mark_notifications_processed(wm_db, notification_ids=[b["id"] for b in batch])
        row = wm_db.execute("SELECT status FROM pending_notifications").fetchone()
        assert row["status"] == "processed"


# ---------------------------------------------------------------------------
# Thread summaries
# ---------------------------------------------------------------------------

class TestThreadSummaries:
    def test_create_summary(self, wm_db):
        from agent_runtime.working_memory import upsert_thread_summary
        sid = upsert_thread_summary(
            wm_db, cluster_tag="ml", summary_text="ML findings summary",
            entry_ids=[1, 2, 3],
        )
        assert sid > 0
        row = wm_db.execute("SELECT * FROM thread_summaries WHERE id = ?", (sid,)).fetchone()
        assert row["cluster_tag"] == "ml"
        assert json.loads(row["entry_ids"]) == [1, 2, 3]
        assert row["entry_count"] == 3

    def test_update_summary_merges_entry_ids(self, wm_db):
        from agent_runtime.working_memory import upsert_thread_summary
        upsert_thread_summary(wm_db, cluster_tag="ml", summary_text="v1", entry_ids=[1, 2])
        upsert_thread_summary(wm_db, cluster_tag="ml", summary_text="v2", entry_ids=[2, 3, 4])
        row = wm_db.execute("SELECT * FROM thread_summaries WHERE cluster_tag = 'ml'").fetchone()
        assert row["summary_text"] == "v2"
        assert json.loads(row["entry_ids"]) == [1, 2, 3, 4]  # merged + sorted
        assert row["entry_count"] == 4

    def test_fts_search_summaries(self, wm_db):
        from agent_runtime.working_memory import upsert_thread_summary
        upsert_thread_summary(
            wm_db, cluster_tag="quantum", summary_text="Quantum computing advances in 2025",
            entry_ids=[10],
        )
        upsert_thread_summary(
            wm_db, cluster_tag="ml", summary_text="Machine learning model architectures",
            entry_ids=[20],
        )
        results = wm_db.execute(
            "SELECT cluster_tag FROM summaries_fts WHERE summaries_fts MATCH 'quantum'"
        ).fetchall()
        assert len(results) == 1
        assert results[0]["cluster_tag"] == "quantum"

    def test_touch_summary_increments_access(self, wm_db):
        from agent_runtime.working_memory import upsert_thread_summary, touch_summary
        sid = upsert_thread_summary(
            wm_db, cluster_tag="ml", summary_text="test", entry_ids=[1],
        )
        initial = wm_db.execute("SELECT access_count FROM thread_summaries WHERE id = ?", (sid,)).fetchone()
        touch_summary(wm_db, summary_id=sid)
        after = wm_db.execute("SELECT access_count FROM thread_summaries WHERE id = ?", (sid,)).fetchone()
        assert after["access_count"] == initial["access_count"] + 1


# ---------------------------------------------------------------------------
# Relevance decay
# ---------------------------------------------------------------------------

class TestRelevanceDecay:
    def test_recent_summary_high_relevance(self):
        from agent_runtime.working_memory import compute_relevance
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = {"updated_at": now, "access_count": 1}
        relevance = compute_relevance(summary)
        assert relevance > 0.9

    def test_old_summary_low_relevance(self):
        from agent_runtime.working_memory import compute_relevance
        old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = {"updated_at": old, "access_count": 1}
        relevance = compute_relevance(summary)
        assert relevance < 0.5

    def test_access_boost_increases_relevance(self):
        from agent_runtime.working_memory import compute_relevance
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        low_access = {"updated_at": now, "access_count": 1}
        high_access = {"updated_at": now, "access_count": 20}
        r_low = compute_relevance(low_access)
        r_high = compute_relevance(high_access)
        assert r_high > r_low


# ---------------------------------------------------------------------------
# Agent actions audit trail
# ---------------------------------------------------------------------------

class TestAgentActions:
    def test_record_action(self, wm_db):
        from agent_runtime.working_memory import record_action
        aid = record_action(
            wm_db, action_type="post", bbs_entry_id=42,
            record_hash="abc", payload={"content": "test"},
        )
        assert aid > 0
        row = wm_db.execute("SELECT * FROM agent_actions WHERE id = ?", (aid,)).fetchone()
        assert row["action_type"] == "post"
        assert row["bbs_entry_id"] == 42


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------

class TestCompaction:
    def test_compact_old_events(self, wm_db):
        from agent_runtime.working_memory import record_event, compact_events
        # Insert an old event manually
        old_time = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
        wm_db.execute(
            "INSERT INTO events (event_type, source, created_at) VALUES (?, ?, ?)",
            ("notification_received", "test", old_time),
        )
        wm_db.commit()
        # Insert a recent event
        record_event(wm_db, event_type="notification_received", source="test")
        count = compact_events(wm_db, older_than_days=90)
        assert count == 1
        # Recent event not compacted
        recent = wm_db.execute(
            "SELECT compacted FROM events WHERE compacted = FALSE"
        ).fetchall()
        assert len(recent) == 1

    def test_audit_events_not_compacted(self, wm_db):
        old_time = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
        wm_db.execute(
            "INSERT INTO events (event_type, source, created_at) VALUES (?, ?, ?)",
            ("action_taken", "test", old_time),
        )
        wm_db.execute(
            "INSERT INTO events (event_type, source, created_at) VALUES (?, ?, ?)",
            ("system_event", "test", old_time),
        )
        wm_db.commit()
        from agent_runtime.working_memory import compact_events
        count = compact_events(wm_db, older_than_days=90)
        assert count == 0  # audit events retained
