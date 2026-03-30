"""Cold-start bootstrap — Section 11.2 of the spec.

Procedure:
1. Search BBS for each watched tag (recent 50 entries)
2. Cluster results by tag overlap
3. Generate seed summary placeholders (raw content as summary until LLM is wired up)
4. Store summaries in working memory
"""

import json
import sqlite3
from collections import defaultdict
from typing import Optional

from agent_runtime.working_memory import (
    create_working_memory_tables,
    record_event,
    upsert_thread_summary,
)


def bootstrap_working_memory(
    bbs_conn: sqlite3.Connection,
    wm_conn: sqlite3.Connection,
    *,
    agent_id: str,
    watch_tags: list[str],
    entries_per_tag: int = 50,
) -> dict:
    """Execute cold-start bootstrap.

    Searches BBS for watched tags, clusters by tag overlap,
    and stores seed summaries in working memory.

    Args:
        bbs_conn: Connection to BBS database
        wm_conn: Connection to working memory database
        agent_id: This agent's ID
        watch_tags: Tags to bootstrap from
        entries_per_tag: Max entries to fetch per tag (default 50)

    Returns:
        {
            "entries_fetched": int,
            "clusters_created": int,
            "tags_searched": list[str],
        }
    """
    # Ensure working memory tables exist
    create_working_memory_tables(wm_conn)

    # Step 1: Search BBS for each watched tag
    all_entries = {}  # entry_id -> entry dict (dedup across tags)

    for tag in watch_tags:
        entries = _search_bbs_by_tag(bbs_conn, tag=tag, limit=entries_per_tag)
        for entry in entries:
            eid = entry["id"]
            if eid not in all_entries:
                all_entries[eid] = entry

    if not all_entries:
        return {
            "entries_fetched": 0,
            "clusters_created": 0,
            "tags_searched": watch_tags,
        }

    # Record bootstrap event
    record_event(
        wm_conn,
        event_type="system_event",
        source="bootstrap",
        payload={
            "action": "cold_start",
            "tags_searched": watch_tags,
            "entries_found": len(all_entries),
        },
    )

    # Step 2: Cluster by tag overlap
    clusters = _cluster_by_tags(all_entries, watch_tags=watch_tags)

    # Step 3: Generate seed summaries (stub — raw content as summary)
    clusters_created = 0
    for cluster_tag, entry_ids in clusters.items():
        entries_in_cluster = [all_entries[eid] for eid in entry_ids if eid in all_entries]
        summary_text = _generate_stub_summary(entries_in_cluster)

        upsert_thread_summary(
            wm_conn,
            cluster_tag=cluster_tag,
            summary_text=summary_text,
            entry_ids=sorted(entry_ids),
        )
        clusters_created += 1

    return {
        "entries_fetched": len(all_entries),
        "clusters_created": clusters_created,
        "tags_searched": watch_tags,
    }


def _search_bbs_by_tag(
    conn: sqlite3.Connection,
    *,
    tag: str,
    limit: int = 50,
) -> list[dict]:
    """Search BBS entries that contain the given tag, ordered by recency."""
    # Use FTS to search tags, or fall back to LIKE query
    try:
        rows = conn.execute(
            "SELECT e.id, e.record_hash, e.author_id, e.created_at, "
            "       e.entry_type, e.performative, e.content, "
            "       e.confidence, e.tags, e.directed_to "
            "FROM entries e "
            "WHERE e.tags LIKE ? "
            "ORDER BY e.created_at DESC LIMIT ?",
            (f'%"{tag}"%', limit),
        ).fetchall()
    except Exception:
        return []

    return [dict(r) for r in rows]


def _cluster_by_tags(
    entries: dict[int, dict],
    *,
    watch_tags: list[str],
) -> dict[str, list[int]]:
    """Cluster entries by their primary watched tag.

    Each entry is assigned to the watched tag it matches best
    (most specific match). Entries matching multiple watched tags
    are assigned to each matching tag's cluster.
    """
    clusters: dict[str, list[int]] = defaultdict(list)

    for eid, entry in entries.items():
        tags_raw = entry.get("tags", "[]")
        if isinstance(tags_raw, str):
            try:
                entry_tags = json.loads(tags_raw)
            except (json.JSONDecodeError, TypeError):
                entry_tags = []
        else:
            entry_tags = tags_raw

        matched = [t for t in entry_tags if t in watch_tags]
        if not matched:
            # Assign to first watch tag as fallback (it was found via that search)
            if watch_tags:
                clusters[watch_tags[0]].append(eid)
        else:
            for tag in matched:
                clusters[tag].append(eid)

    # Deduplicate within clusters
    return {tag: sorted(set(ids)) for tag, ids in clusters.items()}


def _generate_stub_summary(entries: list[dict]) -> str:
    """Generate a stub summary from raw entry content.

    This is a placeholder — actual LLM summarization will be wired up later.
    For now, concatenates entry content with metadata headers.
    """
    if not entries:
        return "(no entries)"

    lines = []
    for entry in entries[:10]:  # Cap at 10 for reasonable summary size
        etype = entry.get("entry_type", "?")
        perf = entry.get("performative", "?")
        conf = entry.get("confidence", 0.5)
        content = entry.get("content", "")
        # Truncate long content
        if len(content) > 200:
            content = content[:200] + "..."
        lines.append(f"[{etype}/{perf} conf={conf}] {content}")

    remaining = len(entries) - 10
    if remaining > 0:
        lines.append(f"(+{remaining} more entries)")

    return "\n".join(lines)
