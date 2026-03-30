"""Link operations per Sections 4.2, 7.4.

- Create typed relationships between entries
- Contradicts links trigger Tier 1 notification to target author
- Idempotency via (author_id, idempotency_key)
"""

import sqlite3
from datetime import datetime, timezone
from typing import Optional


def create_link(
    conn: sqlite3.Connection,
    *,
    source_entry_id: int,
    target_entry_id: int,
    link_type: str,
    author_id: str,
    annotation: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Create a link between two entries.

    If idempotency_key matches an existing link by this author, returns original.
    If link_type is 'contradicts', enqueues notification for target entry's author.
    """
    # Idempotency check
    if idempotency_key is not None:
        existing = conn.execute(
            "SELECT id, source_entry, target_entry, link_type FROM links "
            "WHERE author_id = ? AND idempotency_key = ?",
            (author_id, idempotency_key),
        ).fetchone()
        if existing:
            return dict(existing)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    cur = conn.execute(
        "INSERT INTO links (source_entry, target_entry, link_type, author_id, "
        "created_at, annotation, idempotency_key) VALUES (?,?,?,?,?,?,?)",
        (source_entry_id, target_entry_id, link_type, author_id,
         now, annotation, idempotency_key),
    )
    link_id = cur.lastrowid

    # Contradicts links trigger Tier 1 notification to target author
    if link_type == "contradicts":
        _notify_contradiction(conn, target_entry_id, source_entry_id, now)

    conn.commit()

    return {
        "id": link_id,
        "source_entry": source_entry_id,
        "target_entry": target_entry_id,
        "link_type": link_type,
    }


def _notify_contradiction(conn, target_entry_id, source_entry_id, now):
    """Enqueue a notification for the author of the target entry."""
    target_entry = conn.execute(
        "SELECT author_id FROM entries WHERE id = ?", (target_entry_id,)
    ).fetchone()
    if target_entry is None:
        return

    # Enqueue notification about the source entry (the contradicting entry)
    try:
        conn.execute(
            "INSERT INTO notification_queue "
            "(agent_id, entry_id, created_at, status) VALUES (?,?,?,?)",
            (target_entry["author_id"], source_entry_id, now, "pending"),
        )
    except sqlite3.IntegrityError:
        pass  # already enqueued
