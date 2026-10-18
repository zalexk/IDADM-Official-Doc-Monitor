"""Operational CLI.

Examples:
    python -m app.cli init-db
    python -m app.cli check
    python -m app.cli check --document sea-course-list --full
    python -m app.cli status
    python -m app.cli changes --document sea-course-list
    python -m app.cli show-change --document sea-course-list --change-id 1 --format markdown
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from . import db, presenters
from .config import ConfigError, Settings, load_documents
from .monitor import MonitorService


def _configure_output(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:  # pragma: no cover
        pass


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _prepare(settings: Settings) -> tuple[Any, ...]:
    documents = load_documents(settings.documents_config)
    db.init_db(settings)
    db.seed_documents(settings, documents)
    return documents


def _cmd_init_db(settings: Settings) -> int:
    documents = _prepare(settings)
    _print_json(
        {
            "database": str(settings.database_path),
            "data_dir": str(settings.data_dir),
            "documents": [document.key for document in documents],
        }
    )
    return 0


def _cmd_check(settings: Settings, args: argparse.Namespace) -> int:
    documents = _prepare(settings)
    service = MonitorService(settings, documents)

    async def run() -> list[dict[str, Any]]:
        if args.document:
            return [await service.check_document(args.document, force_full=args.full)]
        return await service.check_all(force_full=args.full)

    try:
        results = asyncio.run(run())
    except KeyError:
        print(f"Unknown document: {args.document}", file=sys.stderr)
        return 2
    _print_json({"results": results})
    return 1 if any(result.get("outcome") == "error" for result in results) else 0


def _cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    documents = _prepare(settings)
    if args.document and args.document not in {document.key for document in documents}:
        print(f"Unknown document: {args.document}", file=sys.stderr)
        return 2
    keys = [args.document] if args.document else [document.key for document in documents]
    with db.read(settings) as conn:
        statuses = [
            presenters.build_document_status(conn, settings, key)
            for key in keys
        ]
    _print_json({"documents": [status for status in statuses if status is not None]})
    return 0


def _cmd_changes(settings: Settings, args: argparse.Namespace) -> int:
    documents = _prepare(settings)
    if args.document and args.document not in {document.key for document in documents}:
        print(f"Unknown document: {args.document}", file=sys.stderr)
        return 2
    keys = [args.document] if args.document else [document.key for document in documents]
    output = []
    with db.read(settings) as conn:
        for key in keys:
            output.append(
                {
                    "document_key": key,
                    "changes": presenters.build_change_list(
                        conn, key, args.limit, args.offset
                    ),
                }
            )
    _print_json({"documents": output})
    return 0


def _cmd_show_change(settings: Settings, args: argparse.Namespace) -> int:
    _prepare(settings)
    with db.read(settings) as conn:
        detail = presenters.build_change_detail(
            conn, args.document, args.change_id
        )
    if detail is None:
        print(
            f"Change {args.change_id} not found for {args.document}",
            file=sys.stderr,
        )
        return 2
    if args.format == "markdown":
        print(detail["markdown"])
    elif args.format == "diff":
        print(detail["unified_diff"])
    else:
        _print_json(detail)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="CUHK CDMP PDF monitor operations",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create the database and seed documents")

    check_parser = subparsers.add_parser("check", help="Run a check now")
    check_parser.add_argument("--document", help="Only check this document key")
    check_parser.add_argument(
        "--full",
        action="store_true",
        help="Ignore HTTP validators and download the full file",
    )

    status_parser = subparsers.add_parser("status", help="Show document status")
    status_parser.add_argument("--document", help="Only show this document key")

    changes_parser = subparsers.add_parser("changes", help="List recorded changes")
    changes_parser.add_argument("--document", help="Only show this document key")
    changes_parser.add_argument("--limit", type=int, default=20)
    changes_parser.add_argument("--offset", type=int, default=0)

    show_parser = subparsers.add_parser("show-change", help="Show one change report")
    show_parser.add_argument("--document", required=True)
    show_parser.add_argument("--change-id", type=int, required=True)
    show_parser.add_argument(
        "--format",
        choices=["json", "markdown", "diff"],
        default="json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    _configure_output(settings)

    try:
        if args.command == "init-db":
            return _cmd_init_db(settings)
        if args.command == "check":
            return _cmd_check(settings, args)
        if args.command == "status":
            return _cmd_status(settings, args)
        if args.command == "changes":
            return _cmd_changes(settings, args)
        if args.command == "show-change":
            return _cmd_show_change(settings, args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        logging.getLogger(__name__).exception("Command failed")
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    parser.error("Unknown command")
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
