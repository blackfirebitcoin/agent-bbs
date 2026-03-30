"""Search operations — FTS5-backed full-text search per Section 7.3.

Supports: full-text query, tag/type/performative/author filtering,
min_confidence, since date, pagination, and graph traversal on results.
"""

import json
import sqlite3
from typing import Optional


def search_entries(
    conn: sqlite3.Connection,
    *,
    q: str,
    tags: Optional[list[str]] = None,
    entry_type: Optional[str] = None,
    performative: Optional[str] = None,
    author: Optional[str] = None,
    min_confidence: Optional[float] = None,
    since: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    hops: int = 0,
    link_types: Optional[list[str]] = None,
    direction: str = "both",
) -> list[dict]:
    """Full-text search with filters and optional graph traversal.

    Filters are AND-combined: all specified filters must match.
    Graph traversal (hops) expands from the search result set.
    """
    # Base FTS query — "*" means return all entries (no FTS filter)
    if q.strip() == "*":
        where_clauses = ["1=1"]
        params = []
    else:
        where_clauses = ["entries_fts MATCH ?"]
        params = [q]

    if entry_type is not None:
        where_clauses.append("e.entry_type = ?")
        params.append(entry_type)

    if performative is not None:
        where_clauses.append("e.performative = ?")
        params.append(performative)

    if author is not None:
        where_clauses.append("e.author_id = ?")
        params.append(author)

    if min_confidence is not None:
        where_clauses.append("e.confidence >= ?")
        params.append(min_confidence)

    if since is not None:
        where_clauses.append("e.created_at >= ?")
        params.append(since)

    where_sql = " AND ".join(where_clauses)

    rows = conn.execute(
        f"SELECT e.id, e.record_hash, e.content_fingerprint, e.author_id, "
        f"       e.created_at, e.entry_type, e.performative, e.content, "
        f"       e.confidence, e.tags, e.directed_to, e.metadata "
        f"FROM entries_fts AS f "
        f"JOIN entries AS e ON e.id = f.rowid "
        f"WHERE {where_sql} "
        f"ORDER BY rank "
        f"LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    results = [dict(r) for r in rows]

    # Post-filter by tags (exact JSON array membership)
    if tags:
        tag_set = set(tags)
        results = [
            r for r in results
            if tag_set.intersection(set(json.loads(r["tags"])))
        ]

    # Graph traversal on results
    if hops > 0 and results:
        from agent_bbs.read import _traverse
        seed_ids = {r["id"] for r in results}
        all_ids = _traverse(conn, seed_ids, hops, link_types, direction)
        # Fetch additional entries not already in results
        existing_ids = {r["id"] for r in results}
        new_ids = all_ids - existing_ids
        if new_ids:
            placeholders = ",".join("?" for _ in new_ids)
            extra_rows = conn.execute(
                f"SELECT e.id, e.record_hash, e.content_fingerprint, e.author_id, "
                f"       e.created_at, e.entry_type, e.performative, e.content, "
                f"       e.confidence, e.tags, e.directed_to, e.metadata "
                f"FROM entries e WHERE e.id IN ({placeholders})",
                list(new_ids),
            ).fetchall()
            results.extend(dict(r) for r in extra_rows)

    return results
