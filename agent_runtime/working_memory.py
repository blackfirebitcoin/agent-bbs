"""Working memory SQLite schema and operations — Section 12 of the spec.

Agent-side database with:
- events: ground truth, append-only with compaction
- pending_notifications: local notification queue
- thread_summaries: cluster-level with dual relevance decay
- agent_actions: outbound audit trail
- FTS on summaries

Priority scoring (Section 10.3) and relevance decay (time_decay * access_boost).
"""

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
-- Events: ground truth, append-only with compaction at 90 days
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,
    event_type      TEXT NOT NULL,
    source          TEXT NOT NULL,       -- 'bbs_notification', 'user_message', 'system', etc.
    entry_id        INTEGER,             -- BBS entry ID if from BBS
    record_hash     TEXT,                -- BBS record hash if from BBS
    payload         TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    compacted       BOOLEAN DEFAULT FALSE,

    CHECK (event_type IN (
        'notification_received', 'entry_fetched', 'entry_posted',
        'link_created', 'subscription_created', 'user_message',
        'system_event', 'summary_generated', 'action_taken'
    ))
);

CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_entry ON events(entry_id);

-- Pending notifications: local queue mirroring BBS notification_queue
CREATE TABLE IF NOT EXISTS pending_notifications (
    id                  INTEGER PRIMARY KEY,
    bbs_notification_id INTEGER UNIQUE,
    entry_id            INTEGER NOT NULL,
    record_hash         TEXT,
    author_id           TEXT NOT NULL,
    entry_type          TEXT NOT NULL,
    performative        TEXT NOT NULL,
    tags                TEXT DEFAULT '[]',
    confidence          REAL DEFAULT 0.5,
    directed_to_me      BOOLEAN DEFAULT FALSE,
    created_at          TEXT NOT NULL,
    received_at         TEXT NOT NULL,
    priority_score      REAL DEFAULT 0.0,
    status              TEXT DEFAULT 'pending',
    processed_at        TEXT,

    CHECK (status IN ('pending', 'scored', 'batched', 'processed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_notifications(status);
CREATE INDEX IF NOT EXISTS idx_pending_priority ON pending_notifications(priority_score DESC);

-- Thread summaries: cluster-level with dual relevance decay
CREATE TABLE IF NOT EXISTS thread_summaries (
    id              INTEGER PRIMARY KEY,
    cluster_tag     TEXT NOT NULL,       -- primary tag or topic cluster
    summary_text    TEXT NOT NULL,
    entry_ids       TEXT DEFAULT '[]',   -- JSON array of BBS entry IDs in this cluster
    entry_count     INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_accessed   TEXT NOT NULL,
    access_count    INTEGER DEFAULT 1,

    UNIQUE(cluster_tag)
);

CREATE INDEX IF NOT EXISTS idx_summaries_updated ON thread_summaries(updated_at);
CREATE INDEX IF NOT EXISTS idx_summaries_accessed ON thread_summaries(last_accessed);

-- FTS on summaries for quick retrieval
CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
    cluster_tag, summary_text,
    content=thread_summaries, content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS summaries_fts_insert AFTER INSERT ON thread_summaries BEGIN
    INSERT INTO summaries_fts(rowid, cluster_tag, summary_text)
    VALUES (new.id, new.cluster_tag, new.summary_text);
END;

CREATE TRIGGER IF NOT EXISTS summaries_fts_update AFTER UPDATE ON thread_summaries BEGIN
    INSERT INTO summaries_fts(summaries_fts, rowid, cluster_tag, summary_text)
    VALUES ('delete', old.id, old.cluster_tag, old.summary_text);
    INSERT INTO summaries_fts(rowid, cluster_tag, summary_text)
    VALUES (new.id, new.cluster_tag, new.summary_text);
END;

-- Agent actions: outbound audit trail
CREATE TABLE IF NOT EXISTS agent_actions (
    id              INTEGER PRIMARY KEY,
    action_type     TEXT NOT NULL,       -- 'post', 'link', 'subscribe', 'read', 'search'
    bbs_entry_id    INTEGER,             -- resulting entry ID if applicable
    record_hash     TEXT,
    payload         TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    trigger_event_id INTEGER REFERENCES events(id),

    CHECK (action_type IN ('post', 'link', 'subscribe', 'read', 'search', 'notify'))
);

CREATE INDEX IF NOT EXISTS idx_actions_type ON agent_actions(action_type);
CREATE INDEX IF NOT EXISTS idx_actions_created ON agent_actions(created_at);
"""


def create_working_memory_tables(conn: sqlite3.Connection) -> None:
    """Create all working memory tables."""
    conn.executescript(_SCHEMA_SQL)
    conn.commit()


# ---------------------------------------------------------------------------
# Priority scoring (Section 10.3)
# ---------------------------------------------------------------------------

def compute_priority_score(
    notification: dict,
    *,
    agent_id: str,
    watch_tags: list[str],
    weights: dict,
) -> float:
    """Compute priority score for a notification envelope.

    Uses scorer weights from agent-config.yaml:
    - directed_to_me: high priority if entry is directed at this agent
    - contradiction_of_mine: high if someone contradicts our entry
    - request_to_me: high if a request is directed at us
    - watched_tag_match: moderate for entries in watched tags
    - high_confidence / low_confidence: multiplier based on confidence
    - synthesis_proposed: moderate for proposed syntheses
    - question_in_domain: moderate for questions matching watched tags
    - recency_halflife_hours: time decay half-life
    """
    score = 0.0

    # Directed-to-me
    if notification.get("directed_to_me"):
        score += weights.get("directed_to_me", 10.0)

    # Request directed at me
    if notification.get("performative") == "request" and notification.get("directed_to_me"):
        score += weights.get("request_to_me", 9.0)

    # Contradiction (approximation — we check performative, not whether it contradicts OUR entry)
    if notification.get("performative") == "disconfirm":
        score += weights.get("contradiction_of_mine", 8.0)

    # Proposed synthesis
    if notification.get("entry_type") == "synthesis" and notification.get("performative") == "propose":
        score += weights.get("synthesis_proposed", 4.0)

    # Question in domain
    if notification.get("entry_type") == "question":
        notif_tags = _parse_tags(notification.get("tags", "[]"))
        if any(t in watch_tags for t in notif_tags):
            score += weights.get("question_in_domain", 3.5)

    # Watched tag match
    notif_tags = _parse_tags(notification.get("tags", "[]"))
    matching_tags = sum(1 for t in notif_tags if t in watch_tags)
    if matching_tags > 0:
        score += weights.get("watched_tag_match", 3.0) * matching_tags

    # Confidence multiplier
    confidence = notification.get("confidence", 0.5)
    if confidence >= 0.8:
        score *= weights.get("high_confidence", 1.5)
    elif confidence <= 0.3:
        score *= weights.get("low_confidence", 0.5)

    # Time decay
    halflife_hours = weights.get("recency_halflife_hours", 24.0)
    created_at = notification.get("created_at", "")
    if created_at:
        score *= _time_decay(created_at, halflife_hours)

    return round(score, 4)


def _parse_tags(tags_val) -> list[str]:
    """Parse tags from either a JSON string or a list."""
    if isinstance(tags_val, list):
        return tags_val
    if isinstance(tags_val, str):
        try:
            return json.loads(tags_val)
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def _time_decay(created_at: str, halflife_hours: float) -> float:
    """Exponential decay based on age of the notification."""
    try:
        # Handle various ISO formats
        created_at_clean = created_at.replace("Z", "+00:00")
        ts = datetime.fromisoformat(created_at_clean)
        now = datetime.now(timezone.utc)
        age_hours = (now - ts).total_seconds() / 3600.0
        if age_hours < 0:
            age_hours = 0
        return math.pow(0.5, age_hours / halflife_hours)
    except (ValueError, TypeError):
        return 1.0  # Can't parse — no decay


# ---------------------------------------------------------------------------
# Relevance decay for thread summaries (Section 12)
# ---------------------------------------------------------------------------

def compute_relevance(
    summary: dict,
    *,
    time_halflife_hours: float = 168.0,  # 7 days
    access_boost_factor: float = 0.1,
) -> float:
    """Compute dual relevance decay: time_decay * access_boost.

    - time_decay: exponential decay from last update
    - access_boost: 1.0 + (access_count * access_boost_factor)
    """
    updated_at = summary.get("updated_at", "")
    access_count = summary.get("access_count", 1)

    time_decay = _time_decay(updated_at, time_halflife_hours)
    access_boost = 1.0 + (access_count * access_boost_factor)

    return round(time_decay * access_boost, 4)


# ---------------------------------------------------------------------------
# Working memory operations
# ---------------------------------------------------------------------------

def record_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    source: str,
    entry_id: Optional[int] = None,
    record_hash: Optional[str] = None,
    payload: Optional[dict] = None,
) -> int:
    """Record an event in working memory. Returns the event ID."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        "INSERT INTO events (event_type, source, entry_id, record_hash, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event_type, source, entry_id, record_hash, json.dumps(payload or {}), now),
    )
    conn.commit()
    return cur.lastrowid


def store_pending_notification(
    conn: sqlite3.Connection,
    *,
    notification: dict,
) -> int:
    """Store a BBS notification envelope in local pending queue. Returns local ID."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tags = notification.get("tags", "[]")
    if isinstance(tags, list):
        tags = json.dumps(tags)

    cur = conn.execute(
        "INSERT OR IGNORE INTO pending_notifications "
        "(bbs_notification_id, entry_id, record_hash, author_id, "
        " entry_type, performative, tags, confidence, directed_to_me, "
        " created_at, received_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
        (
            notification.get("notification_id"),
            notification["entry_id"],
            notification.get("record_hash"),
            notification["author_id"],
            notification["entry_type"],
            notification["performative"],
            tags,
            notification.get("confidence", 0.5),
            notification.get("directed_to_me", False),
            notification.get("created_at", now),
            now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def score_pending_notifications(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    watch_tags: list[str],
    weights: dict,
) -> int:
    """Score all pending notifications and update their priority. Returns count scored."""
    rows = conn.execute(
        "SELECT * FROM pending_notifications WHERE status = 'pending'"
    ).fetchall()

    count = 0
    for row in rows:
        notif = dict(row)
        score = compute_priority_score(
            notif, agent_id=agent_id, watch_tags=watch_tags, weights=weights
        )
        conn.execute(
            "UPDATE pending_notifications SET priority_score = ?, status = 'scored' WHERE id = ?",
            (score, row["id"]),
        )
        count += 1

    conn.commit()
    return count


def get_scored_batch(
    conn: sqlite3.Connection,
    *,
    limit: int = 10,
    min_score: float = 0.0,
) -> list[dict]:
    """Get the top-priority scored notifications as a batch."""
    rows = conn.execute(
        "SELECT * FROM pending_notifications "
        "WHERE status = 'scored' AND priority_score >= ? "
        "ORDER BY priority_score DESC LIMIT ?",
        (min_score, limit),
    ).fetchall()

    batch = [dict(r) for r in rows]

    # Mark as batched
    if batch:
        ids = [n["id"] for n in batch]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE pending_notifications SET status = 'batched' WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()

    return batch


def mark_notifications_processed(
    conn: sqlite3.Connection,
    *,
    notification_ids: list[int],
) -> None:
    """Mark local notifications as processed."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    placeholders = ",".join("?" for _ in notification_ids)
    conn.execute(
        f"UPDATE pending_notifications SET status = 'processed', processed_at = ? "
        f"WHERE id IN ({placeholders})",
        [now] + notification_ids,
    )
    conn.commit()


def upsert_thread_summary(
    conn: sqlite3.Connection,
    *,
    cluster_tag: str,
    summary_text: str,
    entry_ids: list[int],
) -> int:
    """Create or update a thread summary. Returns the summary ID."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry_ids_json = json.dumps(entry_ids)

    existing = conn.execute(
        "SELECT id, entry_ids FROM thread_summaries WHERE cluster_tag = ?",
        (cluster_tag,),
    ).fetchone()

    if existing:
        # Merge entry IDs
        old_ids = json.loads(existing["entry_ids"])
        merged = sorted(set(old_ids + entry_ids))
        conn.execute(
            "UPDATE thread_summaries SET summary_text = ?, entry_ids = ?, "
            "entry_count = ?, updated_at = ?, access_count = access_count + 1 "
            "WHERE id = ?",
            (summary_text, json.dumps(merged), len(merged), now, existing["id"]),
        )
        conn.commit()
        return existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO thread_summaries "
            "(cluster_tag, summary_text, entry_ids, entry_count, "
            " created_at, updated_at, last_accessed, access_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (cluster_tag, summary_text, entry_ids_json, len(entry_ids), now, now, now),
        )
        conn.commit()
        return cur.lastrowid


def touch_summary(conn: sqlite3.Connection, *, summary_id: int) -> None:
    """Update last_accessed and increment access_count for relevance tracking."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE thread_summaries SET last_accessed = ?, access_count = access_count + 1 "
        "WHERE id = ?",
        (now, summary_id),
    )
    conn.commit()


def record_action(
    conn: sqlite3.Connection,
    *,
    action_type: str,
    bbs_entry_id: Optional[int] = None,
    record_hash: Optional[str] = None,
    payload: Optional[dict] = None,
    trigger_event_id: Optional[int] = None,
) -> int:
    """Record an outbound action in the audit trail. Returns action ID."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        "INSERT INTO agent_actions "
        "(action_type, bbs_entry_id, record_hash, payload, created_at, trigger_event_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (action_type, bbs_entry_id, record_hash, json.dumps(payload or {}), now, trigger_event_id),
    )
    conn.commit()
    return cur.lastrowid


def compact_events(conn: sqlite3.Connection, *, older_than_days: int = 90) -> int:
    """Mark events older than N days as compacted (retain audit events).

    Returns count of events compacted.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        "UPDATE events SET compacted = TRUE "
        "WHERE compacted = FALSE AND created_at < ? "
        "AND event_type NOT IN ('action_taken', 'system_event')",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount
