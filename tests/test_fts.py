"""Tests for FTS5 full-text search — trigger sync and query.

Spec reference: Section 4.6 of agent-bbs-v2-technical-proposal.md

The entries_fts_insert trigger keeps entries_fts in sync automatically.
"""

import pytest

from agent_bbs.entries import post_entry
from agent_bbs.search import search_entries


class TestFTSTriggerSync:
    """The FTS5 trigger must keep the search index in sync on INSERT."""

    def test_insert_populates_fts(self, db_with_schema):
        post_entry(db_with_schema, author_id="a1", entry_type="finding",
                   performative="inform", content="quantum entanglement breakthrough")

        rows = db_with_schema.execute(
            "SELECT rowid FROM entries_fts WHERE entries_fts MATCH 'quantum'"
        ).fetchall()
        assert len(rows) == 1

    def test_multiple_inserts_all_indexed(self, db_with_schema):
        post_entry(db_with_schema, author_id="a1", entry_type="finding",
                   performative="inform", content="alpha discovery")
        post_entry(db_with_schema, author_id="a1", entry_type="finding",
                   performative="inform", content="beta analysis")
        post_entry(db_with_schema, author_id="a1", entry_type="finding",
                   performative="inform", content="gamma radiation alpha")

        rows = db_with_schema.execute(
            "SELECT rowid FROM entries_fts WHERE entries_fts MATCH 'alpha'"
        ).fetchall()
        assert len(rows) == 2

    def test_tags_indexed(self, db_with_schema):
        post_entry(db_with_schema, author_id="a1", entry_type="finding",
                   performative="inform", content="some content",
                   tags=["machine-learning", "nlp"])

        # FTS5 treats '-' as NOT operator, so search for the component word
        rows = db_with_schema.execute(
            "SELECT rowid FROM entries_fts WHERE entries_fts MATCH 'nlp'"
        ).fetchall()
        assert len(rows) == 1

        # Also verify the tag "machine" component is findable
        rows2 = db_with_schema.execute(
            "SELECT rowid FROM entries_fts WHERE entries_fts MATCH 'machine'"
        ).fetchall()
        assert len(rows2) == 1


class TestFullTextSearch:
    """search_entries returns expected entries by content and tags."""

    def _seed(self, db):
        post_entry(db, author_id="a1", entry_type="finding",
                   performative="inform", content="Python is great for data science",
                   tags=["python", "data-science"])
        post_entry(db, author_id="a1", entry_type="finding",
                   performative="inform", content="Rust provides memory safety",
                   tags=["rust", "systems"])
        post_entry(db, author_id="a2", entry_type="question",
                   performative="query", content="How does Python garbage collection work?",
                   tags=["python", "internals"])

    def test_search_by_content_word(self, db_with_schema):
        self._seed(db_with_schema)
        results = search_entries(db_with_schema, q="Python")
        assert len(results) == 2
        contents = [r["content"] for r in results]
        assert all("Python" in c for c in contents)

    def test_search_by_tag(self, db_with_schema):
        self._seed(db_with_schema)
        results = search_entries(db_with_schema, q="rust")
        assert len(results) == 1
        assert "Rust" in results[0]["content"]

    def test_search_no_results(self, db_with_schema):
        self._seed(db_with_schema)
        results = search_entries(db_with_schema, q="javascript")
        assert len(results) == 0

    def test_search_returns_entry_fields(self, db_with_schema):
        self._seed(db_with_schema)
        results = search_entries(db_with_schema, q="memory")
        assert len(results) == 1
        r = results[0]
        assert "id" in r
        assert "record_hash" in r
        assert "author_id" in r
        assert "entry_type" in r
        assert r["entry_type"] == "finding"
