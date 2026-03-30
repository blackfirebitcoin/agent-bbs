"""Notification operations per Sections 7.6, 8, 4.5 of the spec.

- get_notifications: metadata-only inbox check
- lease_notifications: pending → leased
- mark_delivered: leased → delivered
- mark_failed: leased → pending (retry with incremented attempt_count)
- expire_notifications: pending → expired when past expires_at
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional


def get_notifications(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    limit: int = 50,
    since: Optional[str] = None,
    status_filter: Optional[str] = None,
) -> list[dict]:
    """Check inbox — metadata only, no content.

    Returns notification envelopes with entry metadata.
    """
    where_clauses = ["nq.agent_id = ?"]
    params: list = [agent_id]

    if since is not None:
        where_clauses.append("nq.created_at >= ?")
        params.append(since)

    if status_filter is not None:
        where_clauses.append("nq.status = ?")
        params.append(status_filter)

    where_sql = " AND ".join(where_clauses)

    rows = conn.execute(
        f"SELECT nq.id AS notification_id, nq.status, nq.created_at AS notif_created_at, "
        f"       nq.attempt_count, "
        f"       e.id AS entry_id, e.record_hash, e.author_id, "
        f"       e.created_at, e.entry_type, e.performative, "
        f"       e.tags, e.confidence, e.directed_to "
        f"FROM notification_queue nq "
        f"JOIN entries e ON e.id = nq.entry_id "
        f"WHERE {where_sql} "
        f"ORDER BY nq.created_at ASC "
        f"LIMIT ?",
        params + [limit],
    ).fetchall()

    results = []
    for row in rows:
        directed_to = json.loads(row["directed_to"])
        results.append({
            "notification_id": row["notification_id"],
            "status": row["status"],
            "entry_id": row["entry_id"],
            "record_hash": row["record_hash"],
            "author_id": row["author_id"],
            "created_at": row["created_at"],
            "entry_type": row["entry_type"],
            "performative": row["performative"],
            "tags": row["tags"],
            "confidence": row["confidence"],
            "directed_to_me": agent_id in directed_to,
        })

    return results


def lease_notifications(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    limit: int = 10,
) -> list[dict]:
    """Transition pending notifications to leased. Returns leased items."""
    rows = conn.execute(
        "SELECT id FROM notification_queue "
        "WHERE agent_id = ? AND status = 'pending' "
        "ORDER BY created_at ASC LIMIT ?",
        (agent_id, limit),
    ).fetchall()

    ids = [r["id"] for r in rows]
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE notification_queue SET status = 'leased' WHERE id IN ({placeholders})",
        ids,
    )
    conn.commit()

    return get_notifications(conn, agent_id=agent_id, status_filter="leased", limit=limit)


def mark_delivered(
    conn: sqlite3.Connection,
    *,
    notification_ids: list[int],
) -> None:
    """Transition notifications to delivered."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    placeholders = ",".join("?" for _ in notification_ids)
    conn.execute(
        f"UPDATE notification_queue SET status = 'delivered', delivered_at = ? "
        f"WHERE id IN ({placeholders})",
        [now] + notification_ids,
    )
    conn.commit()


def mark_failed(
    conn: sqlite3.Connection,
    *,
    notification_ids: list[int],
) -> None:
    """Transition leased → pending (retry), incrementing attempt_count."""
    placeholders = ",".join("?" for _ in notification_ids)
    conn.execute(
        f"UPDATE notification_queue SET status = 'pending', "
        f"attempt_count = attempt_count + 1 "
        f"WHERE id IN ({placeholders})",
        notification_ids,
    )
    conn.commit()


def expire_notifications(conn: sqlite3.Connection) -> None:
    """Transition pending notifications past their expires_at to expired."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE notification_queue SET status = 'expired' "
        "WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < ?",
        (now,),
    )
    conn.commit()
