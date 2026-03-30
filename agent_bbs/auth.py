"""Authentication — API key validation via bcrypt.

All REST endpoints that require auth use the `require_auth` dependency.
"""

import bcrypt
import sqlite3
from typing import Optional


def verify_api_key(conn: sqlite3.Connection, api_key: str) -> Optional[str]:
    """Validate an API key and return the agent_id if valid.

    Returns None if the key is missing, invalid, or the agent is not active.

    The API key is stored as a bcrypt hash. We iterate all agents
    (the table is small — this is fine) and check the hash.
    """
    if not api_key:
        return None

    rows = conn.execute(
        "SELECT id, api_key_hash, status FROM agents"
    ).fetchall()

    key_bytes = api_key.encode("utf-8")
    for row in rows:
        try:
            stored_hash = row["api_key_hash"]
            # Skip agents without a valid hash (shouldn't happen)
            if not stored_hash:
                continue
            if bcrypt.checkpw(key_bytes, stored_hash.encode("utf-8")):
                if row["status"] == "active":
                    return row["id"]
                # Valid key but not yet approved
                return None
        except Exception:
            continue

    return None


def require_auth(conn: sqlite3.Connection, api_key: str) -> str:
    """FastAPI dependency — raises 401 if auth fails, returns agent_id if valid."""
    from fastapi import HTTPException

    agent_id = verify_api_key(conn, api_key)
    if not agent_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key, or account not yet approved.",
        )
    return agent_id


def require_admin(conn: sqlite3.Connection, api_key: str, admin_id: str = "roo") -> str:
    """FastAPI dependency — requires admin API key, raises 403 if not admin."""
    from fastapi import HTTPException

    agent_id = verify_api_key(conn, api_key)
    if not agent_id:
        raise HTTPException(status_code=401, detail="Invalid API key.")
    if agent_id != admin_id:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return agent_id
