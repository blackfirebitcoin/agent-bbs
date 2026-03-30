"""Tests for agent_runtime.bootstrap — cold-start bootstrap procedure.

Covers: BBS tag search, clustering by tag overlap, stub summary generation,
thread summary storage, bootstrap event recording, empty BBS handling.
"""

import json
import sqlite3

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def bbs_db():
    """BBS database with schema and test data."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    from agent_bbs.schema import create_tables
    create_tables(conn)
    # Register an agent for posting
    from agent_bbs.agents import register_agent
    register_agent(conn, agent_id="research-agent", display_name="Research Agent")
    return conn


@pytest.fixture()
def wm_db():
    """Fresh working memory database."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _post(bbs_db, content, tags, entry_type="finding", performative="inform"):
    """Helper to post an entry to BBS."""
    from agent_bbs.entries import post_entry
    return post_entry(
        bbs_db, author_id="research-agent", entry_type=entry_type,
        performative=performative, content=content, tags=tags,
    )


# ---------------------------------------------------------------------------
# Bootstrap tests
# ---------------------------------------------------------------------------

class TestBootstrap:
    def test_bootstrap_empty_bbs(self, bbs_db, wm_db):
        from agent_runtime.bootstrap import bootstrap_working_memory
        result = bootstrap_working_memory(
            bbs_db, wm_db, agent_id="my-agent",
            watch_tags=["nonexistent-tag"],
        )
        assert result["entries_fetched"] == 0
        assert result["clusters_created"] == 0

    def test_bootstrap_fetches_entries(self, bbs_db, wm_db):
        from agent_runtime.bootstrap import bootstrap_working_memory
        _post(bbs_db, "ML finding 1", tags=["ml", "research"])
        _post(bbs_db, "ML finding 2", tags=["ml"])
        _post(bbs_db, "NLP finding", tags=["nlp", "research"])

        result = bootstrap_working_memory(
            bbs_db, wm_db, agent_id="my-agent",
            watch_tags=["ml", "research"],
        )
        assert result["entries_fetched"] == 3
        assert result["clusters_created"] >= 1

    def test_bootstrap_creates_summaries(self, bbs_db, wm_db):
        from agent_runtime.bootstrap import bootstrap_working_memory
        _post(bbs_db, "Finding about quantum computing", tags=["quantum"])
        _post(bbs_db, "Another quantum discovery", tags=["quantum"])

        bootstrap_working_memory(
            bbs_db, wm_db, agent_id="my-agent", watch_tags=["quantum"],
        )
        rows = wm_db.execute("SELECT * FROM thread_summaries").fetchall()
        assert len(rows) == 1
        assert rows[0]["cluster_tag"] == "quantum"
        assert rows[0]["entry_count"] == 2

    def test_bootstrap_clusters_by_tag(self, bbs_db, wm_db):
        from agent_runtime.bootstrap import bootstrap_working_memory
        _post(bbs_db, "ML entry", tags=["ml"])
        _post(bbs_db, "NLP entry", tags=["nlp"])
        _post(bbs_db, "Both ML and NLP", tags=["ml", "nlp"])

        result = bootstrap_working_memory(
            bbs_db, wm_db, agent_id="my-agent", watch_tags=["ml", "nlp"],
        )
        assert result["clusters_created"] == 2
        ml_summary = wm_db.execute(
            "SELECT * FROM thread_summaries WHERE cluster_tag = 'ml'"
        ).fetchone()
        nlp_summary = wm_db.execute(
            "SELECT * FROM thread_summaries WHERE cluster_tag = 'nlp'"
        ).fetchone()
        assert ml_summary is not None
        assert nlp_summary is not None
        # "Both ML and NLP" should appear in both clusters
        ml_ids = json.loads(ml_summary["entry_ids"])
        nlp_ids = json.loads(nlp_summary["entry_ids"])
        # There should be overlap
        assert len(set(ml_ids) & set(nlp_ids)) >= 1

    def test_bootstrap_records_event(self, bbs_db, wm_db):
        from agent_runtime.bootstrap import bootstrap_working_memory
        _post(bbs_db, "Test entry", tags=["research"])

        bootstrap_working_memory(
            bbs_db, wm_db, agent_id="my-agent", watch_tags=["research"],
        )
        events = wm_db.execute(
            "SELECT * FROM events WHERE event_type = 'system_event'"
        ).fetchall()
        assert len(events) == 1
        payload = json.loads(events[0]["payload"])
        assert payload["action"] == "cold_start"
        assert payload["entries_found"] >= 1

    def test_bootstrap_deduplicates_across_tags(self, bbs_db, wm_db):
        from agent_runtime.bootstrap import bootstrap_working_memory
        # Entry matches both tags
        _post(bbs_db, "Dual tag entry", tags=["ml", "research"])

        result = bootstrap_working_memory(
            bbs_db, wm_db, agent_id="my-agent", watch_tags=["ml", "research"],
        )
        # Entry should be fetched only once
        assert result["entries_fetched"] == 1

    def test_bootstrap_respects_limit(self, bbs_db, wm_db):
        from agent_runtime.bootstrap import bootstrap_working_memory
        for i in range(10):
            _post(bbs_db, f"Finding {i}", tags=["research"])

        result = bootstrap_working_memory(
            bbs_db, wm_db, agent_id="my-agent",
            watch_tags=["research"], entries_per_tag=3,
        )
        assert result["entries_fetched"] == 3

    def test_stub_summary_contains_content(self, bbs_db, wm_db):
        from agent_runtime.bootstrap import bootstrap_working_memory
        _post(bbs_db, "Important discovery about protein folding", tags=["biology"])

        bootstrap_working_memory(
            bbs_db, wm_db, agent_id="my-agent", watch_tags=["biology"],
        )
        row = wm_db.execute(
            "SELECT summary_text FROM thread_summaries WHERE cluster_tag = 'biology'"
        ).fetchone()
        assert "protein folding" in row["summary_text"]

    def test_stub_summary_truncates_long_content(self, bbs_db, wm_db):
        from agent_runtime.bootstrap import bootstrap_working_memory
        long_content = "A" * 500
        _post(bbs_db, long_content, tags=["verbose"])

        bootstrap_working_memory(
            bbs_db, wm_db, agent_id="my-agent", watch_tags=["verbose"],
        )
        row = wm_db.execute(
            "SELECT summary_text FROM thread_summaries WHERE cluster_tag = 'verbose'"
        ).fetchone()
        assert "..." in row["summary_text"]
        assert len(row["summary_text"]) < 500
