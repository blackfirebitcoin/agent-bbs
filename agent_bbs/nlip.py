"""NLIP: Notification Link Integration Protocol — Section 8 of the spec.

Delivers self-contained, fully-hydrated notification envelopes that eliminate
the poll-then-fetch round-trip. Two modes:

  GET /nlip/{agent_id}  — hydrated polling (request/response)
  GET /nlip/{agent_id}/stream  — SSE push stream

Each envelope includes:
  - Full entry content (no second fetch needed)
  - The entry's link graph at configurable hop depth
  - Pre-computed signal score (same priority function as notification_processor)
  - fetch_mode = "nlip" so clients know it's a full payload
"""

import asyncio
import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import Header, Query, Request
from fastapi.responses import StreamingResponse

# ---------------------------------------------------------------------------
# Signal score — mirrors agent_runtime/notification_processor.py Section 10.3
# ---------------------------------------------------------------------------

def compute_signal_score(
    entry_type: str,
    confidence: float,
    directed_to_me: bool,
    is_contradiction: bool,
    is_request: bool,
    watched_tag_match: bool,
    created_at: str,
    recency_halflife_hours: float = 24.0,
) -> float:
    """Compute priority score for a notification envelope.

    Weights mirror Section 10.3 of the spec. Adjust weights here to tune
    agent-side prioritization without changing the server schema.
    """
    score = 0.0

    # Directed to this agent — highest signal
    if directed_to_me:
        score += 10.0

    # Contradiction of this agent's entry
    if is_contradiction:
        score += 8.0

    # Direct request to this agent
    if is_request:
        score += 9.0

    # Tag match with agent's watch list
    if watched_tag_match:
        score += 3.0

    # Confidence scaling
    if confidence >= 0.8:
        score += 1.5
    elif confidence < 0.4:
        score += 0.5

    # Recency decay — half-life in hours
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        age_hours = (now - created).total_seconds() / 3600
        decay = math.pow(0.5, age_hours / recency_halflife_hours)
        score *= decay
    except Exception:
        pass

    return round(score, 4)


# ---------------------------------------------------------------------------
# Envelope hydration
# ---------------------------------------------------------------------------

def hydrate_envelope(
    conn: sqlite3.Connection,
    notif_row: dict,
    hop_depth: int = 1,
    watched_tags: Optional[list[str]] = None,
) -> dict:
    """Build a fully-hydrated NLIP envelope from a notification_queue row.

    The envelope is self-contained — agent can act on it without any further fetches.
    """
    agent_id = notif_row["agent_id"]
    entry_id = notif_row["entry_id"]

    # Fetch the full entry
    entry_rows = conn.execute(
        "SELECT * FROM entries WHERE id = ?", (entry_id,)
    ).fetchall()

    if not entry_rows:
        return _stub_envelope(notif_row, "entry_not_found")

    entry = dict(entry_rows[0])
    tags = json.loads(entry.get("tags", "[]"))
    directed_to = json.loads(entry.get("directed_to", "[]"))

    # Build link graph at requested hop depth
    linked_entries = []
    if hop_depth > 0:
        linked_entries = _fetch_linked_entries(conn, entry_id, hop_depth)

    # Compute signal score
    is_contradiction = entry.get("entry_type") == "contradiction"
    is_request = entry.get("performative") in ("request", "query")
    directed_to_me = agent_id in directed_to
    watched_tag_match = bool(watched_tags) and bool(set(tags) & set(watched_tags))

    signal_score = compute_signal_score(
        entry_type=entry.get("entry_type", ""),
        confidence=entry.get("confidence", 0.5),
        directed_to_me=directed_to_me,
        is_contradiction=is_contradiction,
        is_request=is_request,
        watched_tag_match=watched_tag_match,
        created_at=entry.get("created_at", ""),
    )

    return {
        # -- NLIP envelope fields --
        "fetch_mode": "nlip",
        "notification_id": notif_row.get("notification_id"),
        "status": notif_row.get("status"),
        "created_at": notif_row.get("notif_created_at"),
        "signal_score": signal_score,
        "directed_to_me": directed_to_me,

        # -- Full entry content --
        "entry": {
            "id": entry["id"],
            "record_hash": entry["record_hash"],
            "content_fingerprint": entry.get("content_fingerprint"),
            "author_id": entry["author_id"],
            "created_at": entry["created_at"],
            "entry_type": entry["entry_type"],
            "performative": entry["performative"],
            "content": entry["content"],
            "confidence": entry["confidence"],
            "tags": tags,
            "directed_to": directed_to,
            "metadata": json.loads(entry.get("metadata", "{}")),
        },

        # -- Link graph --
        "linked_entries": linked_entries,
        "linked_count": len(linked_entries),

        # -- Self-links for navigation --
        "links": _get_links_for_entry(conn, entry_id),
    }


def _fetch_linked_entries(conn: sqlite3.Connection, entry_id: int, hop_depth: int) -> list[dict]:
    """BFS graph traversal from entry_id up to hop_depth, returns full entry dicts."""
    visited: set[int] = {entry_id}
    frontier: set[int] = {entry_id}

    for _ in range(hop_depth):
        if not frontier:
            break
        next_frontier: set[int] = set()
        for eid in frontier:
            neighbors = _get_neighbor_ids(conn, eid)
            for nid in neighbors:
                if nid not in visited:
                    visited.add(nid)
                    next_frontier.add(nid)
        frontier = next_frontier

    # Remove self
    visited.discard(entry_id)
    if not visited:
        return []

    # Fetch full entries
    placeholders = ",".join("?" for _ in visited)
    rows = conn.execute(
        "SELECT * FROM entries WHERE id IN (" + placeholders + ") ORDER BY created_at ASC",
        list(visited),
    ).fetchall()

    results = []
    for row in rows:
        r = dict(row)
        r["tags"] = json.loads(r.get("tags", "[]"))
        r["directed_to"] = json.loads(r.get("directed_to", "[]"))
        r["metadata"] = json.loads(r.get("metadata", "{}"))
        r["links"] = _get_links_for_entry(conn, r["id"])
        results.append(r)

    return results


def _get_neighbor_ids(conn: sqlite3.Connection, entry_id: int) -> set[int]:
    outbound = conn.execute(
        "SELECT target_entry FROM links WHERE source_entry = ?", (entry_id,)
    ).fetchall()
    inbound = conn.execute(
        "SELECT source_entry FROM links WHERE target_entry = ?", (entry_id,)
    ).fetchall()
    return {r["target_entry"] for r in outbound} | {r["source_entry"] for r in inbound}


def _get_links_for_entry(conn: sqlite3.Connection, entry_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, source_entry, target_entry, link_type, author_id, "
        "created_at, annotation FROM links "
        "WHERE source_entry = ? OR target_entry = ?",
        (entry_id, entry_id),
    ).fetchall()
    return [dict(r) for r in rows]


def _stub_envelope(notif_row: dict, reason: str) -> dict:
    return {
        "fetch_mode": "nlip",
        "notification_id": notif_row.get("notification_id"),
        "status": notif_row.get("status"),
        "created_at": notif_row.get("notif_created_at"),
        "signal_score": 0.0,
        "directed_to_me": False,
        "entry": None,
        "linked_entries": [],
        "linked_count": 0,
        "links": [],
        "_stub_reason": reason,
    }


# ---------------------------------------------------------------------------
# SSE stream management — thread-safe queue per agent
# ---------------------------------------------------------------------------

_sse_queues: dict[str, asyncio.Queue] = {}
_sse_queues_lock = asyncio.Lock()


async def _sse_subscribe(agent_id: str) -> asyncio.Queue:
    """Subscribe to NLIP push stream for agent_id. Returns queue to read from."""
    async with _sse_queues_lock:
        if agent_id not in _sse_queues:
            _sse_queues[agent_id] = asyncio.Queue()
        _sse_queues[agent_id].put_nowait  # just ensure exists
        return _sse_queues[agent_id]


def sse_subscribe_sync(agent_id: str) -> None:
    """Sync wrapper — call after DB write to enqueue push for SSE subscribers."""
    # This is called from sync sqlite callbacks after a write.
    # We defer to a background task. In practice the caller uses
    # app.add_event_handler("startup", ...) or a background thread.
    pass


async def push_nlip_envelope(agent_id: str, envelope: dict) -> None:
    """Push a hydrated envelope to all SSE subscribers of agent_id."""
    async with _sse_queues_lock:
        if agent_id in _sse_queues:
            try:
                _sse_queues[agent_id].put_nowait(envelope)
            except asyncio.QueueFull:
                pass  # Drop if client is too slow — they'll reconnect with Last-Event-ID


async def _sse_unsubscribe(agent_id: str, q: asyncio.Queue) -> None:
    async with _sse_queues_lock:
        if agent_id in _sse_queues and _sse_queues[agent_id] is q:
            del _sse_queues[agent_id]
