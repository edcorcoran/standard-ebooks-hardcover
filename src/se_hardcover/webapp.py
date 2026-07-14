"""Local web UI for reviewing the queue and the audit CSV.

A lightweight FastAPI app (optional ``[web]`` extra) that lets the librarian
approve/deny review-queue matches and audit fixes visually, instead of via the
CLI. It reuses the same core logic (:mod:`review`, :mod:`audit`) so the two
front-ends never drift.

Launch with ``se-hardcover web``. It binds to localhost only.
"""

from __future__ import annotations

import csv
import logging
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .audit import CSV_FIELDS
from .audit import apply_fixes as apply_fixes_impl
from .config import Settings, load_settings
from .hardcover import HardcoverClient
from .review import refresh_queue, resolve_review_item
from .state import Store
from .sync import RefData, resolve_ref_data

logger = logging.getLogger(__name__)


class ResolveRequest(BaseModel):
    se_url: str
    action: str  # attach | create | skip
    book_id: int | None = None


class AuditSaveRequest(BaseModel):
    # Map of edition_id -> {field -> approved(bool)}. Applied per (edition, field).
    approvals: dict[str, dict[str, bool]]


def create_app(
    settings: Settings | None = None,
    audit_csv: Path | None = None,
    client_provider=None,
) -> FastAPI:
    settings = settings or load_settings()
    audit_csv = audit_csv or Path("audit-report.csv")
    app = FastAPI(title="se-hardcover review")

    # Resolve Hardcover reference data / client lazily and cache it, so the app
    # starts even before a token is needed (and a bad token surfaces per-request).
    # ``client_provider`` is an injection point for tests.
    @lru_cache(maxsize=1)
    def _client_and_ref() -> tuple[HardcoverClient, RefData]:
        if client_provider is not None:
            return client_provider()
        client = HardcoverClient(settings.require_token())
        return client, resolve_ref_data(client)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _index_html()

    @app.get("/api/queue")
    def get_queue() -> JSONResponse:
        with Store(settings.state_db_path) as store:
            pending = store.pending_reviews()
            # Enrich candidates with covers + hardcover URLs (one batched query).
            covers: dict[int, dict[str, Any]] = {}
            try:
                client, _ = _client_and_ref()
                ids = [c["id"] for item in pending for c in item["candidates"]]
                covers = client.books_with_covers(sorted(set(ids)))
            except Exception as exc:  # covers are a nicety; queue still loads
                logger.warning("Could not fetch candidate covers: %s", exc)

            items = []
            for item in pending:
                book = store.get_book(item["se_url"])
                for c in item["candidates"]:
                    meta = covers.get(c["id"], {})
                    c["image_url"] = meta.get("image_url")
                    c["hardcover_url"] = f"https://hardcover.app/books/{c.get('slug')}"
                items.append({
                    "se_url": item["se_url"],
                    "title": item["title"],
                    "reason": item["reason"],
                    "candidates": item["candidates"],
                    "se": {
                        "author": ", ".join(book.author_names) if book else "",
                        "subtitle": book.subtitle if book else "",
                        "release_date": book.release_date if book else "",
                        "cover_url": book.cover_url if book else "",
                    },
                })
            return JSONResponse({"count": len(items), "items": items})

    @app.post("/api/queue/refresh")
    def refresh() -> JSONResponse:
        try:
            client, _ = _client_and_ref()
            with Store(settings.state_db_path) as store:
                summary = refresh_queue(store, client)
        except Exception as exc:
            logger.exception("queue refresh failed")
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse(summary)

    @app.post("/api/queue/resolve")
    def resolve(req: ResolveRequest) -> JSONResponse:
        if req.action not in {"attach", "create", "skip"}:
            raise HTTPException(400, f"bad action {req.action!r}")
        try:
            client, ref = _client_and_ref()
            with Store(settings.state_db_path) as store:
                res = resolve_review_item(
                    store, client, ref, req.se_url, req.action, book_id=req.book_id
                )
        except Exception as exc:
            logger.exception("resolve failed")
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse({
            "se_url": res.se_url, "action": res.action,
            "book_id": res.book_id, "edition_id": res.edition_id, "detail": res.detail,
        })

    @app.get("/api/audit")
    def get_audit() -> JSONResponse:
        rows = _read_audit(audit_csv)
        return JSONResponse({"count": len(rows), "rows": rows, "exists": audit_csv.exists()})

    @app.post("/api/audit/save")
    def save_audit(req: AuditSaveRequest) -> JSONResponse:
        rows = _read_audit(audit_csv)
        for r in rows:
            per_field = req.approvals.get(str(r["edition_id"]), {})
            if r["field"] in per_field:
                r["approve"] = "yes" if per_field[r["field"]] else ""
        _write_audit(audit_csv, rows)
        approved = sum(1 for r in rows if r["approve"] == "yes")
        return JSONResponse({"saved": len(rows), "approved": approved})

    @app.post("/api/audit/apply")
    def apply_audit() -> JSONResponse:
        if not audit_csv.exists():
            raise HTTPException(400, "no audit CSV to apply")
        try:
            client, _ = _client_and_ref()
            summary = apply_fixes_impl(client, audit_csv)
        except Exception as exc:
            logger.exception("apply-fixes failed")
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse(summary)

    return app


# -- audit CSV helpers ----------------------------------------------------


def _read_audit(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _write_audit(path: Path, rows: list[dict[str, str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows({k: r.get(k, "") for k in CSV_FIELDS} for r in rows)


def _index_html() -> str:
    return resources.files("se_hardcover").joinpath("web/index.html").read_text(
        encoding="utf-8"
    )
