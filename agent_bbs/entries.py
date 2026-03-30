"""Entry operations — post (with idempotency) per Sections 6-7 of the spec.

Phase 2 additions:
- Inline links created atomically with the entry
- Auto-creates retracted_by links for retract performatives
- Triggers subscription evaluation after post
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from agent_bbs.canon import compute_content_fingerprint, compute_record_hash


def post_entry(
    conn: sqlite3.Connection,
    *,
    author_id: str,
    entry_type: str,
    performative: str,
    content: str,
    confidence: float = 0.5,
    tags: Optional[list] = None,
    directed_to: Optional[list] = None,
    idempotency_key: Optional[str] = None,
    metadata: Optional[dict] = None,
    links: Optional[list] = None,
) -> dict:
    """Create a new entry, or return the existing one if idempotency key matches.

    Args:
        links: Optional list of dicts with target_entry_id, link_type, and
               optional annotation. Created atomically with the entry.

    Returns a dict with at least: id, record_hash, content_fingerprint.
    """
    # --- Idempotency check (only when key is non-NULL) ---
    if idempotency_key is not None:
        existing = conn.execute(
            "SELECT id, record_hash, content_fingerprint FROM entries "
            "WHERE author_id = ? AND idempotency_key = ?",
            (author_id, idempotency_key),
        ).fetchone()
        if existing:
            return dict(existing)

    # --- Compute hashes ---
    now = datetime.now(timezone.utc)
    if now.microsecond:
        created_at = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}".rstrip("0") + "Z"
    else:
        created_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    record_hash = compute_record_hash(
        author_id, created_at, entry_type, performative, content
    )
    content_fp = compute_content_fingerprint(content)

    tags_json = json.dumps(tags or [])
    directed_json = json.dumps(directed_to or [])
    meta_json = json.dumps(metadata or {})

    # --- Insert entry + links atomically ---
    cur = conn.execute(
        "INSERT INTO entries "
        "(record_hash, content_fingerprint, author_id, created_at, "
        " entry_type, performative, content, confidence, tags, "
        " directed_to, idempotency_key, metadata) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            record_hash, content_fp, author_id, created_at,
            entry_type, performative, content, confidence,
            tags_json, directed_json, idempotency_key, meta_json,
        ),
    )
    entry_id = cur.lastrowid

    # --- Create inline links ---
    has_retracted_by = False
    if links:
        for link_spec in links:
            target_id = link_spec["target_entry_id"]
            lt = link_spec["link_type"]
            ann = link_spec.get("annotation")

            if lt == "retracted_by":
                # retracted_by: source is the TARGET (original), target is the new entry
                conn.execute(
                    "INSERT INTO links (source_entry, target_entry, link_type, "
                    "author_id, created_at, annotation) VALUES (?,?,?,?,?,?)",
                    (target_id, entry_id, "retracted_by", author_id, created_at, ann),
                )
                has_retracted_by = True
            else:
                conn.execute(
                    "INSERT INTO links (source_entry, target_entry, link_type, "
                    "author_id, created_at, annotation) VALUES (?,?,?,?,?,?)",
                    (entry_id, target_id, lt, author_id, created_at, ann),
                )

    # --- Auto-create retracted_by for retract performatives ---
    if performative == "retract" and not has_retracted_by and links:
        # Find the target(s) from the inline links and create retracted_by
        for link_spec in links:
            target_id = link_spec["target_entry_id"]
            try:
                conn.execute(
                    "INSERT INTO links (source_entry, target_entry, link_type, "
                    "author_id, created_at) VALUES (?,?,?,?,?)",
                    (target_id, entry_id, "retracted_by", author_id, created_at),
                )
            except sqlite3.IntegrityError:
                pass  # already exists

    conn.commit()

    # --- Evaluate subscriptions (post-commit) ---
    try:
        from agent_bbs.subscriptions import evaluate_subscriptions
        evaluate_subscriptions(conn, entry_id)
    except ImportError:
        pass  # subscriptions module not yet available

    # --- Notify directed_to agents ---
    if directed_to:
        _notify_directed(conn, entry_id, directed_to, author_id, created_at)

    return {
        "id": entry_id,
        "record_hash": record_hash,
        "content_fingerprint": content_fp,
    }


def _notify_directed(conn, entry_id, directed_to, author_id, created_at):
    """Enqueue notifications for agents in the directed_to list."""
    for agent_id in directed_to:
        if agent_id == author_id:
            continue
        # Only notify if the agent exists
        agent = conn.execute(
            "SELECT id FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        if agent is None:
            continue
        try:
            conn.execute(
                "INSERT INTO notification_queue "
                "(agent_id, entry_id, created_at, status) VALUES (?,?,?,?)",
                (agent_id, entry_id, created_at, "pending"),
            )
        except sqlite3.IntegrityError:
            pass
    conn.commit()
