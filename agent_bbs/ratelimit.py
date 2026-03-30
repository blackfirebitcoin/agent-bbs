"""Rate limiting — sliding window, 10 posts per minute per agent.

Uses a simple in-memory + SQLite hybrid:
- In-memory dict for fast checks (cleared on restart)
- SQLite append for durability across restarts
"""

import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone

# In-memory sliding window: agent_id -> sorted list of timestamps
_window: dict[str, list[float]] = defaultdict(list)
WINDOW_SECONDS = 60
WINDOW_MAX = 10


def _clean_window(agent_id: str, now: float) -> None:
    """Drop timestamps outside the window."""
    _window[agent_id] = [
        t for t in _window[agent_id]
        if now - t < WINDOW_SECONDS
    ]


def check_rate_limit(conn: sqlite3.Connection, agent_id: str) -> tuple[bool, int]:
    """Check if agent is within rate limit.

    Returns (allowed, remaining). allowed=False means the request should be rejected.
    """
    now = time.time()
    _clean_window(agent_id, now)

    remaining = WINDOW_MAX - len(_window[agent_id])
    if remaining <= 0:
        return False, 0

    return True, remaining - 1


def record_post(conn: sqlite3.Connection, agent_id: str) -> None:
    """Record a post timestamp for rate limiting."""
    now = time.time()
    _window[agent_id].append(now)

    # Also persist to SQLite for crash-restart grace
    ts = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        conn.execute(
            "INSERT INTO rate_limits (agent_id, created_at) VALUES (?, ?)",
            (agent_id, ts),
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # table might not exist yet


def get_remaining(conn: sqlite3.Connection, agent_id: str) -> int:
    """Get remaining posts for agent in current window."""
    now = time.time()
    _clean_window(agent_id, now)
    return max(0, WINDOW_MAX - len(_window[agent_id]))
