"""Resolving review-queue items — shared by the CLI and the web UI.

A queued item is a Standard Ebooks book the sync pipeline could not match to a
Hardcover book confidently. Resolving it means one of:

- **attach**: add the SE edition to an existing Hardcover book the librarian picked,
- **create**: create a brand-new Hardcover book (+ the SE edition), or
- **skip**: leave it alone and mark it handled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .hardcover import HardcoverClient
from .models import SeBook
from .state import Store
from .sync import RefData, build_edition_dto, create_se_edition, finalize_edition

logger = logging.getLogger(__name__)


def refresh_queue(store: Store, client: HardcoverClient) -> dict[str, int]:
    """Re-score every pending review item with the current matcher.

    Existing queue rows keep whatever candidates were computed when they were
    enqueued. After the matcher improves (e.g. the title-only fallback), call
    this to recompute each item's candidates and reason so the UI/CLI reflect
    the better matches. It refreshes candidates only — it never auto-adds.
    """
    from .matching import score_candidate
    from .sync import find_match

    updated = 0
    now_confident = 0
    for item in store.pending_reviews():
        book = store.get_book(item["se_url"])
        if book is None:
            continue
        result = find_match(book, client)
        cand_dicts = [
            {
                "id": c.id,
                "title": c.title,
                "slug": c.slug,
                "authors": c.author_names,
                "score": round(score_candidate(book, c), 3),
            }
            for c in result.candidates
        ]
        store.enqueue_review(book.se_url, book.title, result.reason, cand_dicts)
        updated += 1
        if result.decision.value == "confident":
            now_confident += 1
    return {"updated": updated, "now_confident": now_confident}


@dataclass
class ReviewResolution:
    se_url: str
    action: str
    book_id: int | None = None
    edition_id: int | None = None
    detail: str = ""


def resolve_review_item(
    store: Store,
    client: HardcoverClient,
    ref: RefData,
    se_url: str,
    action: str,
    *,
    book_id: int | None = None,
) -> ReviewResolution:
    """Apply a librarian's decision to a queued book and mark it resolved."""
    book = store.get_book(se_url)
    if book is None:
        raise ValueError(f"{se_url} is not in the catalog.")

    if action == "skip":
        store.mark_processed(se_url, "skipped_existing", detail="review:skip")
        store.resolve_review(se_url)
        return ReviewResolution(se_url, "skip", detail="skipped")

    if action == "attach":
        if not book_id:
            raise ValueError("attach requires a Hardcover book_id.")
        edition_id, cover_detail = create_se_edition(book, client, ref, book_id)
        store.mark_processed(se_url, "added", book_id=book_id, edition_id=edition_id,
                             detail="review:attach")
        store.resolve_review(se_url)
        return ReviewResolution(se_url, "attach", book_id=book_id, edition_id=edition_id,
                                detail=cover_detail)

    if action == "create":
        book_id, edition_id, cover_detail = _create_new_book(book, client, ref)
        store.mark_processed(se_url, "added", book_id=book_id, edition_id=edition_id,
                             detail="review:create")
        store.resolve_review(se_url)
        return ReviewResolution(se_url, "create", book_id=book_id, edition_id=edition_id,
                                detail=cover_detail)

    raise ValueError(f"Unknown review action: {action!r}")


def _create_new_book(
    book: SeBook, client: HardcoverClient, ref: RefData
) -> tuple[int | None, int | None, str]:
    result = client.insert_book_with_edition(build_edition_dto(book, ref))
    new_edition = result.get("edition") or {}
    edition_id = result.get("id") or new_edition.get("id")
    book_id = new_edition.get("book_id")
    cover_detail = finalize_edition(book, client, ref, edition_id) if edition_id else "no edition"
    return book_id, edition_id, cover_detail
