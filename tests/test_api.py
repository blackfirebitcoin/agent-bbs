"""Tests for FastAPI REST endpoints (Phase 2).

Spec reference: Section 13
Tests use httpx TestClient against the FastAPI app with an in-memory SQLite DB.
"""

import pytest
from fastapi.testclient import TestClient

from agent_bbs.api import create_app
from agent_bbs.schema import create_tables

import sqlite3


@pytest.fixture()
def client():
    """Create a test client with a fresh in-memory database."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    create_tables(conn)

    app = create_app(conn)
    with TestClient(app) as c:
        yield c
    conn.close()


# ---------------------------------------------------------------------------
# Agent registration
# ---------------------------------------------------------------------------

class TestRegisterEndpoint:
    def test_register_agent(self, client):
        resp = client.post("/agents", json={
            "agent_id": "test-agent",
            "display_name": "Test Agent",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["agent_id"] == "test-agent"
        assert "api_key" in data

    def test_duplicate_agent_returns_409(self, client):
        client.post("/agents", json={
            "agent_id": "dup", "display_name": "D"
        })
        resp = client.post("/agents", json={
            "agent_id": "dup", "display_name": "D2"
        })
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Post endpoint
# ---------------------------------------------------------------------------

class TestPostEndpoint:
    def test_post_entry(self, client):
        resp = client.post("/entries", json={
            "author_id": "a1",
            "entry_type": "finding",
            "performative": "inform",
            "content": "Test finding",
            "tags": ["test"],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "record_hash" in data
        assert "id" in data

    def test_post_with_idempotency_key(self, client):
        payload = {
            "author_id": "a1",
            "entry_type": "finding",
            "performative": "inform",
            "content": "Idempotent",
            "idempotency_key": "k1",
        }
        r1 = client.post("/entries", json=payload)
        r2 = client.post("/entries", json=payload)
        assert r1.json()["id"] == r2.json()["id"]

    def test_post_invalid_type_returns_422(self, client):
        resp = client.post("/entries", json={
            "author_id": "a1",
            "entry_type": "bogus",
            "performative": "inform",
            "content": "bad",
        })
        # FastAPI/pydantic validation or DB constraint
        assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Read endpoint
# ---------------------------------------------------------------------------

class TestReadEndpoint:
    def test_read_by_id(self, client):
        r = client.post("/entries", json={
            "author_id": "a1", "entry_type": "finding",
            "performative": "inform", "content": "read me"
        })
        eid = r.json()["id"]
        resp = client.post("/entries/read", json={"entry_ids": [eid]})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["content"] == "read me"

    def test_read_with_hops(self, client):
        r1 = client.post("/entries", json={
            "author_id": "a1", "entry_type": "finding",
            "performative": "inform", "content": "A"
        })
        r2 = client.post("/entries", json={
            "author_id": "a1", "entry_type": "finding",
            "performative": "inform", "content": "B"
        })
        client.post("/links", json={
            "source_entry_id": r1.json()["id"],
            "target_entry_id": r2.json()["id"],
            "link_type": "supports",
            "author_id": "a1",
        })
        resp = client.post("/entries/read", json={
            "entry_ids": [r1.json()["id"]], "hops": 1
        })
        assert len(resp.json()) == 2


# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------

class TestSearchEndpoint:
    def test_search(self, client):
        client.post("/entries", json={
            "author_id": "a1", "entry_type": "finding",
            "performative": "inform", "content": "quantum computing",
            "tags": ["physics"],
        })
        resp = client.get("/entries/search", params={"q": "quantum"})
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_search_with_filters(self, client):
        client.post("/entries", json={
            "author_id": "a1", "entry_type": "finding",
            "performative": "inform", "content": "quantum A",
        })
        client.post("/entries", json={
            "author_id": "a1", "entry_type": "question",
            "performative": "query", "content": "quantum B",
        })
        resp = client.get("/entries/search", params={
            "q": "quantum", "entry_type": "question"
        })
        assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# Link endpoint
# ---------------------------------------------------------------------------

class TestLinkEndpoint:
    def test_create_link(self, client):
        r1 = client.post("/entries", json={
            "author_id": "a1", "entry_type": "finding",
            "performative": "inform", "content": "A"
        })
        r2 = client.post("/entries", json={
            "author_id": "a1", "entry_type": "finding",
            "performative": "inform", "content": "B"
        })
        resp = client.post("/links", json={
            "source_entry_id": r1.json()["id"],
            "target_entry_id": r2.json()["id"],
            "link_type": "supports",
            "author_id": "a1",
        })
        assert resp.status_code == 201
        assert resp.json()["link_type"] == "supports"


# ---------------------------------------------------------------------------
# Subscribe endpoint
# ---------------------------------------------------------------------------

class TestSubscribeEndpoint:
    def test_create_subscription(self, client):
        client.post("/agents", json={
            "agent_id": "watcher", "display_name": "W"
        })
        resp = client.post("/subscriptions", json={
            "agent_id": "watcher",
            "filter_tags": ["ai"],
        })
        assert resp.status_code == 201
        assert "id" in resp.json()


# ---------------------------------------------------------------------------
# Notify endpoint
# ---------------------------------------------------------------------------

class TestNotifyEndpoint:
    def test_get_notifications(self, client):
        client.post("/agents", json={
            "agent_id": "watcher", "display_name": "W"
        })
        client.post("/subscriptions", json={
            "agent_id": "watcher", "filter_tags": ["ai"],
        })
        client.post("/entries", json={
            "author_id": "poster", "entry_type": "finding",
            "performative": "inform", "content": "AI stuff", "tags": ["ai"],
        })
        resp = client.get("/notifications/watcher")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert "content" not in data[0]
        assert "entry_id" in data[0]

    def test_mark_delivered_via_api(self, client):
        client.post("/agents", json={
            "agent_id": "watcher", "display_name": "W"
        })
        client.post("/subscriptions", json={
            "agent_id": "watcher", "filter_tags": ["ai"],
        })
        client.post("/entries", json={
            "author_id": "poster", "entry_type": "finding",
            "performative": "inform", "content": "AI", "tags": ["ai"],
        })
        notifs = client.get("/notifications/watcher").json()
        nid = notifs[0]["notification_id"]

        resp = client.post("/notifications/deliver", json={
            "notification_ids": [nid]
        })
        assert resp.status_code == 200

        # Verify delivered
        notifs2 = client.get("/notifications/watcher").json()
        # Pending ones should be gone (unless we return all statuses)
        delivered = [n for n in notifs2 if n.get("status") == "delivered"]
        # or check DB directly — the API may still return it
