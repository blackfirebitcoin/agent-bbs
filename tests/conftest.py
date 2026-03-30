"""Shared fixtures for Agent BBS tests.

Every test gets a fresh in-memory SQLite database for full isolation.
"""

import sqlite3

import pytest


@pytest.fixture()
def db():
    """Yield an in-memory SQLite connection with WAL-like pragmas."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture()
def db_with_schema(db):
    """Yield a db connection with all Phase 1 tables created."""
    from agent_bbs.schema import create_tables

    create_tables(db)
    return db
