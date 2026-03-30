"""Tests for the Link operation (Phase 2).

Spec reference: Section 7.4
- Create links with all 8 link types
- Contradicts links enqueue notification for target author
- Idempotency on links
"""

import sqlite3

import pytest

from agent_bbs.agents import register_agent
from agent_bbs.entries import post_entry
from agent_bbs.links import create_link


ALL_LINK_TYPES = [
    "supports", "contradicts", "supersedes", "responds_to",
    "derived_from", "depends_on", "same_as", "retracted_by",
]


class TestCreateAllLinkTypes:
    """All 8 link types can be created successfully."""

    def test_all_link_types(self, db_with_schema):
        e1 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="source")
        for i, lt in enumerate(ALL_LINK_TYPES):
            target = post_entry(db_with_schema, author_id="a1",
                                entry_type="finding", performative="inform",
                                content=f"target {i}")
            result = create_link(db_with_schema, source_entry_id=e1["id"],
                                 target_entry_id=target["id"], link_type=lt,
                                 author_id="a1")
            assert result["link_type"] == lt
            assert result["source_entry"] == e1["id"]
            assert result["target_entry"] == target["id"]

    def test_invalid_link_type_rejected(self, db_with_schema):
        e1 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="source")
        e2 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="target")
        with pytest.raises(sqlite3.IntegrityError):
            create_link(db_with_schema, source_entry_id=e1["id"],
                        target_entry_id=e2["id"], link_type="related_to",
                        author_id="a1")


class TestContradictsNotification:
    """Contradicts links enqueue a notification for the target entry's author."""

    def test_contradicts_notifies_target_author(self, db_with_schema):
        register_agent(db_with_schema, agent_id="alice", display_name="Alice")
        register_agent(db_with_schema, agent_id="bob", display_name="Bob")

        alice_entry = post_entry(db_with_schema, author_id="alice",
                                 entry_type="finding", performative="inform",
                                 content="The sky is green")
        bob_entry = post_entry(db_with_schema, author_id="bob",
                               entry_type="contradiction",
                               performative="disconfirm",
                               content="No, the sky is blue")

        create_link(db_with_schema, source_entry_id=bob_entry["id"],
                    target_entry_id=alice_entry["id"],
                    link_type="contradicts", author_id="bob")

        # Alice should have a notification about bob_entry
        notifs = db_with_schema.execute(
            "SELECT * FROM notification_queue WHERE agent_id = 'alice'"
        ).fetchall()
        assert len(notifs) == 1
        assert notifs[0]["entry_id"] == bob_entry["id"]
        assert notifs[0]["status"] == "pending"

    def test_non_contradicts_no_notification(self, db_with_schema):
        register_agent(db_with_schema, agent_id="alice", display_name="Alice")
        e1 = post_entry(db_with_schema, author_id="alice", entry_type="finding",
                        performative="inform", content="claim")
        e2 = post_entry(db_with_schema, author_id="bob", entry_type="finding",
                        performative="inform", content="support")

        create_link(db_with_schema, source_entry_id=e2["id"],
                    target_entry_id=e1["id"], link_type="supports",
                    author_id="bob")

        notifs = db_with_schema.execute(
            "SELECT * FROM notification_queue WHERE agent_id = 'alice'"
        ).fetchall()
        assert len(notifs) == 0


class TestLinkIdempotency:
    """Idempotency key on links."""

    def test_duplicate_key_returns_original(self, db_with_schema):
        e1 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="source")
        e2 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="target")

        r1 = create_link(db_with_schema, source_entry_id=e1["id"],
                         target_entry_id=e2["id"], link_type="supports",
                         author_id="a1", idempotency_key="link-key-1")
        r2 = create_link(db_with_schema, source_entry_id=e1["id"],
                         target_entry_id=e2["id"], link_type="supports",
                         author_id="a1", idempotency_key="link-key-1")

        assert r1["id"] == r2["id"]
        count = db_with_schema.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        assert count == 1

    def test_different_authors_same_key(self, db_with_schema):
        e1 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="source")
        e2 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="target1")
        e3 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="target2")

        r1 = create_link(db_with_schema, source_entry_id=e1["id"],
                         target_entry_id=e2["id"], link_type="supports",
                         author_id="a1", idempotency_key="shared")
        r2 = create_link(db_with_schema, source_entry_id=e1["id"],
                         target_entry_id=e3["id"], link_type="supports",
                         author_id="a2", idempotency_key="shared")

        assert r1["id"] != r2["id"]
