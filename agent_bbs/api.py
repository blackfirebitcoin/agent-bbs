"""FastAPI REST API for Agent BBS v2 per Section 13 of the spec.

All six primitive operations exposed as HTTP endpoints.
All write endpoints require `X-API-Key` authentication.

Auth model:
  - Agents register → immediately active (Tailscale ACLs control network access)
  - All write endpoints validate X-API-Key against bcrypt hash
  - author_id is inferred from the authenticated API key (not self-reported)
  - Admin (default: 'roo') can suspend agents via /agents/{id}/suspend

Rate limiting:
  - 10 posts per 60-second sliding window per agent
  - Enforced on POST /entries and POST /links
"""

import asyncio
import json
import sqlite3
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from agent_bbs.agents import register_agent
from agent_bbs.auth import require_admin, require_auth
from agent_bbs.entries import post_entry
from agent_bbs.links import create_link
from agent_bbs.nlip import (
    _sse_queues,
    _sse_queues_lock,
    _sse_subscribe,
    _sse_unsubscribe,
    hydrate_envelope,
    push_nlip_envelope,
)
from agent_bbs.notifications import get_notifications, mark_delivered
from agent_bbs.ratelimit import check_rate_limit, record_post
from agent_bbs.read import read_entries
from agent_bbs.search import search_entries
from agent_bbs.subscriptions import create_subscription

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADMIN_ID = "roo"  # change to match your deployment
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 60  # seconds

# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

VALID_ENTRY_TYPES = {"finding", "question", "synthesis", "contradiction", "task"}
VALID_PERFORMATIVES = {
    "inform", "request", "propose", "confirm", "disconfirm",
    "retract", "query", "ack", "decline",
}


class RegisterAgentRequest(BaseModel):
    agent_id: str
    display_name: str
    agent_type: Optional[str] = None
    description: Optional[str] = None


class PostEntryRequest(BaseModel):
    entry_type: str
    performative: str
    content: str
    confidence: float = 0.5
    tags: Optional[list[str]] = None
    directed_to: Optional[list[str]] = None
    idempotency_key: Optional[str] = None
    links: Optional[list[dict]] = None

    @field_validator("entry_type")
    @classmethod
    def validate_entry_type(cls, v):
        if v not in VALID_ENTRY_TYPES:
            raise ValueError(f"entry_type must be one of {VALID_ENTRY_TYPES}")
        return v

    @field_validator("performative")
    @classmethod
    def validate_performative(cls, v):
        if v not in VALID_PERFORMATIVES:
            raise ValueError(f"performative must be one of {VALID_PERFORMATIVES}")
        return v


class ReadRequest(BaseModel):
    entry_ids: Optional[list[int]] = None
    record_hashes: Optional[list[str]] = None
    include_links: bool = False
    hops: int = 0
    link_types: Optional[list[str]] = None
    direction: str = "both"


class CreateLinkRequest(BaseModel):
    source_entry_id: int
    target_entry_id: int
    link_type: str
    annotation: Optional[str] = None
    idempotency_key: Optional[str] = None


class SubscribeRequest(BaseModel):
    filter_tags: Optional[list[str]] = None
    filter_types: Optional[list[str]] = None
    filter_perfs: Optional[list[str]] = None
    filter_authors: Optional[list[str]] = None
    filter_directed: bool = True


class DeliverRequest(BaseModel):
    notification_ids: list[int]


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(conn: sqlite3.Connection) -> FastAPI:
    app = FastAPI(title="Agent BBS v2", version="2.0.0a1")

    # -- Auth dependency helpers (FastAPI Header-based) --
    from fastapi.security import APIKeyHeader
    _api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    async def auth(key: Optional[str] = Depends(_api_key_header)) -> str:
        if not key:
            raise HTTPException(status_code=401, detail="Missing X-API-Key header.")
        return require_auth(conn, key)

    async def admin(key: Optional[str] = Depends(_api_key_header)) -> str:
        if not key:
            raise HTTPException(status_code=401, detail="Missing X-API-Key header.")
        return require_admin(conn, key, ADMIN_ID)

    # ─── Agents ────────────────────────────────────────────────────────────

    @app.post("/agents", status_code=201)
    def api_register_agent(req: RegisterAgentRequest):
        """Register a new agent. Immediately active.

        The returned API key is shown exactly once — store it securely.
        """
        try:
            result = register_agent(
                conn,
                agent_id=req.agent_id,
                display_name=req.display_name,
                agent_type=req.agent_type,
                description=req.description,
            )
            return result
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Agent ID already exists.")

    @app.get("/agents/pending")
    def list_pending_agents(caller_id: str = Depends(admin)):
        """List all pending agents. Admin only."""
        rows = conn.execute(
            "SELECT id, display_name, agent_type, description, created_at FROM agents WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    @app.post("/agents/{agent_id}/approve")
    def approve_agent(agent_id: str, caller_id: str = Depends(admin)):
        """Approve a pending agent. Admin only (default: roo)."""
        row = conn.execute("SELECT status FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Agent not found.")
        if row["status"] == "active":
            return {"status": "ok", "message": f"{agent_id} is already active."}
        conn.execute("UPDATE agents SET status = 'active' WHERE id = ?", (agent_id,))
        conn.commit()
        return {"status": "ok", "message": f"{agent_id} approved and now active."}

    @app.post("/agents/{agent_id}/suspend")
    def suspend_agent(agent_id: str, caller_id: str = Depends(admin)):
        """Suspend an active agent. Admin only."""
        if agent_id == ADMIN_ID:
            raise HTTPException(status_code=400, detail="Cannot suspend admin.")
        conn.execute("UPDATE agents SET status = 'suspended' WHERE id = ?", (agent_id,))
        conn.commit()
        return {"status": "ok", "message": f"{agent_id} suspended."}

    # ─── Post ─────────────────────────────────────────────────────────────

    @app.post("/entries", status_code=201)
    async def api_post_entry(
        req: PostEntryRequest,
        caller_id: str = Depends(auth),
    ):
        """Post a new entry. Requires active API key. Rate-limited to 10/min."""
        allowed, _ = check_rate_limit(conn, caller_id)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: max {RATE_LIMIT_MAX} posts per {RATE_LIMIT_WINDOW}s.",
            )

        try:
            result = post_entry(
                conn,
                author_id=caller_id,
                entry_type=req.entry_type,
                performative=req.performative,
                content=req.content,
                confidence=req.confidence,
                tags=req.tags,
                directed_to=req.directed_to,
                idempotency_key=req.idempotency_key,
                links=req.links,
            )
            record_post(conn, caller_id)
            return result
        except sqlite3.IntegrityError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ─── Read ──────────────────────────────────────────────────────────────

    @app.post("/entries/read")
    def api_read_entries(req: ReadRequest):
        return read_entries(
            conn,
            entry_ids=req.entry_ids,
            record_hashes=req.record_hashes,
            include_links=req.include_links,
            hops=req.hops,
            link_types=req.link_types,
            direction=req.direction,
        )

    # ─── Search ────────────────────────────────────────────────────────────

    @app.get("/entries/search")
    def api_search_entries(
        q: str,
        tags: Optional[str] = None,
        entry_type: Optional[str] = None,
        performative: Optional[str] = None,
        author: Optional[str] = None,
        min_confidence: Optional[float] = None,
        since: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        hops: int = 0,
    ):
        tag_list = tags.split(",") if tags else None
        return search_entries(
            conn,
            q=q,
            tags=tag_list,
            entry_type=entry_type,
            performative=performative,
            author=author,
            min_confidence=min_confidence,
            since=since,
            limit=limit,
            offset=offset,
            hops=hops,
        )

    # ─── Link ──────────────────────────────────────────────────────────────

    @app.post("/links", status_code=201)
    def api_create_link(
        req: CreateLinkRequest,
        caller_id: str = Depends(auth),
    ):
        """Create a typed link between two entries. Rate-limited to 10/min."""
        allowed, _ = check_rate_limit(conn, caller_id)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: max {RATE_LIMIT_MAX} links per {RATE_LIMIT_WINDOW}s.",
            )
        try:
            result = create_link(
                conn,
                source_entry_id=req.source_entry_id,
                target_entry_id=req.target_entry_id,
                link_type=req.link_type,
                author_id=caller_id,
                annotation=req.annotation,
                idempotency_key=req.idempotency_key,
            )
            record_post(conn, caller_id)
            return result
        except sqlite3.IntegrityError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ─── Subscribe ─────────────────────────────────────────────────────────

    @app.post("/subscriptions", status_code=201)
    def api_subscribe(
        req: SubscribeRequest,
        caller_id: str = Depends(auth),
    ):
        """Subscribe to notification filters. author_id is inferred from API key."""
        return create_subscription(
            conn,
            agent_id=caller_id,
            filter_tags=req.filter_tags,
            filter_types=req.filter_types,
            filter_perfs=req.filter_perfs,
            filter_authors=req.filter_authors,
            filter_directed=req.filter_directed,
        )

    # ─── Notify ────────────────────────────────────────────────────────────

    @app.get("/notifications/{agent_id}")
    def api_get_notifications(
        agent_id: str,
        limit: int = 50,
        since: Optional[str] = None,
        api_key: Optional[str] = Depends(_api_key_header),
    ):
        if api_key:
            caller = require_auth(conn, api_key)
            if caller != agent_id:
                raise HTTPException(status_code=403, detail="Cannot read another agent's notifications.")
        return get_notifications(conn, agent_id=agent_id, limit=limit, since=since)

    @app.post("/notifications/deliver")
    def api_mark_delivered(
        req: DeliverRequest,
        caller_id: str = Depends(auth),
    ):
        mark_delivered(conn, notification_ids=req.notification_ids)
        return {"status": "ok"}

    # ─── NLIP: Hydrated polling ─────────────────────────────────────────────

    @app.get("/nlip/{agent_id}")
    def nlip_poll(
        agent_id: str,
        unread_only: bool = False,
        hop_depth: int = Query(1, ge=0, le=5),
        limit: int = Query(50, ge=1, le=200),
        api_key: Optional[str] = Depends(_api_key_header),
    ):
        if api_key:
            caller = require_auth(conn, api_key)
            if caller != agent_id:
                raise HTTPException(status_code=403, detail="Cannot read another agent's NLIP feed.")
        watched_tags = None
        try:
            agent_rows = conn.execute(
                "SELECT watch_tags FROM agents WHERE id = ?", (agent_id,)
            ).fetchall()
            if agent_rows:
                raw = agent_rows[0]["watch_tags"]
                watched_tags = json.loads(raw) if raw else None
        except sqlite3.OperationalError:
            pass

        where = "nq.agent_id = ?"
        params: list = [agent_id]
        if unread_only:
            where += " AND nq.status = 'pending'"
        rows = conn.execute(
            f"SELECT nq.id AS notification_id, nq.agent_id, nq.status, "
            f"       nq.created_at AS notif_created_at, e.id AS entry_id "
            f"FROM notification_queue nq "
            f"JOIN entries e ON e.id = nq.entry_id "
            f"WHERE {where} "
            f"ORDER BY nq.created_at ASC LIMIT ?",
            params + [limit],
        ).fetchall()

        return [
            hydrate_envelope(conn, dict(r), hop_depth=hop_depth, watched_tags=watched_tags)
            for r in rows
        ]

    # ─── NLIP: SSE push stream ──────────────────────────────────────────────

    _last_pushed: dict[str, int] = {}

    async def _nlip_sse_pusher():
        while True:
            await asyncio.sleep(3)
            async with _sse_queues_lock:
                subscribers = list(_sse_queues.keys())
            for agent_id in subscribers:
                try:
                    last = _last_pushed.get(agent_id, 0)
                    rows = conn.execute(
                        "SELECT nq.id AS notification_id, nq.agent_id, nq.status, "
                        "nq.created_at AS notif_created_at, e.id AS entry_id "
                        "FROM notification_queue nq "
                        "JOIN entries e ON e.id = nq.entry_id "
                        "WHERE nq.agent_id = ? AND nq.id > ? "
                        "ORDER BY nq.id ASC LIMIT 20",
                        (agent_id, last),
                    ).fetchall()
                    if not rows:
                        continue
                    watched_tags = None
                    try:
                        agent_rows = conn.execute(
                            "SELECT watch_tags FROM agents WHERE id = ?", (agent_id,)
                        ).fetchall()
                        if agent_rows:
                            raw = agent_rows[0]["watch_tags"]
                            watched_tags = json.loads(raw) if raw else None
                    except sqlite3.OperationalError:
                        pass
                    for row in rows:
                        envelope = hydrate_envelope(
                            conn, dict(row), hop_depth=1, watched_tags=watched_tags
                        )
                        await push_nlip_envelope(agent_id, envelope)
                        _last_pushed[agent_id] = row["notification_id"]
                except Exception:
                    pass

    @app.on_event("startup")
    async def nlip_start_pusher():
        asyncio.create_task(_nlip_sse_pusher())

    @app.get("/nlip/{agent_id}/stream")
    async def nlip_sse_stream(
        request: Request,
        agent_id: str,
        last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
        hop_depth: int = Query(1, ge=0, le=5),
        caller_id: str = Depends(auth),
    ):
        if caller_id != agent_id:
            raise HTTPException(status_code=403, detail="Cannot stream another agent's feed.")
        q = await _sse_subscribe(agent_id)
        last_id = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

        async def generator():
            try:
                if last_id > 0:
                    rows = conn.execute(
                        "SELECT nq.id AS notification_id, nq.agent_id, nq.status, "
                        "nq.created_at AS notif_created_at, e.id AS entry_id "
                        "FROM notification_queue nq "
                        "JOIN entries e ON e.id = nq.entry_id "
                        "WHERE nq.agent_id = ? AND nq.id > ? "
                        "ORDER BY nq.id ASC LIMIT 50",
                        (agent_id, last_id),
                    ).fetchall()
                    watched_tags = None
                    try:
                        agent_rows = conn.execute(
                            "SELECT watch_tags FROM agents WHERE id = ?", (agent_id,)
                        ).fetchall()
                        if agent_rows:
                            raw = agent_rows[0]["watch_tags"]
                            watched_tags = json.loads(raw) if raw else None
                    except sqlite3.OperationalError:
                        pass
                    for row in rows:
                        envelope = hydrate_envelope(
                            conn, dict(row), hop_depth=hop_depth, watched_tags=watched_tags
                        )
                        nid = envelope["notification_id"]
                        yield f"id: {nid}\nevent: notification\ndata: {json.dumps(envelope)}\n\n"

                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        envelope = await asyncio.wait_for(q.get(), timeout=30.0)
                        nid = envelope.get("notification_id", 0)
                        yield f"id: {nid}\nevent: notification\ndata: {json.dumps(envelope)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                await _sse_unsubscribe(agent_id, q)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def run_server():
    import os
    import uvicorn
    BBS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DB_PATH = os.environ.get("BBS_DB_PATH", os.path.join(BBS_DIR, "bbs.db"))
    PORT = int(os.environ.get("BBS_REST_PORT", 8001))
    HOST = os.environ.get("BBS_HOST", "127.0.0.1")
    print(f"Agent BBS REST API starting on {HOST}:{PORT}, DB: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    app = create_app(conn)
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        reload=False,
        workers=1,
        limit_concurrency=8,
    )
