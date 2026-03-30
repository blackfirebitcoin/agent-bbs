"""Read operation per Section 7.2 of the spec.

Fetch entries by ID or record_hash with optional link graph and graph traversal.
"""

import sqlite3
from typing import Optional

_ENTRY_COLS = (
    "e.id, e.record_hash, e.content_fingerprint, e.author_id, "
    "e.created_at, e.entry_type, e.performative, e.content, "
    "e.confidence, e.tags, e.directed_to, e.metadata"
)


def read_entries(
    conn: sqlite3.Connection,
    *,
    entry_ids: Optional[list[int]] = None,
    record_hashes: Optional[list[str]] = None,
    include_links: bool = False,
    hops: int = 0,
    link_types: Optional[list[str]] = None,
    direction: str = "both",
) -> list[dict]:
    """Fetch entries by ID or record_hash.

    Args:
        entry_ids: List of local integer IDs.
        record_hashes: List of record hash strings.
        include_links: If True, attach a 'links' key with all links touching each entry.
        hops: Number of graph traversal hops (0 = no traversal).
        link_types: Filter traversal to these link types only (None = all).
        direction: 'outbound', 'inbound', or 'both'.
    """
    # Resolve seed entry IDs
    seed_ids = set()
    if entry_ids:
        seed_ids.update(entry_ids)
    if record_hashes:
        placeholders = ",".join("?" for _ in record_hashes)
        rows = conn.execute(
            f"SELECT id FROM entries WHERE record_hash IN ({placeholders})",
            record_hashes,
        ).fetchall()
        seed_ids.update(r["id"] for r in rows)

    if not seed_ids:
        return []

    # Graph traversal
    if hops > 0:
        all_ids = _traverse(conn, seed_ids, hops, link_types, direction)
    else:
        all_ids = seed_ids

    # Fetch entries
    placeholders = ",".join("?" for _ in all_ids)
    rows = conn.execute(
        f"SELECT {_ENTRY_COLS} FROM entries e WHERE e.id IN ({placeholders})",
        list(all_ids),
    ).fetchall()

    results = [dict(r) for r in rows]

    # Attach links if requested
    if include_links:
        for entry in results:
            entry["links"] = _get_links_for_entry(conn, entry["id"])

    return results


def _traverse(
    conn: sqlite3.Connection,
    seed_ids: set[int],
    hops: int,
    link_types: Optional[list[str]],
    direction: str,
) -> set[int]:
    """BFS graph traversal from seed_ids for the given number of hops."""
    visited = set(seed_ids)
    frontier = set(seed_ids)

    for _ in range(hops):
        if not frontier:
            break
        next_frontier = set()
        for eid in frontier:
            neighbors = _get_neighbors(conn, eid, link_types, direction)
            for n in neighbors:
                if n not in visited:
                    next_frontier.add(n)
                    visited.add(n)
        frontier = next_frontier

    return visited


def _get_neighbors(
    conn: sqlite3.Connection,
    entry_id: int,
    link_types: Optional[list[str]],
    direction: str,
) -> set[int]:
    """Get neighboring entry IDs reachable via links."""
    neighbors = set()

    type_filter = ""
    type_params: list = []
    if link_types:
        placeholders = ",".join("?" for _ in link_types)
        type_filter = f" AND link_type IN ({placeholders})"
        type_params = list(link_types)

    if direction in ("outbound", "both"):
        rows = conn.execute(
            f"SELECT target_entry FROM links WHERE source_entry = ?{type_filter}",
            [entry_id] + type_params,
        ).fetchall()
        neighbors.update(r["target_entry"] for r in rows)

    if direction in ("inbound", "both"):
        rows = conn.execute(
            f"SELECT source_entry FROM links WHERE target_entry = ?{type_filter}",
            [entry_id] + type_params,
        ).fetchall()
        neighbors.update(r["source_entry"] for r in rows)

    return neighbors


def _get_links_for_entry(conn: sqlite3.Connection, entry_id: int) -> list[dict]:
    """Get all links where entry is source or target."""
    rows = conn.execute(
        "SELECT id, source_entry, target_entry, link_type, author_id, "
        "created_at, annotation FROM links "
        "WHERE source_entry = ? OR target_entry = ?",
        (entry_id, entry_id),
    ).fetchall()
    return [dict(r) for r in rows]
