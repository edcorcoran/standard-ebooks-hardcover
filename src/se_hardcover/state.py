"""Local sqlite state: catalog cache, processed-book log, and review queue.

One database file (``STATE_DB_PATH``) holds everything so the backfill and the
daemon share the same view and both are resumable.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import Contributor, SeBook

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog (
    se_url        TEXT PRIMARY KEY,
    repo          TEXT NOT NULL,
    title         TEXT NOT NULL,
    subtitle      TEXT,
    contributors  TEXT NOT NULL DEFAULT '[]',   -- JSON list of Contributor
    description   TEXT,
    short_description TEXT,
    subjects      TEXT NOT NULL DEFAULT '[]',   -- JSON list
    release_date  TEXT,
    cover_url     TEXT,
    word_count    INTEGER,
    fetched_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS processed (
    se_url        TEXT PRIMARY KEY,
    hardcover_book_id     INTEGER,
    hardcover_edition_id  INTEGER,
    status        TEXT NOT NULL,               -- added | skipped_existing | queued | error
    detail        TEXT,
    updated_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS review_queue (
    se_url        TEXT PRIMARY KEY,
    title         TEXT,
    reason        TEXT,
    candidates    TEXT NOT NULL DEFAULT '[]',   -- JSON list of candidate books
    created_at    TEXT DEFAULT (datetime('now')),
    resolved      INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # -- catalog -----------------------------------------------------------

    def upsert_book(self, book: SeBook) -> None:
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO catalog (se_url, repo, title, subtitle, contributors,
                    description, short_description, subjects, release_date,
                    cover_url, word_count, fetched_at)
                VALUES (:se_url, :repo, :title, :subtitle, :contributors,
                    :description, :short_description, :subjects, :release_date,
                    :cover_url, :word_count, datetime('now'))
                ON CONFLICT(se_url) DO UPDATE SET
                    repo=excluded.repo, title=excluded.title, subtitle=excluded.subtitle,
                    contributors=excluded.contributors, description=excluded.description,
                    short_description=excluded.short_description, subjects=excluded.subjects,
                    release_date=excluded.release_date, cover_url=excluded.cover_url,
                    word_count=excluded.word_count, fetched_at=datetime('now')
                """,
                _book_to_row(book),
            )

    def get_book(self, se_url: str) -> SeBook | None:
        row = self._conn.execute(
            "SELECT * FROM catalog WHERE se_url = ?", (se_url,)
        ).fetchone()
        return _row_to_book(row) if row else None

    def has_book(self, se_url: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM catalog WHERE se_url = ?", (se_url,)
            ).fetchone()
            is not None
        )

    def all_books(self) -> list[SeBook]:
        rows = self._conn.execute("SELECT * FROM catalog ORDER BY se_url").fetchall()
        return [_row_to_book(r) for r in rows]

    def catalog_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0]

    # -- processed log -----------------------------------------------------

    def mark_processed(
        self,
        se_url: str,
        status: str,
        *,
        book_id: int | None = None,
        edition_id: int | None = None,
        detail: str = "",
    ) -> None:
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO processed (se_url, hardcover_book_id, hardcover_edition_id,
                    status, detail, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(se_url) DO UPDATE SET
                    hardcover_book_id=excluded.hardcover_book_id,
                    hardcover_edition_id=excluded.hardcover_edition_id,
                    status=excluded.status, detail=excluded.detail,
                    updated_at=datetime('now')
                """,
                (se_url, book_id, edition_id, status, detail),
            )

    def processed_status(self, se_url: str) -> str | None:
        row = self._conn.execute(
            "SELECT status FROM processed WHERE se_url = ?", (se_url,)
        ).fetchone()
        return row["status"] if row else None

    def processed_row(self, se_url: str) -> dict[str, Any] | None:
        """Return the full processed record for a book, or None if never processed."""
        row = self._conn.execute(
            "SELECT * FROM processed WHERE se_url = ?", (se_url,)
        ).fetchone()
        return dict(row) if row else None

    def is_done(self, se_url: str) -> bool:
        """True if this book was successfully added or intentionally skipped."""
        return self.processed_status(se_url) in {"added", "skipped_existing"}

    def processed_summary(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) n FROM processed GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # -- review queue ------------------------------------------------------

    def enqueue_review(
        self, se_url: str, title: str, reason: str, candidates: list[dict[str, Any]]
    ) -> None:
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO review_queue (se_url, title, reason, candidates, resolved)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(se_url) DO UPDATE SET
                    title=excluded.title, reason=excluded.reason,
                    candidates=excluded.candidates, resolved=0
                """,
                (se_url, title, reason, json.dumps(candidates)),
            )

    def pending_reviews(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM review_queue WHERE resolved = 0 ORDER BY created_at"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["candidates"] = json.loads(d["candidates"])
            out.append(d)
        return out

    def resolve_review(self, se_url: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE review_queue SET resolved = 1 WHERE se_url = ?", (se_url,)
            )


# -- row adapters ---------------------------------------------------------


def _book_to_row(book: SeBook) -> dict[str, Any]:
    return {
        "se_url": book.se_url,
        "repo": book.repo,
        "title": book.title,
        "subtitle": book.subtitle,
        "contributors": json.dumps([asdict(c) for c in book.contributors]),
        "description": book.description,
        "short_description": book.short_description,
        "subjects": json.dumps(book.subjects),
        "release_date": book.release_date,
        "cover_url": book.cover_url,
        "word_count": book.word_count,
    }


def _row_to_book(row: sqlite3.Row) -> SeBook:
    return SeBook(
        se_url=row["se_url"],
        repo=row["repo"],
        title=row["title"],
        subtitle=row["subtitle"] or "",
        contributors=[Contributor(**c) for c in json.loads(row["contributors"])],
        description=row["description"] or "",
        short_description=row["short_description"] or "",
        subjects=json.loads(row["subjects"]),
        release_date=row["release_date"] or "",
        cover_url=row["cover_url"] or "",
        word_count=row["word_count"],
    )
