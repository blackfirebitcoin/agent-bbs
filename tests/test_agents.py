"""Tests for agent registration.

Spec reference: Sections 4.3, 16 of agent-bbs-v2-technical-proposal.md

Rules:
  - Registration returns an API key (plaintext, only time it's visible)
  - Duplicate handle (agent id) is rejected
  - API key is stored as a bcrypt hash — never in plain text
"""

import sqlite3

import bcrypt
import pytest

from agent_bbs.agents import register_agent


class TestRegistrationReturnsKey:
    """register_agent must return an API key on success."""

    def test_returns_api_key(self, db_with_schema):
        result = register_agent(db_with_schema, agent_id="agent-alpha",
                                display_name="Alpha Agent")
        assert "api_key" in result
        assert isinstance(result["api_key"], str)
        assert len(result["api_key"]) > 20  # reasonable key length

    def test_returns_agent_id(self, db_with_schema):
        result = register_agent(db_with_schema, agent_id="agent-beta",
                                display_name="Beta Agent")
        assert result["agent_id"] == "agent-beta"

    def test_agent_row_created(self, db_with_schema):
        register_agent(db_with_schema, agent_id="agent-gamma",
                       display_name="Gamma Agent")
        row = db_with_schema.execute(
            "SELECT id, display_name FROM agents WHERE id = ?",
            ("agent-gamma",),
        ).fetchone()
        assert row is not None
        assert row["display_name"] == "Gamma Agent"


class TestDuplicateHandleRejection:
    """Duplicate agent IDs must be rejected."""

    def test_duplicate_id_raises(self, db_with_schema):
        register_agent(db_with_schema, agent_id="agent-dup",
                       display_name="First")
        with pytest.raises((sqlite3.IntegrityError, ValueError)):
            register_agent(db_with_schema, agent_id="agent-dup",
                           display_name="Second")


class TestAPIKeyHashing:
    """The API key must NEVER be stored in plain text."""

    def test_stored_hash_is_bcrypt(self, db_with_schema):
        result = register_agent(db_with_schema, agent_id="agent-secure",
                                display_name="Secure Agent")
        raw_key = result["api_key"]

        row = db_with_schema.execute(
            "SELECT api_key_hash FROM agents WHERE id = ?",
            ("agent-secure",),
        ).fetchone()
        stored_hash = row["api_key_hash"]

        # The stored value must be a bcrypt hash (starts with $2b$)
        assert stored_hash.startswith("$2b$"), "Stored hash is not bcrypt"
        # The stored hash must NOT equal the raw key
        assert stored_hash != raw_key

    def test_raw_key_verifies_against_stored_hash(self, db_with_schema):
        result = register_agent(db_with_schema, agent_id="agent-verify",
                                display_name="Verify Agent")
        raw_key = result["api_key"]

        row = db_with_schema.execute(
            "SELECT api_key_hash FROM agents WHERE id = ?",
            ("agent-verify",),
        ).fetchone()
        stored_hash = row["api_key_hash"]

        # bcrypt.checkpw must confirm the key matches the hash
        assert bcrypt.checkpw(raw_key.encode("utf-8"), stored_hash.encode("utf-8"))

    def test_plaintext_key_not_anywhere_in_db(self, db_with_schema):
        result = register_agent(db_with_schema, agent_id="agent-notext",
                                display_name="No Plaintext")
        raw_key = result["api_key"]

        # Scan the entire agents table for the raw key
        rows = db_with_schema.execute("SELECT * FROM agents").fetchall()
        for row in rows:
            for value in tuple(row):
                if isinstance(value, str):
                    assert value != raw_key, "Raw API key found in DB!"
