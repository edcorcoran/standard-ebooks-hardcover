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

from .hardcover import HardcoverClient, se_contributions_to_dto
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


def auto_create_orphans(
    store: Store,
    client: HardcoverClient,
    ref: RefData,
    *,
    attach_confident: bool = False,
) -> dict[str, list[str] | int]:
    """Drain queue items the matcher can now decide on its own.

    Re-searches Hardcover live for every pending review item (so candidate reader
    counts are fresh) and resolves the ones the matcher is sure about:

    - **CREATE** — no attach target, only junk stubs or different books — always
      creates a new Hardcover book.
    - **CONFIDENT** — a strong match to an existing book. Only attached when
      ``attach_confident`` is set (the librarian opted in); the confidence is
      re-verified here at write time, so nothing borderline slips through. Skipped
      if that book already carries an SE edition.

    Everything else (REVIEW, and CONFIDENT when not opted in) is left in the queue
    for a human. Returns the SE URLs created/attached and how many were left.
    """
    from .matching import MatchDecision
    from .sync import find_match

    existing = client.se_edition_book_ids(ref.publisher_id) if attach_confident else set()
    created: list[str] = []
    attached: list[str] = []
    left = 0
    for item in list(store.pending_reviews()):
        book = store.get_book(item["se_url"])
        if book is None:
            left += 1
            continue
        result = find_match(book, client)
        try:
            if result.decision == MatchDecision.CREATE:
                resolve_review_item(store, client, ref, item["se_url"], "create")
                created.append(item["se_url"])
            elif (
                attach_confident
                and result.decision == MatchDecision.CONFIDENT
                and result.best is not None
                and result.best.id not in existing
            ):
                resolve_review_item(
                    store, client, ref, item["se_url"], "attach", book_id=result.best.id
                )
                existing.add(result.best.id)
                attached.append(item["se_url"])
            else:
                left += 1
        except Exception as exc:  # one bad book must not stop the drain
            logger.warning("auto-resolve failed for %s: %s", item["se_url"], exc)
            left += 1
    return {
        "created": created,
        "created_count": len(created),
        "attached": attached,
        "attached_count": len(attached),
        "left": left,
    }


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

    # Idempotency guard: if this item was already added (a prior click, a retry, or
    # a concurrent run), do NOT create/attach again — that is exactly what spawns
    # duplicate Hardcover books. Return the recorded result as a no-op.
    if action in ("attach", "create"):
        prior = store.processed_row(se_url)
        if prior and prior.get("status") == "added":
            store.resolve_review(se_url)
            return ReviewResolution(
                se_url, action,
                book_id=prior.get("hardcover_book_id"),
                edition_id=prior.get("hardcover_edition_id"),
                detail="already resolved (no-op)",
            )

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
    if edition_id:
        set_book_authors(client, edition_id, book)
    return book_id, edition_id, cover_detail


def set_book_authors(client: HardcoverClient, edition_id: int, book: SeBook) -> None:
    """Attach the SE book's contributors to a newly-created book.

    A new Hardcover book starts with no author; setting the contributions on its
    edition surfaces the authors (and translators) on the book itself. Without
    this, created books are authorless — unfindable and duplicate-prone.
    """
    contributions = se_contributions_to_dto(book.contributors, client.resolve_author_id)
    if not contributions:
        logger.warning("No resolvable authors for %s; book will be authorless", book.se_url)
        return
    # update_edition occasionally drops the write; verify the authors persisted.
    for attempt in range(3):
        client.update_edition(edition_id, {"contributions": contributions})
        if client.edition_contributions(edition_id):
            return
        logger.warning(
            "Author write did not persist for edition %s (attempt %d); retrying",
            edition_id, attempt + 1,
        )
    logger.error("Failed to set authors on edition %s (%s)", edition_id, book.se_url)
