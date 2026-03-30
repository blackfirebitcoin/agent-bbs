"""Tests for the Notify operation and notification queue state machine (Phase 2).

Spec reference: Sections 7.6, 8, 4.5

Notify operation:
- Returns metadata only (no content field)
- mark_delivered transitions status
- since filter
- limit
- Notifications enqueued when entries match subscriptions

Queue state machine:
- pending → leased → delivered
- pending → leased → failed → pending (retry, incremented attempt_count)
- pending → expired when past expires_at
"""

import pytest

from agent_bbs.agents import register_agent
from agent_bbs.entries import post_entry
from agent_bbs.notifications import (
    get_notifications,
    lease_notifications,
    mark_delivered,
    mark_failed,
    expire_notifications,
)
from agent_bbs.subscriptions import create_subscription


def _setup_with_notification(db):
    """Create an agent, subscription, and a matching post → notification."""
    register_agent(db, agent_id="watcher", display_name="Watcher")
    create_subscription(db, agent_id="watcher", filter_tags=["ai"])
    entry = post_entry(db, author_id="poster", entry_type="finding",
                       performative="inform", content="AI news", tags=["ai"])
    return entry


# ---------------------------------------------------------------------------
# Notify operation
# ---------------------------------------------------------------------------

class TestNotifyMetadataOnly:
    """Notifications return metadata only — no content field."""

    def test_no_content_in_notification(self, db_with_schema):
        _setup_with_notification(db_with_schema)
        notifs = get_notifications(db_with_schema, agent_id="watcher")
        assert len(notifs) == 1
        n = notifs[0]
        assert "content" not in n
        # But should have metadata fields
        assert "entry_id" in n
        assert "record_hash" in n
        assert "author_id" in n
        assert "entry_type" in n
        assert "performative" in n
        assert "tags" in n
        assert "confidence" in n
        assert "created_at" in n

    def test_directed_to_me_field(self, db_with_schema):
        register_agent(db_with_schema, agent_id="target", display_name="T")
        create_subscription(db_with_schema, agent_id="target", filter_tags=["ai"])
        post_entry(db_with_schema, author_id="poster", entry_type="finding",
                   performative="inform", content="directed msg",
                   tags=["ai"], directed_to=["target"])
        notifs = get_notifications(db_with_schema, agent_id="target")
        assert len(notifs) >= 1
        assert notifs[0]["directed_to_me"] is True


class TestNotifyMarkDelivered:
    """mark_delivered transitions status from pending/leased to delivered."""

    def test_mark_delivered(self, db_with_schema):
        entry = _setup_with_notification(db_with_schema)
        notifs = get_notifications(db_with_schema, agent_id="watcher")
        assert notifs[0]["status"] == "pending"

        mark_delivered(db_with_schema, notification_ids=[notifs[0]["notification_id"]])

        row = db_with_schema.execute(
            "SELECT status, delivered_at FROM notification_queue WHERE id = ?",
            (notifs[0]["notification_id"],),
        ).fetchone()
        assert row["status"] == "delivered"
        assert row["delivered_at"] is not None


class TestNotifySinceFilter:
    """since parameter filters notifications by created_at."""

    def test_since_filter(self, db_with_schema):
        _setup_with_notification(db_with_schema)
        # Future date → no results
        notifs = get_notifications(db_with_schema, agent_id="watcher",
                                   since="2099-01-01T00:00:00Z")
        assert len(notifs) == 0

        # Past date → all results
        notifs = get_notifications(db_with_schema, agent_id="watcher",
                                   since="2020-01-01T00:00:00Z")
        assert len(notifs) == 1


class TestNotifyLimit:
    """limit parameter caps returned notifications."""

    def test_limit(self, db_with_schema):
        register_agent(db_with_schema, agent_id="watcher", display_name="W")
        create_subscription(db_with_schema, agent_id="watcher", filter_tags=["ai"])
        for i in range(5):
            post_entry(db_with_schema, author_id="poster", entry_type="finding",
                       performative="inform", content=f"AI news {i}",
                       tags=["ai"])
        notifs = get_notifications(db_with_schema, agent_id="watcher", limit=3)
        assert len(notifs) == 3


class TestNotificationsEnqueued:
    """Notifications are enqueued when entries match active subscriptions."""

    def test_enqueued_on_post(self, db_with_schema):
        _setup_with_notification(db_with_schema)
        count = db_with_schema.execute(
            "SELECT COUNT(*) FROM notification_queue WHERE agent_id='watcher'"
        ).fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# Queue state machine
# ---------------------------------------------------------------------------

class TestStateMachinePendingToDelivered:
    """pending → leased → delivered."""

    def test_lease_then_deliver(self, db_with_schema):
        _setup_with_notification(db_with_schema)

        # Lease
        leased = lease_notifications(db_with_schema, agent_id="watcher", limit=1)
        assert len(leased) == 1
        assert leased[0]["status"] == "leased"

        # Verify in DB
        row = db_with_schema.execute(
            "SELECT status FROM notification_queue WHERE id = ?",
            (leased[0]["notification_id"],),
        ).fetchone()
        assert row["status"] == "leased"

        # Deliver
        mark_delivered(db_with_schema, notification_ids=[leased[0]["notification_id"]])
        row = db_with_schema.execute(
            "SELECT status FROM notification_queue WHERE id = ?",
            (leased[0]["notification_id"],),
        ).fetchone()
        assert row["status"] == "delivered"


class TestStateMachineFailAndRetry:
    """pending → leased → failed → pending (retry with incremented attempt_count)."""

    def test_fail_and_retry(self, db_with_schema):
        _setup_with_notification(db_with_schema)

        # Lease
        leased = lease_notifications(db_with_schema, agent_id="watcher", limit=1)
        nid = leased[0]["notification_id"]

        # Fail
        mark_failed(db_with_schema, notification_ids=[nid])
        row = db_with_schema.execute(
            "SELECT status, attempt_count FROM notification_queue WHERE id = ?",
            (nid,),
        ).fetchone()
        assert row["status"] == "pending"  # back to pending for retry
        assert row["attempt_count"] == 1

        # Lease again (retry)
        leased2 = lease_notifications(db_with_schema, agent_id="watcher", limit=1)
        assert len(leased2) == 1

        # Fail again
        mark_failed(db_with_schema, notification_ids=[nid])
        row = db_with_schema.execute(
            "SELECT attempt_count FROM notification_queue WHERE id = ?",
            (nid,),
        ).fetchone()
        assert row["attempt_count"] == 2


class TestStateMachineExpired:
    """pending → expired when past expires_at."""

    def test_expire_past_deadline(self, db_with_schema):
        _setup_with_notification(db_with_schema)

        # Set expires_at to the past
        db_with_schema.execute(
            "UPDATE notification_queue SET expires_at = '2020-01-01T00:00:00Z'"
        )
        db_with_schema.commit()

        expire_notifications(db_with_schema)

        row = db_with_schema.execute(
            "SELECT status FROM notification_queue WHERE agent_id = 'watcher'"
        ).fetchone()
        assert row["status"] == "expired"

    def test_no_expire_future_deadline(self, db_with_schema):
        _setup_with_notification(db_with_schema)

        # Set expires_at to the future
        db_with_schema.execute(
            "UPDATE notification_queue SET expires_at = '2099-01-01T00:00:00Z'"
        )
        db_with_schema.commit()

        expire_notifications(db_with_schema)

        row = db_with_schema.execute(
            "SELECT status FROM notification_queue WHERE agent_id = 'watcher'"
        ).fetchone()
        assert row["status"] == "pending"  # still pending

    def test_no_expire_without_deadline(self, db_with_schema):
        """Notifications without expires_at are never expired."""
        _setup_with_notification(db_with_schema)
        expire_notifications(db_with_schema)

        row = db_with_schema.execute(
            "SELECT status FROM notification_queue WHERE agent_id = 'watcher'"
        ).fetchone()
        assert row["status"] == "pending"
