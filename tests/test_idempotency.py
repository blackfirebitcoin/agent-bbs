"""Tests for idempotency — duplicate post detection.

Spec reference: Section 6 of agent-bbs-v2-technical-proposal.md

Rules:
  - (author_id, idempotency_key) is unique
  - Duplicate → return original entry, no new row
  - Different agents CAN reuse the same key
  - NULL keys never trigger dedup
"""

import pytest

from agent_bbs.entries import post_entry


class TestIdempotentPost:
    """Posting with the same idempotency key returns the original."""

    def test_duplicate_returns_original(self, db_with_schema):
        result1 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                             performative="inform", content="hello",
                             idempotency_key="key-1")
        result2 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                             performative="inform", content="hello",
                             idempotency_key="key-1")

        assert result1["id"] == result2["id"]
        assert result1["record_hash"] == result2["record_hash"]

    def test_no_duplicate_row_created(self, db_with_schema):
        post_entry(db_with_schema, author_id="a1", entry_type="finding",
                   performative="inform", content="hello",
                   idempotency_key="key-1")
        post_entry(db_with_schema, author_id="a1", entry_type="finding",
                   performative="inform", content="hello",
                   idempotency_key="key-1")

        count = db_with_schema.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        assert count == 1


class TestDifferentAgentsReuseKey:
    """Different agents CAN reuse the same idempotency key."""

    def test_different_agents_same_key_creates_two(self, db_with_schema):
        r1 = post_entry(db_with_schema, author_id="agent-a", entry_type="finding",
                        performative="inform", content="hello",
                        idempotency_key="shared-key")
        r2 = post_entry(db_with_schema, author_id="agent-b", entry_type="finding",
                        performative="inform", content="hello",
                        idempotency_key="shared-key")

        assert r1["id"] != r2["id"]
        count = db_with_schema.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        assert count == 2


class TestNullIdempotencyKey:
    """NULL keys never trigger dedup — every post creates a new entry."""

    def test_null_key_always_creates_new_entry(self, db_with_schema):
        r1 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="msg 1",
                        idempotency_key=None)
        r2 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="msg 2",
                        idempotency_key=None)

        assert r1["id"] != r2["id"]

    def test_null_key_same_content_still_creates_new(self, db_with_schema):
        """Even identical content with NULL key → new entry (timestamps differ)."""
        r1 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="same",
                        idempotency_key=None)
        r2 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="same",
                        idempotency_key=None)

        assert r1["id"] != r2["id"]
        count = db_with_schema.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        assert count == 2


class TestIdempotentPostFields:
    """The returned entry dict has the expected fields."""

    def test_return_has_record_hash_and_fingerprint(self, db_with_schema):
        result = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                            performative="inform", content="data point",
                            idempotency_key="k1")
        assert "id" in result
        assert "record_hash" in result
        assert "content_fingerprint" in result
        assert len(result["record_hash"]) == 64
