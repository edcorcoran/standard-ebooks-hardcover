"""The shared add-an-edition pipeline used by both backfill and the daemon.

Given an :class:`SeBook`, it searches Hardcover for the canonical work, and
either adds a Standard Ebooks edition (when the match is confident) or routes
the book to the review queue. It never creates a new Hardcover book on its own —
that only happens through an explicit human decision in the ``review`` command.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from .hardcover import DryRunMutation, HardcoverClient
from .matching import match_book
from .models import HardcoverBookMatch, MatchDecision, MatchResult, SeBook
from .notify import Notifier
from .state import Store

logger = logging.getLogger(__name__)


def find_match(book: SeBook, client: HardcoverClient) -> MatchResult:
    """Search Hardcover for the book's canonical work.

    Searches ``"<title> <author>"`` first. Hardcover's search occasionally ranks
    an unrelated popular book above a real-but-obscure one when the author tokens
    confuse it (e.g. "The Four Feathers" returning "The Odyssey"). So when that
    query does not yield a confident match, we broaden the pool with a
    title-only search and re-match over the union.
    """
    author = book.author_names[0] if book.author_names else ""
    primary = client.search_books(f"{book.title} {author}".strip())
    result = match_book(book, primary)
    if result.decision == MatchDecision.CONFIDENT or not author:
        return result

    secondary = client.search_books(book.title)
    merged = _dedupe_candidates(primary + secondary)
    if len(merged) == len(primary):
        return result  # title-only surfaced nothing new
    return match_book(book, merged)


def _dedupe_candidates(candidates: list[HardcoverBookMatch]) -> list[HardcoverBookMatch]:
    seen: set[int] = set()
    out: list[HardcoverBookMatch] = []
    for c in candidates:
        if c.id not in seen:
            seen.add(c.id)
            out.append(c)
    return out


@dataclass
class RefData:
    """Hardcover reference ids resolved once per run."""

    publisher_id: int
    ebook_format_id: int
    english_language_id: int
    # Optional marker on each SE edition (e.g. "Standard Ebooks"). Left unset by
    # default; the Phase 0 probe found SE editions need no distinct edition note.
    edition_information: str | None = None


def resolve_ref_data(
    client: HardcoverClient,
    *,
    publisher_slug: str = "standard-ebooks",
    edition_information: str | None = None,
) -> RefData:
    """Resolve Hardcover reference ids at runtime (never hard-coded)."""
    publisher_id = client.publisher_id(publisher_slug)
    if not publisher_id:
        raise RuntimeError(f"Publisher '{publisher_slug}' not found on Hardcover.")

    formats = client.reading_formats()
    ebook_id = formats.get("ebook") or formats.get("e-book")
    if not ebook_id:
        raise RuntimeError(f"Could not resolve ebook reading_format_id from {formats}")

    languages = client.languages_by_code(("en", "eng", "english"))
    english_id = languages.get("en") or languages.get("eng") or languages.get("english")
    if not english_id:
        raise RuntimeError(f"Could not resolve English language_id from {languages}")

    return RefData(
        publisher_id=publisher_id,
        ebook_format_id=ebook_id,
        english_language_id=english_id,
        edition_information=edition_information,
    )


class Outcome(StrEnum):
    ADDED = "added"
    SKIPPED_EXISTING = "skipped_existing"
    QUEUED = "queued"
    ERROR = "error"
    DRY_RUN = "dry_run"


@dataclass
class SyncResult:
    se_url: str
    outcome: Outcome
    book_id: int | None = None
    edition_id: int | None = None
    detail: str = ""


def build_edition_dto(book: SeBook, ref: RefData) -> dict:
    """Assemble the BookDtoInput for a Standard Ebooks ebook edition."""
    dto: dict = {
        "title": book.title,
        "publisher_id": ref.publisher_id,
        "reading_format_id": ref.ebook_format_id,
        "language_id": ref.english_language_id,
    }
    if book.subtitle:
        dto["subtitle"] = book.subtitle
    if book.release_date:
        dto["release_date"] = book.release_date
    if ref.edition_information:
        dto["edition_information"] = ref.edition_information
    return dto


def sync_book(
    book: SeBook,
    client: HardcoverClient,
    store: Store,
    ref: RefData,
    notifier: Notifier,
    *,
    force: bool = False,
    existing_book_ids: set[int] | None = None,
) -> SyncResult:
    """Process one Standard Ebooks book end to end.

    ``existing_book_ids`` is the set of book_ids already carrying a publisher
    edition. Pass it (fetched once) during bulk backfill to avoid re-querying
    per book; when omitted it is fetched lazily on the first confident match.
    """
    if not force and store.is_done(book.se_url):
        return SyncResult(book.se_url, Outcome.SKIPPED_EXISTING, detail="already processed")

    # 1. Find candidate books on Hardcover.
    try:
        result = find_match(book, client)
    except Exception as exc:
        logger.warning("Search failed for %s: %s", book.se_url, exc)
        store.mark_processed(book.se_url, "error", detail=f"search: {exc}")
        return SyncResult(book.se_url, Outcome.ERROR, detail=str(exc))

    # 2. No confident match -> review queue.
    if result.decision != MatchDecision.CONFIDENT:
        cand_dicts = [
            {
                "id": c.id,
                "title": c.title,
                "slug": c.slug,
                "authors": c.author_names,
                "score": round(_score_for(book, c), 3),
            }
            for c in result.candidates
        ]
        store.enqueue_review(book.se_url, book.title, result.reason, cand_dicts)
        store.mark_processed(book.se_url, "queued", detail=result.reason)
        notifier.queued(book.title, result.reason)
        return SyncResult(book.se_url, Outcome.QUEUED, detail=result.reason)

    # 3. Confident match -> add an edition (unless one already exists).
    book_id = result.best.id
    if existing_book_ids is None:
        try:
            existing_book_ids = client.se_edition_book_ids(ref.publisher_id)
        except Exception as exc:
            logger.warning("Could not list existing editions: %s", exc)
            existing_book_ids = set()

    if book_id in existing_book_ids and not force:
        store.mark_processed(
            book.se_url, "skipped_existing", book_id=book_id,
            detail="SE edition already present on this book",
        )
        return SyncResult(book.se_url, Outcome.SKIPPED_EXISTING, book_id=book_id)

    try:
        edition_id, cover_detail = create_se_edition(book, client, ref, book_id)
    except DryRunMutation:
        logger.info("[dry-run] would add edition to book %s for %s", book_id, book.title)
        return SyncResult(book.se_url, Outcome.DRY_RUN, book_id=book_id, detail="would insert_edition")
    except Exception as exc:
        store.mark_processed(book.se_url, "error", book_id=book_id, detail=f"insert_edition: {exc}")
        notifier.error("insert_edition", f"{book.title}: {exc}")
        return SyncResult(book.se_url, Outcome.ERROR, book_id=book_id, detail=str(exc))

    store.mark_processed(
        book.se_url, "added", book_id=book_id, edition_id=edition_id, detail=cover_detail
    )
    notifier.added(book.title, f"https://hardcover.app/books/{result.best.slug}")
    return SyncResult(
        book.se_url, Outcome.ADDED, book_id=book_id, edition_id=edition_id, detail=cover_detail
    )


def create_se_edition(
    book: SeBook, client: HardcoverClient, ref: RefData, book_id: int
) -> tuple[int, str]:
    """Add a fully-populated Standard Ebooks edition to an existing book.

    Uses the recipe confirmed by the Phase 0 probe against the (beta) API:

    1. ``insert_edition`` — creates the edition, but only ``title`` and
       ``reading_format_id`` reliably persist from the insert payload.
    2. ``insert_image`` — uploads the cover (Hardcover rehosts it); it does NOT
       auto-link to the edition.
    3. ``update_edition`` — re-applies the full field set (publisher, language,
       release date, subtitle, …) AND links the cover via ``image_id``, in one call.

    Returns (edition_id, cover_detail).
    """
    edition = client.insert_edition(book_id, build_edition_dto(book, ref))
    edition_id = edition.get("id") or (edition.get("edition") or {}).get("id")
    if not edition_id:
        raise RuntimeError(f"insert_edition returned no edition id: {edition}")
    cover_detail = finalize_edition(book, client, ref, edition_id)
    return edition_id, cover_detail


def finalize_edition(
    book: SeBook, client: HardcoverClient, ref: RefData, edition_id: int
) -> str:
    """Steps 2-3 of the recipe: upload the cover, then re-apply all fields + link it.

    Shared by the add-edition path and the create-new-book path, since
    ``insert_edition`` and ``insert_book`` both drop most dto fields on insert.
    """
    cover_detail = "no cover"
    image_id: int | None = None
    if book.cover_url:
        try:
            image_id = client.insert_image(edition_id, book.cover_url, "Edition")
            cover_detail = f"cover image {image_id}"
        except Exception as exc:  # a bad cover must not lose the edition's metadata
            logger.warning("insert_image failed for %s: %s", book.se_url, exc)
            cover_detail = f"cover failed: {exc}"

    update = build_edition_dto(book, ref)
    if image_id:
        update["image_id"] = image_id

    try:
        if not update_edition_verified(client, edition_id, update):
            cover_detail += " (WARNING: fields may not have persisted)"
    except DryRunMutation:
        pass
    return cover_detail


# Columns of an edition that update_edition_verified can read back to confirm a write.
_VERIFIABLE_FIELDS = (
    "publisher_id", "language_id", "release_date", "reading_format_id", "image_id",
    "isbn_10", "isbn_13", "asin",
)


def _field_matches(actual, intended) -> bool:
    """Compare a read-back field to what we wrote, treating a clear as falsy."""
    if intended in (None, ""):  # we asked to clear the field
        return actual in (None, "")
    return actual == intended


def update_edition_verified(
    client: HardcoverClient, edition_id: int, dto: dict, *, retries: int = 3
) -> bool:
    """update_edition, then confirm the write actually persisted, retrying if not.

    Hardcover's ``update_edition`` occasionally reports success but does not
    persist (observed ~1 in 14, both right after insert and during audit fixes).
    We read the edition back and compare the fields we can verify; on mismatch we
    retry. Returns True if the write is confirmed, False if it never stuck.
    """
    verifiable = {k: dto[k] for k in dto if k in _VERIFIABLE_FIELDS}
    for attempt in range(retries):
        client.update_edition(edition_id, dto)
        if not verifiable:
            return True
        try:
            fields = client.edition_fields(edition_id) or {}
        except Exception as exc:
            logger.debug("Verify query failed for edition %s: %s", edition_id, exc)
            fields = {}
        if all(_field_matches(fields.get(k), v) for k, v in verifiable.items()):
            return True
        logger.warning(
            "update_edition on %s did not persist (attempt %d); retrying",
            edition_id, attempt + 1,
        )
    logger.error("update_edition on %s never persisted: %s", edition_id, verifiable)
    return False


def _score_for(book: SeBook, cand) -> float:
    from .matching import score_candidate

    return score_candidate(book, cand)
