"""Tests for the Search operation (Phase 2).

Spec reference: Section 7.3
- Full-text query, tag/type/performative/author filtering
- min_confidence, since date, pagination (limit/offset)
- Search + graph traversal (hops) on results
"""

import pytest

from agent_bbs.entries import post_entry
from agent_bbs.links import create_link
from agent_bbs.search import search_entries


def _seed_entries(db):
    """Create a varied dataset for search testing."""
    e1 = post_entry(db, author_id="alice", entry_type="finding",
                    performative="inform", content="Quantum computing advances",
                    confidence=0.9, tags=["quantum", "computing"])
    e2 = post_entry(db, author_id="bob", entry_type="question",
                    performative="query", content="How does quantum entanglement work?",
                    confidence=0.7, tags=["quantum", "physics"])
    e3 = post_entry(db, author_id="alice", entry_type="synthesis",
                    performative="propose", content="Machine learning models improve",
                    confidence=0.85, tags=["ml", "ai"])
    e4 = post_entry(db, author_id="carol", entry_type="finding",
                    performative="inform", content="Quantum error correction breakthrough",
                    confidence=0.95, tags=["quantum", "error-correction"])
    e5 = post_entry(db, author_id="bob", entry_type="task",
                    performative="request", content="Review the quantum paper",
                    confidence=0.6, tags=["quantum", "review"])
    return e1, e2, e3, e4, e5


class TestFullTextQuery:
    """FTS query on content and tags."""

    def test_search_by_content(self, db_with_schema):
        _seed_entries(db_with_schema)
        results = search_entries(db_with_schema, q="quantum")
        assert len(results) == 4  # e1, e2, e4, e5

    def test_search_by_tag_word(self, db_with_schema):
        _seed_entries(db_with_schema)
        results = search_entries(db_with_schema, q="ml")
        assert len(results) == 1
        assert results[0]["author_id"] == "alice"


class TestFilterByType:
    """entry_type filter."""

    def test_filter_finding(self, db_with_schema):
        _seed_entries(db_with_schema)
        results = search_entries(db_with_schema, q="quantum",
                                 entry_type="finding")
        assert all(r["entry_type"] == "finding" for r in results)
        assert len(results) == 2  # e1, e4

    def test_filter_question(self, db_with_schema):
        _seed_entries(db_with_schema)
        results = search_entries(db_with_schema, q="quantum",
                                 entry_type="question")
        assert len(results) == 1


class TestFilterByPerformative:
    """performative filter."""

    def test_filter_request(self, db_with_schema):
        _seed_entries(db_with_schema)
        results = search_entries(db_with_schema, q="quantum",
                                 performative="request")
        assert len(results) == 1
        assert results[0]["performative"] == "request"


class TestFilterByAuthor:
    """author filter."""

    def test_filter_by_author(self, db_with_schema):
        _seed_entries(db_with_schema)
        results = search_entries(db_with_schema, q="quantum", author="alice")
        assert len(results) == 1
        assert results[0]["author_id"] == "alice"


class TestFilterByConfidence:
    """min_confidence threshold."""

    def test_min_confidence(self, db_with_schema):
        _seed_entries(db_with_schema)
        results = search_entries(db_with_schema, q="quantum",
                                 min_confidence=0.8)
        assert all(r["confidence"] >= 0.8 for r in results)
        assert len(results) == 2  # e1 (0.9), e4 (0.95)


class TestFilterBySince:
    """since date filter."""

    def test_since_filter(self, db_with_schema):
        _seed_entries(db_with_schema)
        # All entries are created "now" so a past date should return all
        results = search_entries(db_with_schema, q="quantum",
                                 since="2020-01-01T00:00:00Z")
        assert len(results) == 4

        # A future date should return none
        results = search_entries(db_with_schema, q="quantum",
                                 since="2099-01-01T00:00:00Z")
        assert len(results) == 0


class TestFilterByTags:
    """Tag-based filtering (non-FTS, exact match on JSON tags)."""

    def test_filter_by_tag(self, db_with_schema):
        _seed_entries(db_with_schema)
        results = search_entries(db_with_schema, q="quantum",
                                 tags=["computing"])
        assert len(results) == 1
        assert results[0]["author_id"] == "alice"


class TestPagination:
    """limit and offset."""

    def test_limit(self, db_with_schema):
        _seed_entries(db_with_schema)
        results = search_entries(db_with_schema, q="quantum", limit=2)
        assert len(results) == 2

    def test_offset(self, db_with_schema):
        _seed_entries(db_with_schema)
        all_results = search_entries(db_with_schema, q="quantum")
        offset_results = search_entries(db_with_schema, q="quantum",
                                        limit=2, offset=2)
        # offset results should be a subset of all results
        assert len(offset_results) <= 2
        offset_ids = {r["id"] for r in offset_results}
        first_page_ids = {r["id"] for r in search_entries(
            db_with_schema, q="quantum", limit=2)}
        assert not offset_ids.intersection(first_page_ids)


class TestSearchWithGraphTraversal:
    """Search results + hops traversal."""

    def test_search_with_hops(self, db_with_schema):
        e1, e2, e3, e4, e5 = _seed_entries(db_with_schema)
        # Create link: e1 --supports--> e4
        create_link(db_with_schema, source_entry_id=e1["id"],
                    target_entry_id=e4["id"], link_type="supports",
                    author_id="alice")
        # Search for "machine learning" (hits e3 only), then hops=1 should
        # NOT expand because e3 has no links
        results = search_entries(db_with_schema, q="machine", hops=1)
        assert len(results) == 1

        # Search for "computing" (hits e1), then hops=1 should also get e4
        results = search_entries(db_with_schema, q="computing", hops=1)
        result_ids = {r["id"] for r in results}
        assert e1["id"] in result_ids
        assert e4["id"] in result_ids
