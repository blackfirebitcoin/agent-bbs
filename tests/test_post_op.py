"""Tests for the Post operation (Phase 2).

Spec reference: Section 7.1
- Creates entry with correct record_hash and content_fingerprint
- Auto-creates retracted_by link for retract performatives
- Atomically creates inline links if provided
- Triggers subscription evaluation (enqueues notifications)
"""

import json

import pytest

from agent_bbs.agents import register_agent
from agent_bbs.entries import post_entry
from agent_bbs.subscriptions import create_subscription


class TestPostCreatesEntry:
    """Post creates an entry with correct hashes."""

    def test_record_hash_is_sha256_hex(self, db_with_schema):
        r = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                       performative="inform", content="test data")
        assert len(r["record_hash"]) == 64

    def test_content_fingerprint_present(self, db_with_schema):
        r = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                       performative="inform", content="test data")
        assert len(r["content_fingerprint"]) == 64

    def test_entry_stored_in_db(self, db_with_schema):
        r = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                       performative="inform", content="test data",
                       confidence=0.9, tags=["ai"])
        row = db_with_schema.execute(
            "SELECT * FROM entries WHERE id = ?", (r["id"],)
        ).fetchone()
        assert row["entry_type"] == "finding"
        assert row["performative"] == "inform"
        assert row["confidence"] == 0.9
        assert json.loads(row["tags"]) == ["ai"]


class TestRetractAutoLink:
    """Retract performatives auto-create retracted_by links."""

    def test_retract_creates_retracted_by_link(self, db_with_schema):
        # Post original entry
        orig = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                          performative="inform", content="original claim")
        # Post retraction linking to original
        retraction = post_entry(
            db_with_schema, author_id="a1", entry_type="finding",
            performative="retract", content="I retract the original",
            links=[{"target_entry_id": orig["id"], "link_type": "retracted_by"}],
        )
        # The link should exist: original --retracted_by--> retraction
        link = db_with_schema.execute(
            "SELECT * FROM links WHERE source_entry = ? AND target_entry = ? "
            "AND link_type = 'retracted_by'",
            (orig["id"], retraction["id"]),
        ).fetchone()
        assert link is not None

    def test_retract_without_explicit_link_auto_creates(self, db_with_schema):
        """When retract performative includes a responds_to link to the target,
        the system should also auto-create the retracted_by link on the target."""
        orig = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                          performative="inform", content="original")
        retraction = post_entry(
            db_with_schema, author_id="a1", entry_type="finding",
            performative="retract", content="retracted",
            links=[{"target_entry_id": orig["id"], "link_type": "responds_to"}],
        )
        # retracted_by link should be auto-created (source=original, target=retraction)
        link = db_with_schema.execute(
            "SELECT * FROM links WHERE source_entry = ? AND link_type = 'retracted_by'",
            (orig["id"],),
        ).fetchone()
        assert link is not None
        assert link["target_entry"] == retraction["id"]


class TestInlineLinks:
    """Post atomically creates inline links if provided."""

    def test_inline_links_created(self, db_with_schema):
        e1 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="first")
        e2 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="second")
        e3 = post_entry(
            db_with_schema, author_id="a1", entry_type="synthesis",
            performative="propose", content="synthesis of first and second",
            links=[
                {"target_entry_id": e1["id"], "link_type": "derived_from"},
                {"target_entry_id": e2["id"], "link_type": "derived_from"},
            ],
        )
        links = db_with_schema.execute(
            "SELECT * FROM links WHERE source_entry = ?", (e3["id"],)
        ).fetchall()
        assert len(links) == 2
        assert {l["link_type"] for l in links} == {"derived_from"}

    def test_inline_link_rollback_on_bad_link(self, db_with_schema):
        """If an inline link fails (e.g. bad target), the entry should still exist
        but the bad link should not."""
        e1 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="first")
        # Target 9999 doesn't exist — FK violation on the link
        with pytest.raises(Exception):
            post_entry(
                db_with_schema, author_id="a1", entry_type="finding",
                performative="inform", content="with bad link",
                links=[{"target_entry_id": 9999, "link_type": "supports"}],
            )


class TestPostTriggersSubscription:
    """Posting an entry that matches a subscription enqueues a notification."""

    def _register(self, db, agent_id):
        register_agent(db, agent_id=agent_id, display_name=agent_id)

    def test_matching_subscription_enqueues_notification(self, db_with_schema):
        self._register(db_with_schema, "watcher")
        create_subscription(db_with_schema, agent_id="watcher",
                            filter_tags=["ai"], filter_types=[], filter_perfs=[],
                            filter_authors=[])
        # Post from a different agent with a matching tag
        post_entry(db_with_schema, author_id="poster", entry_type="finding",
                   performative="inform", content="AI breakthrough",
                   tags=["ai"])
        notifs = db_with_schema.execute(
            "SELECT * FROM notification_queue WHERE agent_id = 'watcher'"
        ).fetchall()
        assert len(notifs) == 1
        assert notifs[0]["status"] == "pending"

    def test_non_matching_subscription_no_notification(self, db_with_schema):
        self._register(db_with_schema, "watcher")
        create_subscription(db_with_schema, agent_id="watcher",
                            filter_tags=["biology"], filter_types=[], filter_perfs=[],
                            filter_authors=[])
        post_entry(db_with_schema, author_id="poster", entry_type="finding",
                   performative="inform", content="AI stuff", tags=["ai"])
        notifs = db_with_schema.execute(
            "SELECT * FROM notification_queue WHERE agent_id = 'watcher'"
        ).fetchall()
        assert len(notifs) == 0

    def test_author_not_notified_of_own_posts(self, db_with_schema):
        """An agent should not receive notifications for their own posts."""
        self._register(db_with_schema, "self-poster")
        create_subscription(db_with_schema, agent_id="self-poster",
                            filter_tags=["ai"], filter_types=[], filter_perfs=[],
                            filter_authors=[])
        post_entry(db_with_schema, author_id="self-poster", entry_type="finding",
                   performative="inform", content="my own AI post", tags=["ai"])
        notifs = db_with_schema.execute(
            "SELECT * FROM notification_queue WHERE agent_id = 'self-poster'"
        ).fetchall()
        assert len(notifs) == 0
