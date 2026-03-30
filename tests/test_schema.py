"""Tests for database schema — tables, constraints, indexes.

Spec reference: Section 4 of agent-bbs-v2-technical-proposal.md
"""

import sqlite3

import pytest


# ---------------------------------------------------------------------------
# Table existence
# ---------------------------------------------------------------------------

class TestTablesExist:
    """All Phase 1 tables must be created."""

    EXPECTED = {"entries", "links", "agents", "subscriptions",
                "notification_queue", "entries_fts"}

    def test_all_tables_created(self, db_with_schema):
        rows = db_with_schema.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert self.EXPECTED.issubset(names), f"Missing: {self.EXPECTED - names}"


# ---------------------------------------------------------------------------
# entry_type CHECK constraint
# ---------------------------------------------------------------------------

class TestEntryTypeConstraint:
    """entry_type must be one of the five allowed values."""

    VALID_TYPES = ["finding", "question", "synthesis", "contradiction", "task"]

    def _insert_entry(self, db, entry_type="finding", performative="inform",
                      record_hash="h1", author_id="a1"):
        db.execute(
            "INSERT INTO entries (record_hash, author_id, created_at, "
            "entry_type, performative, content) VALUES (?,?,?,?,?,?)",
            (record_hash, author_id, "2026-01-01T00:00:00Z", entry_type,
             performative, "test"),
        )

    def test_valid_types_accepted(self, db_with_schema):
        for i, t in enumerate(self.VALID_TYPES):
            self._insert_entry(db_with_schema, entry_type=t, record_hash=f"h{i}")

    def test_invalid_type_rejected(self, db_with_schema):
        with pytest.raises(sqlite3.IntegrityError):
            self._insert_entry(db_with_schema, entry_type="bogus")


# ---------------------------------------------------------------------------
# performative CHECK constraint
# ---------------------------------------------------------------------------

class TestPerformativeConstraint:
    """performative must be one of the nine allowed values."""

    VALID_PERFS = ["inform", "request", "propose", "confirm", "disconfirm",
                   "retract", "query", "ack", "decline"]

    def _insert_entry(self, db, performative="inform", record_hash="h1"):
        db.execute(
            "INSERT INTO entries (record_hash, author_id, created_at, "
            "entry_type, performative, content) VALUES (?,?,?,?,?,?)",
            (record_hash, "a1", "2026-01-01T00:00:00Z", "finding",
             performative, "test"),
        )

    def test_valid_performatives_accepted(self, db_with_schema):
        for i, p in enumerate(self.VALID_PERFS):
            self._insert_entry(db_with_schema, performative=p, record_hash=f"h{i}")

    def test_invalid_performative_rejected(self, db_with_schema):
        with pytest.raises(sqlite3.IntegrityError):
            self._insert_entry(db_with_schema, performative="shout")


# ---------------------------------------------------------------------------
# Confidence bounds
# ---------------------------------------------------------------------------

class TestConfidenceBounds:
    """confidence must be in [0.0, 1.0]."""

    def _insert(self, db, confidence, record_hash):
        db.execute(
            "INSERT INTO entries (record_hash, author_id, created_at, "
            "entry_type, performative, content, confidence) VALUES (?,?,?,?,?,?,?)",
            (record_hash, "a1", "2026-01-01T00:00:00Z", "finding", "inform",
             "test", confidence),
        )

    def test_valid_bounds(self, db_with_schema):
        self._insert(db_with_schema, 0.0, "h0")
        self._insert(db_with_schema, 0.5, "h1")
        self._insert(db_with_schema, 1.0, "h2")

    def test_negative_rejected(self, db_with_schema):
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(db_with_schema, -0.1, "h_neg")

    def test_above_one_rejected(self, db_with_schema):
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(db_with_schema, 1.01, "h_high")


# ---------------------------------------------------------------------------
# UNIQUE constraints
# ---------------------------------------------------------------------------

class TestUniqueConstraints:
    """record_hash must be globally unique; (author_id, idempotency_key) must be unique."""

    def _insert(self, db, record_hash="h1", author_id="a1", idempotency_key=None):
        db.execute(
            "INSERT INTO entries (record_hash, author_id, created_at, "
            "entry_type, performative, content, idempotency_key) "
            "VALUES (?,?,?,?,?,?,?)",
            (record_hash, author_id, "2026-01-01T00:00:00Z", "finding",
             "inform", "test", idempotency_key),
        )

    def test_duplicate_record_hash_rejected(self, db_with_schema):
        self._insert(db_with_schema, record_hash="dup")
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(db_with_schema, record_hash="dup", author_id="a2")

    def test_duplicate_author_idempotency_rejected(self, db_with_schema):
        self._insert(db_with_schema, record_hash="h1", author_id="a1",
                     idempotency_key="key1")
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(db_with_schema, record_hash="h2", author_id="a1",
                         idempotency_key="key1")

    def test_different_authors_same_key_allowed(self, db_with_schema):
        """Different agents CAN reuse the same idempotency key."""
        self._insert(db_with_schema, record_hash="h1", author_id="a1",
                     idempotency_key="shared-key")
        self._insert(db_with_schema, record_hash="h2", author_id="a2",
                     idempotency_key="shared-key")

    def test_null_idempotency_keys_dont_conflict(self, db_with_schema):
        """Multiple NULL idempotency keys for the same author are fine."""
        self._insert(db_with_schema, record_hash="h1", author_id="a1",
                     idempotency_key=None)
        self._insert(db_with_schema, record_hash="h2", author_id="a1",
                     idempotency_key=None)


# ---------------------------------------------------------------------------
# Link table constraints
# ---------------------------------------------------------------------------

class TestLinkConstraints:
    """Link table CHECK and UNIQUE constraints."""

    def _seed_entries(self, db):
        for i in range(1, 3):
            db.execute(
                "INSERT INTO entries (record_hash, author_id, created_at, "
                "entry_type, performative, content) VALUES (?,?,?,?,?,?)",
                (f"entry{i}", "a1", "2026-01-01T00:00:00Z", "finding",
                 "inform", f"content {i}"),
            )

    def test_valid_link_type_accepted(self, db_with_schema):
        self._seed_entries(db_with_schema)
        db_with_schema.execute(
            "INSERT INTO links (source_entry, target_entry, link_type, "
            "author_id, created_at) VALUES (1, 2, 'supports', 'a1', "
            "'2026-01-01T00:00:00Z')"
        )

    def test_invalid_link_type_rejected(self, db_with_schema):
        self._seed_entries(db_with_schema)
        with pytest.raises(sqlite3.IntegrityError):
            db_with_schema.execute(
                "INSERT INTO links (source_entry, target_entry, link_type, "
                "author_id, created_at) VALUES (1, 2, 'related_to', 'a1', "
                "'2026-01-01T00:00:00Z')"
            )

    def test_duplicate_link_rejected(self, db_with_schema):
        self._seed_entries(db_with_schema)
        db_with_schema.execute(
            "INSERT INTO links (source_entry, target_entry, link_type, "
            "author_id, created_at) VALUES (1, 2, 'supports', 'a1', "
            "'2026-01-01T00:00:00Z')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db_with_schema.execute(
                "INSERT INTO links (source_entry, target_entry, link_type, "
                "author_id, created_at) VALUES (1, 2, 'supports', 'a1', "
                "'2026-01-01T00:00:00Z')"
            )

    def test_notification_queue_status_constraint(self, db_with_schema):
        """notification_queue status must be in the allowed state machine."""
        # Insert prereqs
        db_with_schema.execute(
            "INSERT INTO agents (id, display_name, created_at, api_key_hash) "
            "VALUES ('a1', 'Agent 1', '2026-01-01T00:00:00Z', 'hash123')"
        )
        db_with_schema.execute(
            "INSERT INTO entries (record_hash, author_id, created_at, "
            "entry_type, performative, content) VALUES "
            "('e1', 'a1', '2026-01-01T00:00:00Z', 'finding', 'inform', 'x')"
        )
        # Valid statuses
        for i, status in enumerate(["pending", "leased", "delivered", "failed", "expired"]):
            db_with_schema.execute(
                "INSERT INTO notification_queue (agent_id, entry_id, created_at, status) "
                "VALUES ('a1', 1, '2026-01-01T00:00:00Z', ?)",
                (status,),
            )
            # Clean up for next iteration (unique constraint on agent_id, entry_id)
            db_with_schema.execute("DELETE FROM notification_queue")

        # Invalid status
        with pytest.raises(sqlite3.IntegrityError):
            db_with_schema.execute(
                "INSERT INTO notification_queue (agent_id, entry_id, created_at, status) "
                "VALUES ('a1', 1, '2026-01-01T00:00:00Z', 'cancelled')"
            )
