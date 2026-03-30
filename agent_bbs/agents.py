"""Agent registration per Sections 4.3 and 16 of the spec.

API keys are generated as secure random tokens and stored only as bcrypt hashes.
"""

import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import bcrypt


def register_agent(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    display_name: str,
    agent_type: Optional[str] = None,
    description: Optional[str] = None,
    public_key: Optional[str] = None,
    status: str = "active",
    metadata: Optional[str] = None,
) -> dict:
    """Register a new agent. Returns dict with agent_id and plaintext api_key.

    The api_key is shown exactly once — it is stored only as a bcrypt hash.
    Raises sqlite3.IntegrityError if agent_id already exists.
    """
    # Generate a secure random API key
    raw_key = secrets.token_urlsafe(32)

    # Hash with bcrypt (spec: Section 16 — bcrypt API keys)
    key_hash = bcrypt.hashpw(raw_key.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn.execute(
        "INSERT INTO agents (id, display_name, agent_type, description, "
        "public_key, created_at, api_key_hash, status, metadata) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            agent_id, display_name, agent_type, description,
            public_key, created_at, key_hash, status, metadata or "{}",
        ),
    )
    conn.commit()

    return {
        "agent_id": agent_id,
        "api_key": raw_key,
        "status": status,
    }
