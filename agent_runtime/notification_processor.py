"""Notification processor — the ticker loop.

Pull pending notifications from working memory, score each with the priority
function, sort, batch, and output a structured "context assembly request"
that a future LLM call will consume.

The processor bridges the BBS notification system and agent working memory:
1. Poll BBS for new notifications (via MCP server or direct DB)
2. Store in local pending_notifications
3. Score each notification
4. Assemble a prioritized batch
5. Output a context assembly request
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from agent_runtime.working_memory import (
    compute_priority_score,
    get_scored_batch,
    mark_notifications_processed,
    record_event,
    score_pending_notifications,
    store_pending_notification,
)


# ---------------------------------------------------------------------------
# Context assembly request structure
# ---------------------------------------------------------------------------

def build_context_request(
    batch: list[dict],
    *,
    token_budget: int,
    agent_id: str,
) -> dict:
    """Build a structured context assembly request from a scored batch.

    This is the output consumed by the future LLM context pipeline.
    """
    return {
        "type": "context_assembly_request",
        "agent_id": agent_id,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "token_budget": token_budget,
        "notification_count": len(batch),
        "notifications": [
            {
                "entry_id": n["entry_id"],
                "record_hash": n.get("record_hash"),
                "author_id": n["author_id"],
                "entry_type": n["entry_type"],
                "performative": n["performative"],
                "tags": n.get("tags", "[]"),
                "confidence": n.get("confidence", 0.5),
                "directed_to_me": bool(n.get("directed_to_me", False)),
                "priority_score": n.get("priority_score", 0.0),
            }
            for n in batch
        ],
        "fetch_entry_ids": [n["entry_id"] for n in batch],
    }


# ---------------------------------------------------------------------------
# Ticker: poll → store → score → batch → context request
# ---------------------------------------------------------------------------

def tick(
    bbs_conn: sqlite3.Connection,
    wm_conn: sqlite3.Connection,
    *,
    agent_id: str,
    watch_tags: list[str],
    scorer_weights: dict,
    batch_size: int = 10,
    min_score: float = 0.0,
    token_budget: int = 4096,
    since: Optional[str] = None,
) -> Optional[dict]:
    """Execute one tick of the notification processor.

    Returns a context assembly request dict if there are notifications
    to process, or None if the inbox is empty.
    """
    # Step 1: Poll BBS for new notifications
    new_notifications = _poll_bbs_notifications(
        bbs_conn, agent_id=agent_id, since=since
    )

    # Step 2: Store in working memory
    stored_count = 0
    for notif in new_notifications:
        lid = store_pending_notification(wm_conn, notification=notif)
        if lid:
            record_event(
                wm_conn,
                event_type="notification_received",
                source="bbs_notification",
                entry_id=notif.get("entry_id"),
                record_hash=notif.get("record_hash"),
                payload=notif,
            )
            stored_count += 1

    # Step 3: Score all pending
    scored = score_pending_notifications(
        wm_conn,
        agent_id=agent_id,
        watch_tags=watch_tags,
        weights=scorer_weights,
    )

    # Step 4: Get top-priority batch
    batch = get_scored_batch(wm_conn, limit=batch_size, min_score=min_score)

    if not batch:
        return None

    # Step 5: Build context assembly request
    context_request = build_context_request(
        batch, token_budget=token_budget, agent_id=agent_id
    )

    return context_request


def _poll_bbs_notifications(
    bbs_conn: sqlite3.Connection,
    *,
    agent_id: str,
    since: Optional[str] = None,
) -> list[dict]:
    """Poll BBS for pending notifications for this agent.

    Uses direct DB access (not MCP) for Phase 4a.
    """
    from agent_bbs.notifications import get_notifications

    return get_notifications(
        bbs_conn,
        agent_id=agent_id,
        since=since,
        status_filter="pending",
    )


def process_tick_result(
    wm_conn: sqlite3.Connection,
    *,
    context_request: dict,
) -> None:
    """Mark the batch as processed after the LLM has consumed it.

    Call this after the context assembly request has been used.
    """
    if not context_request or not context_request.get("notifications"):
        return

    # We need the local notification IDs, not entry_ids
    # Look them up from the batched notifications
    entry_ids = context_request.get("fetch_entry_ids", [])
    if not entry_ids:
        return

    placeholders = ",".join("?" for _ in entry_ids)
    rows = wm_conn.execute(
        f"SELECT id FROM pending_notifications "
        f"WHERE entry_id IN ({placeholders}) AND status = 'batched'",
        entry_ids,
    ).fetchall()

    if rows:
        local_ids = [r["id"] for r in rows]
        mark_notifications_processed(wm_conn, notification_ids=local_ids)

        record_event(
            wm_conn,
            event_type="action_taken",
            source="notification_processor",
            payload={
                "action": "batch_processed",
                "entry_ids": entry_ids,
                "local_notification_ids": local_ids,
            },
        )
