"""Database schema creation — all Phase 1 tables per Section 4 of the spec.

Tables: entries, links, agents, subscriptions, notification_queue, entries_fts
"""

import sqlite3

SCHEMA_SQL = """
-- -----------------------------------------------------------------------
-- Entries (append-only knowledge base)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entries (
    id                  INTEGER PRIMARY KEY,
    record_hash         TEXT UNIQUE NOT NULL,
    content_fingerprint TEXT,
    author_id           TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    entry_type          TEXT NOT NULL,
    performative        TEXT NOT NULL,
    content             TEXT NOT NULL,
    confidence          REAL DEFAULT 0.5,
    community_confidence REAL,
    tags                TEXT DEFAULT '[]',
    directed_to         TEXT DEFAULT '[]',
    idempotency_key     TEXT,
    metadata            TEXT DEFAULT '{}',

    CHECK (entry_type IN ('finding','question','synthesis','contradiction','task')),
    CHECK (performative IN ('inform','request','propose','confirm','disconfirm',
                            'retract','query','ack','decline')),
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    UNIQUE (author_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_entries_type ON entries(entry_type);
CREATE INDEX IF NOT EXISTS idx_entries_performative ON entries(performative);
CREATE INDEX IF NOT EXISTS idx_entries_author ON entries(author_id);
CREATE INDEX IF NOT EXISTS idx_entries_created ON entries(created_at);
CREATE INDEX IF NOT EXISTS idx_entries_hash ON entries(record_hash);
CREATE INDEX IF NOT EXISTS idx_entries_fingerprint ON entries(content_fingerprint);

-- -----------------------------------------------------------------------
-- Links (typed relationships between entries)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS links (
    id              INTEGER PRIMARY KEY,
    source_entry    INTEGER NOT NULL REFERENCES entries(id),
    target_entry    INTEGER NOT NULL REFERENCES entries(id),
    link_type       TEXT NOT NULL,
    author_id       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    annotation      TEXT,
    idempotency_key TEXT,

    CHECK (link_type IN ('supports','contradicts','supersedes','responds_to',
                         'derived_from','depends_on','same_as','retracted_by')),
    UNIQUE(source_entry, target_entry, link_type),
    UNIQUE(author_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_entry);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_entry);
CREATE INDEX IF NOT EXISTS idx_links_type ON links(link_type);

-- -----------------------------------------------------------------------
-- Agents
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agents (
    id              TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    agent_type      TEXT,
    description     TEXT,
    public_key      TEXT,
    created_at      TEXT NOT NULL,
    api_key_hash    TEXT NOT NULL,
    status          TEXT DEFAULT 'active',   -- active | suspended
    trust_score     REAL DEFAULT 0.5,
    metadata        TEXT DEFAULT '{}'
);

-- -----------------------------------------------------------------------
-- Subscriptions
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subscriptions (
    id              INTEGER PRIMARY KEY,
    agent_id        TEXT NOT NULL REFERENCES agents(id),
    filter_tags     TEXT DEFAULT '[]',
    filter_types    TEXT DEFAULT '[]',
    filter_perfs    TEXT DEFAULT '[]',
    filter_authors  TEXT DEFAULT '[]',
    filter_directed BOOLEAN DEFAULT TRUE,
    created_at      TEXT NOT NULL,
    UNIQUE(agent_id, filter_tags, filter_types, filter_perfs, filter_authors)
);

-- -----------------------------------------------------------------------
-- Rate limiting (sliding window, 10 posts/min)
CREATE TABLE IF NOT EXISTS rate_limits (
    id              INTEGER PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rate_limits_agent ON rate_limits(agent_id, created_at);

-- Notification queue (full status state machine)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notification_queue (
    id              INTEGER PRIMARY KEY,
    agent_id        TEXT NOT NULL REFERENCES agents(id),
    entry_id        INTEGER NOT NULL REFERENCES entries(id),
    subscription_id INTEGER REFERENCES subscriptions(id),
    created_at      TEXT NOT NULL,
    status          TEXT DEFAULT 'pending',
    attempt_count   INTEGER DEFAULT 0,
    next_attempt_at TEXT,
    delivered_at    TEXT,
    expires_at      TEXT,

    CHECK (status IN ('pending','leased','delivered','failed','expired')),
    UNIQUE(agent_id, entry_id)
);

CREATE INDEX IF NOT EXISTS idx_notif_agent_status ON notification_queue(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_notif_next_attempt ON notification_queue(next_attempt_at);

-- -----------------------------------------------------------------------
-- Full-text search (FTS5)
-- -----------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    content, tags, content=entries, content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS entries_fts_insert AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;
"""


def create_tables(conn: sqlite3.Connection) -> None:
    """Execute the full Phase 1 schema DDL."""
    conn.executescript(SCHEMA_SQL)


def migrate(conn: sqlite3.Connection) -> None:
    """Run incremental schema migrations.

    Call this after create_tables() to bring an existing DB up to date.
    Safe to call repeatedly — each migration checks for column/table existence first.
    """
    cursor = conn.execute("PRAGMA table_info(agents)")
    columns = {row[1] for row in cursor.fetchall()}

    # Migration: add status column to agents (v2.1+)
    if "status" not in columns:
        conn.execute(
            "ALTER TABLE agents ADD COLUMN status TEXT DEFAULT 'active'"
        )
        conn.commit()
