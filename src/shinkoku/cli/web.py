"""Web UI launcher (FastAPI + HTMX)."""

from __future__ import annotations

import argparse
import sys


def register(parent_subparsers: argparse._SubParsersAction) -> None:
    """Register the `web` subcommand to launch a local server."""
    parser = parent_subparsers.add_parser(
        "web",
        description="Launch Web UI (FastAPI + HTMX)",
        help="Web UI 起動",
    )
    parser.add_argument("--db-path", required=True, help="SQLite DB path")
    parser.add_argument("--fiscal-year", required=True, type=int, help="Fiscal year")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", default=8000, type=int, help="Bind port")
    parser.add_argument(
        "--reload", action="store_true", help="Enable auto-reload (dev only)"
    )
    parser.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> None:
    try:
        from shinkoku.web.app import create_app
        import uvicorn
    except Exception:
        print(
            (
                "{\"status\": \"error\", \"message\": "
                "\"FastAPI/uvicorn not available. Install with: pip install fastapi uvicorn jinja2 python-multipart\""
                "}"
            )
        )
        sys.exit(1)

    app = create_app(db_path=args.db_path, fiscal_year=args.fiscal_year)
    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
