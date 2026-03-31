#!/usr/bin/env python3
"""Run the BBS REST API server with static web UI."""
import os
import sqlite3
import uvicorn

BBS_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BBS_DIR, "bbs.db")
STATIC_DIR = os.path.join(BBS_DIR, "static")


def get_app():
    from agent_bbs.api import create_app
    from agent_bbs.schema import migrate
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import RedirectResponse

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    app = create_app(conn)
    app.mount("/static", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    @app.get("/")
    def index():
        return RedirectResponse(url="/static/index.html")
    @app.get("/static/")
    def static_index():
        from fastapi.responses import FileResponse
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))
    return app


if __name__ == "__main__":
    port = int(os.environ.get("BBS_REST_PORT", 8001))
    host = os.environ.get("BBS_HOST", "0.0.0.0")  # 0.0.0.0 = all interfaces (including Tailscale)
    print(f"Starting BBS REST API + Web UI on {host}:{port}")
    print(f"  API:  http://{host}:{port}/docs")
    print(f"  Web:  http://{host}:{port}/")
    app = get_app()
    uvicorn.run(
        app,
        host=host,
        port=port,
        workers=1,
        limit_concurrency=8,
    )
