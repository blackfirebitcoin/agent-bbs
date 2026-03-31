"""MCP server for Agent Working Memory — stdio transport, JSON-RPC 2.0.

Exposes agent-local working memory as MCP tools:
  wm_tick, wm_bootstrap, wm_get_summaries, wm_upsert_summary,
  wm_record_action, wm_get_recent_actions, wm_status

Usage:
  python -m agent_runtime.mcp_server \
    --wm-db-path ./working-memory.db \
    --bbs-db-path ./bbs.db \
    --config ./agent-config.yaml
"""

import argparse
import json
import sqlite3
import sys
from typing import Optional

from agent_bbs.schema import create_tables as create_bbs_tables
from agent_runtime.bootstrap import bootstrap_working_memory
from agent_runtime.config import AgentConfig, generate_default_config
from agent_runtime.notification_processor import tick, process_tick_result
from agent_runtime.working_memory import (
    compute_relevance,
    create_working_memory_tables,
    record_action,
    touch_summary,
    upsert_thread_summary,
)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

ACTION_TYPES = ["post", "link", "subscribe", "read", "search", "notify"]

# ---------------------------------------------------------------------------
# Tool definitions with inputSchema
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "wm_tick",
        "description": "Run one notification processing cycle. Returns a context assembly request or empty status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": "ISO 8601 datetime to poll notifications from. Auto-tracked if omitted.",
                },
                "batch_size": {
                    "type": "integer",
                    "description": "Max notifications per batch.",
                    "default": 10,
                },
                "min_score": {
                    "type": "number",
                    "description": "Minimum priority score threshold.",
                    "default": 0.0,
                },
            },
        },
    },
    {
        "name": "wm_bootstrap",
        "description": "Cold-start: search BBS for watched tags, cluster, store seed summaries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entries_per_tag": {
                    "type": "integer",
                    "description": "Max entries to fetch per watched tag.",
                    "default": 50,
                },
            },
        },
    },
    {
        "name": "wm_get_summaries",
        "description": "Retrieve thread summaries, ranked by relevance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "FTS search string. Omit to return all summaries.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max summaries to return.",
                    "default": 10,
                },
                "min_relevance": {
                    "type": "number",
                    "description": "Minimum relevance score threshold.",
                    "default": 0.0,
                },
            },
        },
    },
    {
        "name": "wm_upsert_summary",
        "description": "Create or update a thread summary.",
        "inputSchema": {
            "type": "object",
            "required": ["cluster_tag", "summary_text", "entry_ids"],
            "properties": {
                "cluster_tag": {
                    "type": "string",
                    "description": "Primary tag or topic cluster.",
                },
                "summary_text": {
                    "type": "string",
                    "description": "The summary content.",
                },
                "entry_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "BBS entry IDs included in this summary.",
                },
            },
        },
    },
    {
        "name": "wm_record_action",
        "description": "Log an outbound action for audit trail.",
        "inputSchema": {
            "type": "object",
            "required": ["action_type"],
            "properties": {
                "action_type": {
                    "type": "string",
                    "enum": ACTION_TYPES,
                    "description": "Type of action taken.",
                },
                "bbs_entry_id": {
                    "type": "integer",
                    "description": "Resulting BBS entry ID if applicable.",
                },
                "record_hash": {
                    "type": "string",
                    "description": "Record hash if applicable.",
                },
                "payload": {
                    "type": "object",
                    "description": "Additional action metadata.",
                },
            },
        },
    },
    {
        "name": "wm_get_recent_actions",
        "description": "Review recent outbound actions (what have I done lately?).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max actions to return.",
                    "default": 20,
                },
                "action_type": {
                    "type": "string",
                    "enum": ACTION_TYPES,
                    "description": "Filter by action type.",
                },
            },
        },
    },
    {
        "name": "wm_status",
        "description": "Dashboard: pending notification count, summary count, last tick time, last bootstrap time.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ---------------------------------------------------------------------------
# Metadata table for tracking state (e.g., last_seen_entry_id)
# ---------------------------------------------------------------------------

_METADATA_SQL = """
CREATE TABLE IF NOT EXISTS wm_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _ensure_metadata_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_METADATA_SQL)
    conn.commit()


def _get_metadata(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM wm_metadata WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def _set_metadata(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wm_metadata (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# MCP Server class
# ---------------------------------------------------------------------------

class WMServer:
    """Working Memory MCP server implementing JSON-RPC 2.0 over stdio."""

    def __init__(
        self,
        bbs_conn: sqlite3.Connection,
        wm_conn: sqlite3.Connection,
        config: AgentConfig,
    ):
        self._bbs_conn = bbs_conn
        self._wm_conn = wm_conn
        self._config = config
        self._tool_handlers = {
            "wm_tick": self._handle_tick,
            "wm_bootstrap": self._handle_bootstrap,
            "wm_get_summaries": self._handle_get_summaries,
            "wm_upsert_summary": self._handle_upsert_summary,
            "wm_record_action": self._handle_record_action,
            "wm_get_recent_actions": self._handle_get_recent_actions,
            "wm_status": self._handle_status,
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
                "name": "agent-wm",
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

    def _handle_tick(self, args: dict) -> dict:
        agent_id = self._config.agent_id
        watch_tags = self._config.watch_tags
        scorer_weights = self._config.scorer.model_dump()
        profile = self._config.get_active_profile()

        batch_size = args.get("batch_size", 10)
        min_score = args.get("min_score", 0.0)

        # Determine 'since': explicit arg > tracked last_seen > None
        since = args.get("since")
        if since is None:
            last_seen = _get_metadata(self._wm_conn, "last_seen_entry_id")
            if last_seen is not None:
                # Use the last seen entry ID to filter — convert to a since query
                # by looking up its created_at timestamp
                row = self._bbs_conn.execute(
                    "SELECT created_at FROM entries WHERE id = ?", (int(last_seen),)
                ).fetchone()
                if row:
                    since = row["created_at"]

        result = tick(
            self._bbs_conn,
            self._wm_conn,
            agent_id=agent_id,
            watch_tags=watch_tags,
            scorer_weights=scorer_weights,
            batch_size=batch_size,
            min_score=min_score,
            token_budget=profile.stage2_notifications,
            since=since,
        )

        if result is None:
            return {"status": "empty"}

        # Update last_seen_entry_id from the max entry_id in the batch
        entry_ids = result.get("fetch_entry_ids", [])
        if entry_ids:
            max_id = max(entry_ids)
            current = _get_metadata(self._wm_conn, "last_seen_entry_id")
            if current is None or max_id > int(current):
                _set_metadata(self._wm_conn, "last_seen_entry_id", str(max_id))

        return result

    def _handle_bootstrap(self, args: dict) -> dict:
        entries_per_tag = args.get("entries_per_tag", 50)
        result = bootstrap_working_memory(
            self._bbs_conn,
            self._wm_conn,
            agent_id=self._config.agent_id,
            watch_tags=self._config.watch_tags,
            entries_per_tag=entries_per_tag,
        )

        # Record bootstrap time
        from datetime import datetime, timezone
        _set_metadata(
            self._wm_conn, "last_bootstrap_time",
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        return result

    def _handle_get_summaries(self, args: dict) -> list:
        query = args.get("query")
        limit = args.get("limit", 10)
        min_relevance = args.get("min_relevance", 0.0)

        if query:
            # FTS search
            rows = self._wm_conn.execute(
                "SELECT ts.* FROM thread_summaries ts "
                "JOIN summaries_fts fts ON ts.id = fts.rowid "
                "WHERE summaries_fts MATCH ? LIMIT ?",
                (query, limit * 2),  # fetch extra to allow relevance filtering
            ).fetchall()
        else:
            rows = self._wm_conn.execute(
                "SELECT * FROM thread_summaries ORDER BY updated_at DESC LIMIT ?",
                (limit * 2,),
            ).fetchall()

        summaries = []
        for row in rows:
            s = dict(row)
            s["entry_ids"] = json.loads(s["entry_ids"]) if isinstance(s["entry_ids"], str) else s["entry_ids"]
            s["relevance"] = compute_relevance(s)
            if s["relevance"] >= min_relevance:
                summaries.append(s)

        # Sort by relevance descending, limit
        summaries.sort(key=lambda x: x["relevance"], reverse=True)
        summaries = summaries[:limit]

        # Touch returned summaries to track access
        for s in summaries:
            touch_summary(self._wm_conn, summary_id=s["id"])

        return summaries

    def _handle_upsert_summary(self, args: dict) -> dict:
        cluster_tag = args.get("cluster_tag")
        summary_text = args.get("summary_text")
        entry_ids = args.get("entry_ids")

        if not cluster_tag or not summary_text or entry_ids is None:
            raise ValueError("cluster_tag, summary_text, and entry_ids are required")

        summary_id = upsert_thread_summary(
            self._wm_conn,
            cluster_tag=cluster_tag,
            summary_text=summary_text,
            entry_ids=entry_ids,
        )
        return {
            "summary_id": summary_id,
            "cluster_tag": cluster_tag,
            "entry_count": len(entry_ids),
        }

    def _handle_record_action(self, args: dict) -> dict:
        action_type = args.get("action_type")
        if not action_type:
            raise ValueError("action_type is required")
        if action_type not in ACTION_TYPES:
            raise ValueError(f"Invalid action_type: {action_type}. Must be one of {ACTION_TYPES}")

        action_id = record_action(
            self._wm_conn,
            action_type=action_type,
            bbs_entry_id=args.get("bbs_entry_id"),
            record_hash=args.get("record_hash"),
            payload=args.get("payload"),
        )
        return {"action_id": action_id}

    def _handle_get_recent_actions(self, args: dict) -> list:
        limit = args.get("limit", 20)
        action_type = args.get("action_type")

        if action_type and action_type not in ACTION_TYPES:
            raise ValueError(f"Invalid action_type: {action_type}. Must be one of {ACTION_TYPES}")

        if action_type:
            rows = self._wm_conn.execute(
                "SELECT * FROM agent_actions WHERE action_type = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (action_type, limit),
            ).fetchall()
        else:
            rows = self._wm_conn.execute(
                "SELECT * FROM agent_actions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        results = []
        for row in rows:
            d = dict(row)
            if isinstance(d.get("payload"), str):
                try:
                    d["payload"] = json.loads(d["payload"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results

    def _handle_status(self, args: dict) -> dict:
        pending_count = self._wm_conn.execute(
            "SELECT COUNT(*) as c FROM pending_notifications WHERE status IN ('pending', 'scored')"
        ).fetchone()["c"]

        summary_count = self._wm_conn.execute(
            "SELECT COUNT(*) as c FROM thread_summaries"
        ).fetchone()["c"]

        action_count = self._wm_conn.execute(
            "SELECT COUNT(*) as c FROM agent_actions"
        ).fetchone()["c"]

        event_count = self._wm_conn.execute(
            "SELECT COUNT(*) as c FROM events"
        ).fetchone()["c"]

        last_tick = _get_metadata(self._wm_conn, "last_seen_entry_id")
        last_bootstrap = _get_metadata(self._wm_conn, "last_bootstrap_time")

        return {
            "agent_id": self._config.agent_id,
            "pending_notifications": pending_count,
            "summary_count": summary_count,
            "action_count": action_count,
            "event_count": event_count,
            "last_seen_entry_id": int(last_tick) if last_tick else None,
            "last_bootstrap_time": last_bootstrap,
        }

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
    parser = argparse.ArgumentParser(description="Agent Working Memory MCP Server (stdio)")
    parser.add_argument("--wm-db-path", required=True, help="Path to working memory SQLite database")
    parser.add_argument("--bbs-db-path", required=True, help="Path to BBS SQLite database")
    parser.add_argument("--config", default=None, help="Path to agent-config.yaml")
    args = parser.parse_args()

    # Load config
    if args.config:
        config = AgentConfig.from_yaml(args.config)
    else:
        config = generate_default_config()

    # Open databases
    wm_conn = sqlite3.connect(args.wm_db_path)
    wm_conn.execute("PRAGMA journal_mode=WAL")
    wm_conn.row_factory = sqlite3.Row
    create_working_memory_tables(wm_conn)
    _ensure_metadata_table(wm_conn)

    bbs_conn = sqlite3.connect(args.bbs_db_path)
    bbs_conn.execute("PRAGMA journal_mode=WAL")
    bbs_conn.execute("PRAGMA foreign_keys = ON")
    bbs_conn.row_factory = sqlite3.Row
    create_bbs_tables(bbs_conn)

    server = WMServer(bbs_conn, wm_conn, config)
    server.run_stdio()


if __name__ == "__main__":
    main()
