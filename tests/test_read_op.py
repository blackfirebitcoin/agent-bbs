"""Tests for the Read operation (Phase 2).

Spec reference: Section 7.2
- Fetch by local ID or record_hash (single and batch)
- include_links returns the link graph
- Graph traversal: hops, link_types, direction (inbound/outbound/both)
"""

import pytest

from agent_bbs.entries import post_entry
from agent_bbs.links import create_link
from agent_bbs.read import read_entries


# ---------------------------------------------------------------------------
# Helper to build a small graph:
#   A --supports--> B --supports--> C
#   D --contradicts--> B
# ---------------------------------------------------------------------------

def _build_graph(db):
    a = post_entry(db, author_id="x", entry_type="finding",
                   performative="inform", content="A")
    b = post_entry(db, author_id="x", entry_type="finding",
                   performative="inform", content="B")
    c = post_entry(db, author_id="x", entry_type="finding",
                   performative="inform", content="C")
    d = post_entry(db, author_id="y", entry_type="contradiction",
                   performative="disconfirm", content="D")
    create_link(db, source_entry_id=a["id"], target_entry_id=b["id"],
                link_type="supports", author_id="x")
    create_link(db, source_entry_id=b["id"], target_entry_id=c["id"],
                link_type="supports", author_id="x")
    create_link(db, source_entry_id=d["id"], target_entry_id=b["id"],
                link_type="contradicts", author_id="y")
    return a, b, c, d


class TestReadByID:
    """Fetch entries by local integer ID."""

    def test_single_id(self, db_with_schema):
        e = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                       performative="inform", content="hello")
        results = read_entries(db_with_schema, entry_ids=[e["id"]])
        assert len(results) == 1
        assert results[0]["id"] == e["id"]
        assert results[0]["content"] == "hello"

    def test_batch_ids(self, db_with_schema):
        e1 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="one")
        e2 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="two")
        results = read_entries(db_with_schema, entry_ids=[e1["id"], e2["id"]])
        assert len(results) == 2

    def test_nonexistent_id_returns_empty(self, db_with_schema):
        results = read_entries(db_with_schema, entry_ids=[9999])
        assert len(results) == 0


class TestReadByHash:
    """Fetch entries by record_hash."""

    def test_single_hash(self, db_with_schema):
        e = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                       performative="inform", content="hello")
        results = read_entries(db_with_schema, record_hashes=[e["record_hash"]])
        assert len(results) == 1
        assert results[0]["record_hash"] == e["record_hash"]

    def test_batch_hashes(self, db_with_schema):
        e1 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="one")
        e2 = post_entry(db_with_schema, author_id="a1", entry_type="finding",
                        performative="inform", content="two")
        results = read_entries(db_with_schema,
                               record_hashes=[e1["record_hash"], e2["record_hash"]])
        assert len(results) == 2


class TestIncludeLinks:
    """include_links returns the link graph for each entry."""

    def test_include_links_returns_links(self, db_with_schema):
        a, b, c, d = _build_graph(db_with_schema)
        results = read_entries(db_with_schema, entry_ids=[b["id"]],
                               include_links=True)
        assert len(results) == 1
        assert "links" in results[0]
        # B has: inbound supports from A, outbound supports to C,
        #        inbound contradicts from D
        assert len(results[0]["links"]) == 3

    def test_no_links_by_default(self, db_with_schema):
        a, b, c, d = _build_graph(db_with_schema)
        results = read_entries(db_with_schema, entry_ids=[b["id"]])
        # Should not include links key or it should be absent
        assert "links" not in results[0]


class TestGraphTraversalHops:
    """hops parameter controls depth of graph traversal."""

    def test_hops_0_returns_only_requested(self, db_with_schema):
        a, b, c, d = _build_graph(db_with_schema)
        results = read_entries(db_with_schema, entry_ids=[b["id"]], hops=0)
        assert len(results) == 1
        assert results[0]["id"] == b["id"]

    def test_hops_1_returns_direct_neighbors(self, db_with_schema):
        a, b, c, d = _build_graph(db_with_schema)
        results = read_entries(db_with_schema, entry_ids=[b["id"]], hops=1)
        result_ids = {r["id"] for r in results}
        # B's direct neighbors: A (inbound supports), C (outbound supports),
        #                        D (inbound contradicts)
        assert b["id"] in result_ids
        assert a["id"] in result_ids
        assert c["id"] in result_ids
        assert d["id"] in result_ids

    def test_hops_2_returns_two_hop_neighborhood(self, db_with_schema):
        a, b, c, d = _build_graph(db_with_schema)
        # Start from A: hop1 reaches B, hop2 reaches C and D
        results = read_entries(db_with_schema, entry_ids=[a["id"]], hops=2)
        result_ids = {r["id"] for r in results}
        assert a["id"] in result_ids
        assert b["id"] in result_ids
        assert c["id"] in result_ids
        assert d["id"] in result_ids


class TestGraphTraversalDirection:
    """direction parameter: outbound, inbound, both."""

    def test_outbound_only(self, db_with_schema):
        a, b, c, d = _build_graph(db_with_schema)
        # From B outbound: only C (B --supports--> C)
        results = read_entries(db_with_schema, entry_ids=[b["id"]],
                               hops=1, direction="outbound")
        result_ids = {r["id"] for r in results}
        assert b["id"] in result_ids
        assert c["id"] in result_ids
        # A and D are inbound to B, should NOT be included
        assert a["id"] not in result_ids
        assert d["id"] not in result_ids

    def test_inbound_only(self, db_with_schema):
        a, b, c, d = _build_graph(db_with_schema)
        # From B inbound: A (A --supports--> B) and D (D --contradicts--> B)
        results = read_entries(db_with_schema, entry_ids=[b["id"]],
                               hops=1, direction="inbound")
        result_ids = {r["id"] for r in results}
        assert b["id"] in result_ids
        assert a["id"] in result_ids
        assert d["id"] in result_ids
        assert c["id"] not in result_ids

    def test_both_direction(self, db_with_schema):
        a, b, c, d = _build_graph(db_with_schema)
        results = read_entries(db_with_schema, entry_ids=[b["id"]],
                               hops=1, direction="both")
        result_ids = {r["id"] for r in results}
        assert {a["id"], b["id"], c["id"], d["id"]} == result_ids


class TestGraphTraversalLinkTypes:
    """link_types filters which links to traverse."""

    def test_filter_supports_only(self, db_with_schema):
        a, b, c, d = _build_graph(db_with_schema)
        # From B with only supports links: A and C but NOT D
        results = read_entries(db_with_schema, entry_ids=[b["id"]],
                               hops=1, link_types=["supports"])
        result_ids = {r["id"] for r in results}
        assert a["id"] in result_ids
        assert c["id"] in result_ids
        assert d["id"] not in result_ids

    def test_filter_contradicts_only(self, db_with_schema):
        a, b, c, d = _build_graph(db_with_schema)
        # From B with only contradicts links: only D
        results = read_entries(db_with_schema, entry_ids=[b["id"]],
                               hops=1, link_types=["contradicts"])
        result_ids = {r["id"] for r in results}
        assert d["id"] in result_ids
        assert a["id"] not in result_ids
        assert c["id"] not in result_ids
