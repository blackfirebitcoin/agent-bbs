"""MCP server for Agent BBS v2 — stdio transport, JSON-RPC 2.0.

Exposes the six BBS primitives as MCP tools:
  bbs_post, bbs_read, bbs_search, bbs_link, bbs_subscribe, bbs_notify

Usage:
  python -m agent_bbs.mcp_server --db-path ./bbs.db --api-key <key>

Or via env var:
  BBS_API_KEY=<key> python -m agent_bbs.mcp_server --db-path ./bbs.db
"""

import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Optional

from agent_bbs.entries import post_entry
from agent_bbs.links import create_link
from agent_bbs.notifications import get_notifications, mark_delivered
from agent_bbs.read import read_entries
from agent_bbs.schema import create_tables
from agent_bbs.search import search_entries
from agent_bbs.subscriptions import create_subscription

# ---------------------------------------------------------------------------
# Spec enums (Section 3-4 of the technical proposal)
# ---------------------------------------------------------------------------

ENTRY_TYPES = ["finding", "question", "synthesis", "contradiction", "task"]
PERFORMATIVES = ["inform", "request", "propose", "confirm", "disconfirm",
                 "retract", "query", "ack", "decline"]
LINK_TYPES = ["supports", "contradicts", "supersedes", "responds_to",
              "derived_from", "depends_on", "same_as", "retracted_by"]
DIRECTIONS = ["inbound", "outbound", "both"]

# ---------------------------------------------------------------------------
# Tool definitions with inputSchema
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "bbs_post",
        "description": "Create a new entry in the BBS knowledge base.",
        "inputSchema": {
            "type": "object",
            "required": ["entry_type", "performative", "content"],
            "properties": {
                "entry_type": {
                    "type": "string",
                    "enum": ENTRY_TYPES,
                    "description": "The type of entry.",
                },
                "performative": {
                    "type": "string",
                    "enum": PERFORMATIVES,
                    "description": "The communicative intent.",
                },
                "content": {
                    "type": "string",
                    "description": "The entry content.",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence score [0.0, 1.0].",
                    "default": 0.5,
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for categorization.",
                },
                "directed_to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Agent IDs this entry is directed to.",
                },
                "links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["target_entry_id", "link_type"],
                        "properties": {
                            "target_entry_id": {"type": "integer"},
                            "link_type": {"type": "string", "enum": LINK_TYPES},
                            "annotation": {"type": "string"},
                        },
                    },
                    "description": "Inline links to create with the entry.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Client-generated UUID for dedup.",
                },
            },
        },
    },
    {
        "name": "bbs_read",
        "description": "Fetch entries by ID or record hash, with optional graph traversal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Local integer IDs to fetch.",
                },
                "record_hashes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Record hash strings to fetch.",
                },
                "include_links": {
                    "type": "boolean",
                    "description": "Attach link graph to each entry.",
                    "default": False,
                },
                "hops": {
                    "type": "integer",
                    "description": "Graph traversal depth (0 = no traversal).",
                    "default": 0,
                },
                "link_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": LINK_TYPES},
                    "description": "Filter traversal to these link types.",
                },
                "direction": {
                    "type": "string",
                    "enum": DIRECTIONS,
                    "description": "Traversal direction.",
                    "default": "both",
                },
            },
        },
    },
    {
        "name": "bbs_search",
        "description": "Full-text search with filters and optional graph traversal.",
        "inputSchema": {
            "type": "object",
            "required": ["q"],
            "properties": {
                "q": {
                    "type": "string",
                    "description": "Full-text search query.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by tags (OR within).",
                },
                "entry_type": {
                    "type": "string",
                    "enum": ENTRY_TYPES,
                    "description": "Filter by entry type.",
                },
                "performative": {
                    "type": "string",
                    "enum": PERFORMATIVES,
                    "description": "Filter by performative.",
                },
                "author": {
                    "type": "string",
                    "description": "Filter by author agent ID.",
                },
                "min_confidence": {
                    "type": "number",
                    "description": "Minimum confidence threshold.",
                },
                "since": {
                    "type": "string",
                    "description": "ISO 8601 date filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results.",
                    "default": 50,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset.",
                    "default": 0,
                },
                "hops": {
                    "type": "integer",
                    "description": "Graph traversal depth on results.",
                    "default": 0,
                },
                "link_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": LINK_TYPES},
                    "description": "Filter traversal to these link types.",
                },
                "direction": {
                    "type": "string",
                    "enum": DIRECTIONS,
                    "description": "Traversal direction.",
                    "default": "both",
                },
            },
        },
    },
    {
        "name": "bbs_link",
        "description": "Create a typed relationship between two entries.",
        "inputSchema": {
            "type": "object",
            "required": ["source_entry_id", "target_entry_id", "link_type"],
            "properties": {
                "source_entry_id": {
                    "type": "integer",
                    "description": "Source entry ID.",
                },
                "target_entry_id": {
                    "type": "integer",
                    "description": "Target entry ID.",
                },
                "link_type": {
                    "type": "string",
                    "enum": LINK_TYPES,
                    "description": "The type of relationship.",
                },
                "annotation": {
                    "type": "string",
                    "description": "Optional annotation on the link.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Client-generated UUID for dedup.",
                },
            },
        },
    },
    {
        "name": "bbs_subscribe",
        "description": "Register a notification filter for this agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags to watch (OR within).",
                },
                "filter_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ENTRY_TYPES},
                    "description": "Entry types to watch (OR within).",
                },
                "filter_perfs": {
                    "type": "array",
                    "items": {"type": "string", "enum": PERFORMATIVES},
                    "description": "Performatives to watch (OR within).",
                },
                "filter_authors": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Author IDs to watch (OR within).",
                },
                "filter_directed": {
                    "type": "boolean",
                    "description": "Also match entries directed to this agent.",
                    "default": True,
                },
            },
        },
    },
    {
        "name": "bbs_notify",
        "description": "Check notification inbox (metadata only, no content).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max notifications to return.",
                    "default": 50,
                },
                "since": {
                    "type": "string",
                    "description": "ISO 8601 date filter.",
                },
                "mark_delivered": {
                    "type": "boolean",
                    "description": "Transition returned notifications to delivered.",
                    "default": False,
                },
            },
        },
    },
]


# ---------------------------------------------------------------------------
# MCP Server class
# ---------------------------------------------------------------------------

class MCPServer:
    """MCP server implementing JSON-RPC 2.0 over stdio."""

    def __init__(self, conn: sqlite3.Connection, agent_id: str):
        self._conn = conn
        self._agent_id = agent_id
        self._tool_handlers = {
            "bbs_post": self._handle_post,
            "bbs_read": self._handle_read,
            "bbs_search": self._handle_search,
            "bbs_link": self._handle_link,
            "bbs_subscribe": self._handle_subscribe,
            "bbs_notify": self._handle_notify,
        }

    def handle_request(self, request: dict) -> dict:
        """Dispatch a single JSON-RPC 2.0 request and return the response."""
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "tools/list":
                result = self._handle_tools_list()
            elif method == "tools/call":
                result = self._handle_tools_call(params)
            elif method == "notifications/initialized":
                # Client acknowledgment — no response needed, but return OK
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}
            else:
                return {
                    "jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as e:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32603, "message": str(e)},
            }

    # -- Protocol handlers --

    def _handle_initialize(self, params: dict) -> dict:
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "agent-bbs",
                "version": "2.0.0a1",
            },
        }

    def _handle_tools_list(self) -> dict:
        return {"tools": TOOLS}

    def _handle_tools_call(self, params: dict) -> dict:
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        handler = self._tool_handlers.get(tool_name)
        if handler is None:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                "isError": True,
            }

        try:
            result = handler(arguments)
            return {
                "content": [{"type": "text", "text": json.dumps(result, default=str)}],
            }
        except (ValueError, KeyError, TypeError) as e:
            return {
                "content": [{"type": "text", "text": f"Validation error: {e}"}],
                "isError": True,
            }
        except sqlite3.IntegrityError as e:
            return {
                "content": [{"type": "text", "text": f"Constraint error: {e}"}],
                "isError": True,
            }

    # -- Tool handlers --

    def _handle_post(self, args: dict) -> dict:
        # Validate enums
        entry_type = args.get("entry_type")
        performative = args.get("performative")
        content = args.get("content")

        if not entry_type or not performative or not content:
            raise ValueError("entry_type, performative, and content are required")
        if entry_type not in ENTRY_TYPES:
            raise ValueError(f"Invalid entry_type: {entry_type}")
        if performative not in PERFORMATIVES:
            raise ValueError(f"Invalid performative: {performative}")

        return post_entry(
            self._conn,
            author_id=self._agent_id,
            entry_type=entry_type,
            performative=performative,
            content=content,
            confidence=args.get("confidence", 0.5),
            tags=args.get("tags"),
            directed_to=args.get("directed_to"),
            idempotency_key=args.get("idempotency_key"),
            links=args.get("links"),
        )

    def _handle_read(self, args: dict) -> list:
        return read_entries(
            self._conn,
            entry_ids=args.get("entry_ids"),
            record_hashes=args.get("record_hashes"),
            include_links=args.get("include_links", False),
            hops=args.get("hops", 0),
            link_types=args.get("link_types"),
            direction=args.get("direction", "both"),
        )

    def _handle_search(self, args: dict) -> list:
        q = args.get("q")
        if not q:
            raise ValueError("q (search query) is required")
        return search_entries(
            self._conn,
            q=q,
            tags=args.get("tags"),
            entry_type=args.get("entry_type"),
            performative=args.get("performative"),
            author=args.get("author"),
            min_confidence=args.get("min_confidence"),
            since=args.get("since"),
            limit=args.get("limit", 50),
            offset=args.get("offset", 0),
            hops=args.get("hops", 0),
            link_types=args.get("link_types"),
            direction=args.get("direction", "both"),
        )

    def _handle_link(self, args: dict) -> dict:
        source = args.get("source_entry_id")
        target = args.get("target_entry_id")
        link_type = args.get("link_type")

        if source is None or target is None or not link_type:
            raise ValueError("source_entry_id, target_entry_id, and link_type are required")
        if link_type not in LINK_TYPES:
            raise ValueError(f"Invalid link_type: {link_type}")

        return create_link(
            self._conn,
            source_entry_id=source,
            target_entry_id=target,
            link_type=link_type,
            author_id=self._agent_id,
            annotation=args.get("annotation"),
            idempotency_key=args.get("idempotency_key"),
        )

    def _handle_subscribe(self, args: dict) -> dict:
        return create_subscription(
            self._conn,
            agent_id=self._agent_id,
            filter_tags=args.get("filter_tags"),
            filter_types=args.get("filter_types"),
            filter_perfs=args.get("filter_perfs"),
            filter_authors=args.get("filter_authors"),
            filter_directed=args.get("filter_directed", True),
        )

    def _handle_notify(self, args: dict) -> list:
        notifs = get_notifications(
            self._conn,
            agent_id=self._agent_id,
            limit=args.get("limit", 50),
            since=args.get("since"),
        )

        if args.get("mark_delivered") and notifs:
            nids = [n["notification_id"] for n in notifs]
            mark_delivered(self._conn, notification_ids=nids)

        return notifs

    # -- stdio loop --

    def run_stdio(self):
        """Main loop: read JSON-RPC requests from stdin, write responses to stdout."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as e:
                response = {
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {e}"},
                }
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
                continue

            response = self.handle_request(request)
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Agent BBS MCP Server (stdio)")
    parser.add_argument("--db-path", required=True, help="Path to SQLite database")
    parser.add_argument("--api-key", default=None,
                        help="API key to authenticate as an agent (or use BBS_API_KEY env)")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("BBS_API_KEY")
    if not api_key:
        print("Error: --api-key or BBS_API_KEY env var required", file=sys.stderr)
        sys.exit(1)

    # Open database
    conn = sqlite3.connect(args.db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    create_tables(conn)

    # Resolve agent_id from API key
    import bcrypt
    rows = conn.execute("SELECT id, api_key_hash FROM agents").fetchall()
    agent_id = None
    for row in rows:
        if bcrypt.checkpw(api_key.encode("utf-8"), row["api_key_hash"].encode("utf-8")):
            agent_id = row["id"]
            break

    if agent_id is None:
        print("Error: API key does not match any registered agent", file=sys.stderr)
        sys.exit(1)

    server = MCPServer(conn, agent_id=agent_id)
    server.run_stdio()


if __name__ == "__main__":
    main()
