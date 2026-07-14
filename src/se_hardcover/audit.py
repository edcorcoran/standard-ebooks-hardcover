"""Audit existing Standard Ebooks editions on Hardcover, then apply approved fixes.

The audit only *reports*: it writes a CSV of discrepancies with a blank
``approve`` column. ``apply_fixes`` re-reads that CSV and executes only the rows
the librarian approved.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import asdict
from pathlib import Path

import httpx

from .hardcover import HardcoverClient
from .matching import normalize_title, surname
from .models import Discrepancy, HardcoverEdition, SeBook
from .standard_ebooks import cover_bytes
from .state import Store
from .sync import RefData, update_edition_verified

logger = logging.getLogger(__name__)

CSV_FIELDS = [
    "edition_id", "hardcover_url", "se_url", "field",
    "problem", "current", "proposed", "fix_kind", "approve",
]


def build_catalog_index(store: Store) -> dict:
    """Index catalog books for matching against existing Hardcover editions.

    Keyed two ways: by ``(normalized title, author surname)`` tuples, and by
    normalized title alone (string key) as a fallback. Title-only keys that are
    ambiguous (two catalog books share a normalized title) are dropped, so the
    fallback can never match the wrong book.
    """
    index: dict = {}
    title_counts: dict[str, int] = {}
    for book in store.all_books():
        nt = normalize_title(book.title)
        title_counts[nt] = title_counts.get(nt, 0) + 1
        for a in book.authors or []:
            index[(nt, surname(a.name))] = book
        index.setdefault(nt, book)  # string key = title-only fallback
    for nt, count in title_counts.items():
        if count > 1:
            index.pop(nt, None)  # ambiguous title — no safe title-only match
    return index


def match_edition_to_catalog(edition: HardcoverEdition, index: dict) -> SeBook | None:
    """Match an existing Hardcover edition to a catalog book.

    Existing editions often carry a noisy edition title ("Frankenstein; Or, The
    Modern Prometheus", "The Golden Bowl, by Henry James") and no edition-level
    contributions, so we try several title variants against both the
    title+author and title-only indexes.
    """
    variants = _title_variants(edition)
    # Author surnames from the edition's own contributions and, importantly, from
    # the book it hangs on — our backfilled editions carry no edition-level
    # contributions, so the book's authors are what let them match.
    surnames = [surname(c.author_name) for c in edition.contributions]
    surnames += [surname(name) for name in edition.book_author_names]
    for nt in variants:
        for sn in surnames:
            book = index.get((nt, sn))
            if book is not None:
                return book
    for nt in variants:
        book = index.get(nt)
        if isinstance(book, SeBook):
            return book
    return None


def _title_variants(edition: HardcoverEdition) -> list[str]:
    """Normalized title candidates, including subtitle-stripped forms."""
    out: list[str] = []
    for raw in (edition.title, edition.book_title):
        if not raw:
            continue
        for candidate in (raw, re.split(r"[:;,]", raw, maxsplit=1)[0]):
            nt = normalize_title(candidate)
            if nt and nt not in out:
                out.append(nt)
    return out


def audit_editions(
    client: HardcoverClient,
    store: Store,
    ref: RefData,
    *,
    check_covers: bool = True,
) -> list[Discrepancy]:
    """Fetch publisher editions and produce a list of discrepancies."""
    editions = client.editions_for_publisher(ref.publisher_id)
    logger.info("Fetched %d editions for publisher %d", len(editions), ref.publisher_id)
    index = build_catalog_index(store)
    http = httpx.Client(timeout=30.0, follow_redirects=True)
    discrepancies: list[Discrepancy] = []
    try:
        for ed in editions:
            se = match_edition_to_catalog(ed, index)
            discrepancies.extend(_check_edition(ed, se, ref, http, check_covers))
    finally:
        http.close()
    logger.info("Audit found %d discrepancies", len(discrepancies))
    return discrepancies


def _check_edition(
    ed: HardcoverEdition,
    se: SeBook | None,
    ref: RefData,
    http: httpx.Client,
    check_covers: bool,
) -> list[Discrepancy]:
    url = f"https://hardcover.app/books/{ed.book_slug}"
    out: list[Discrepancy] = []

    def add(field: str, problem: str, current: str, proposed: str, kind: str) -> None:
        out.append(
            Discrepancy(ed.id, url, se.se_url if se else None, field, problem,
                        current, proposed, kind)
        )

    if se is None:
        add("book_match", "No SE catalog match for this edition", ed.book_title, "",
            "manual")
        return out

    # Reading format should be ebook.
    if ed.reading_format_id != ref.ebook_format_id:
        add("reading_format", "Not marked as ebook",
            str(ed.reading_format or ed.reading_format_id),
            str(ref.ebook_format_id), "update_edition")

    # Release date should equal the SE first-release date.
    if se.release_date and (ed.release_date or "")[:10] != se.release_date:
        add("release_date", "Release date differs from SE",
            ed.release_date or "(none)", se.release_date, "update_edition")

    # Language should be English.
    if ed.language_id is not None and ed.language_id != ref.english_language_id:
        add("language", "Language is not English",
            str(ed.language_id), str(ref.english_language_id), "update_edition")

    # SE books carry no ISBN/ASIN — these can be cleared automatically.
    if ed.isbn_10 or ed.isbn_13:
        add("isbn", "ISBN set on a Standard Ebooks edition",
            ed.isbn_13 or ed.isbn_10 or "", "(clear)", "clear_field")
    if ed.asin:
        add("asin", "ASIN set on a Standard Ebooks edition", ed.asin, "(clear)", "clear_field")

    # Cover: present and matching the SE cover.
    if not ed.image_url:
        add("cover", "Edition has no cover image", "(none)", se.cover_url, "insert_image")
    elif check_covers and se.cover_url:
        verdict = _compare_covers(http, ed.image_url, se.cover_url)
        if verdict == "mismatch":
            add("cover", "Cover does not match SE cover", ed.image_url, se.cover_url,
                "insert_image")

    return out


# -- cover comparison (perceptual dhash) ----------------------------------


def _compare_covers(http: httpx.Client, hardcover_url: str, se_url: str) -> str:
    """Return 'match', 'mismatch', or 'unknown' (on fetch/decoding failure)."""
    try:
        a = _dhash(http.get(hardcover_url).content)
        b = _dhash(cover_bytes(se_url, http))
    except Exception as exc:
        logger.debug("Cover compare failed (%s): %s", hardcover_url, exc)
        return "unknown"
    if a is None or b is None:
        return "unknown"
    distance = bin(a ^ b).count("1")
    return "match" if distance <= 12 else "mismatch"


def _dhash(data: bytes, size: int = 8) -> int | None:
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data)).convert("L").resize((size + 1, size))
    except Exception:
        return None
    pixels = list(img.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = pixels[row * (size + 1) + col]
            right = pixels[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


# -- CSV report / apply ---------------------------------------------------


def write_report(discrepancies: list[Discrepancy], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for d in discrepancies:
            row = asdict(d)
            row["approve"] = ""
            writer.writerow(row)


def summarize(discrepancies: list[Discrepancy]) -> str:
    by_field: dict[str, int] = {}
    editions: set[int] = set()
    for d in discrepancies:
        by_field[d.field] = by_field.get(d.field, 0) + 1
        editions.add(d.edition_id)
    lines = [f"{len(discrepancies)} discrepancies across {len(editions)} editions:"]
    for field, n in sorted(by_field.items(), key=lambda x: -x[1]):
        lines.append(f"  {field}: {n}")
    return "\n".join(lines)


def auto_fix_discrepancies(
    client: HardcoverClient, discrepancies: list[Discrepancy]
) -> dict:
    """Automatically apply the mechanical fixes; surface the judgment calls.

    Used by the daemon's reconcile audit. All deterministic fixes are applied
    with verify+retry: format, date, language, ISBN/ASIN, and the cover — whether
    it's missing or differs from the SE cover, since for a Standard Ebooks
    edition the SE cover is authoritative and re-applying it is self-resolving.
    Only an edition that matches no SE catalog book (a possible mis-attribution)
    is returned for a human, never auto-changed.
    """
    per_edition: dict[int, list[Discrepancy]] = {}
    for d in discrepancies:
        per_edition.setdefault(d.edition_id, []).append(d)

    result = {"fixed": 0, "errors": 0, "covers_fixed": 0, "mis_attributed": []}
    for edition_id, discs in per_edition.items():
        dto: dict = {}
        for d in discs:
            if d.field == "book_match":
                result["mis_attributed"].append(d)
            elif d.field == "cover":  # missing OR mismatched — SE cover wins
                try:
                    dto["image_id"] = client.insert_image(edition_id, d.proposed, "Edition")
                    result["covers_fixed"] += 1
                except Exception as exc:
                    logger.warning("insert_image failed for %d: %s", edition_id, exc)
            elif d.field == "reading_format":
                dto["reading_format_id"] = int(d.proposed)
            elif d.field == "language":
                dto["language_id"] = int(d.proposed)
            elif d.field == "release_date":
                dto["release_date"] = d.proposed
            elif d.field == "isbn":
                dto["isbn_10"] = ""
                dto["isbn_13"] = ""
            elif d.field == "asin":
                dto["asin"] = ""
        if dto:
            try:
                if update_edition_verified(client, edition_id, dto):
                    result["fixed"] += 1
                else:
                    result["errors"] += 1
            except Exception as exc:
                result["errors"] += 1
                logger.error("Auto-fix failed for edition %d: %s", edition_id, exc)
    return result


_APPROVE_VALUES = {"y", "yes", "true", "1", "x", "approve"}


def apply_fixes(client: HardcoverClient, csv_path: Path) -> dict[str, int]:
    """Execute approved rows from an audit CSV. Groups by edition."""
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh)
                if (r.get("approve") or "").strip().lower() in _APPROVE_VALUES]

    # Group approved rows per edition so multiple field fixes become one update.
    per_edition: dict[int, list[dict]] = {}
    for r in rows:
        per_edition.setdefault(int(r["edition_id"]), []).append(r)

    summary = {"editions": 0, "updated": 0, "covers": 0, "cleared": 0,
               "skipped_manual": 0, "errors": 0}
    for edition_id, edits in per_edition.items():
        summary["editions"] += 1
        dto: dict = {}
        cover_url = ""
        for r in edits:
            field = r["field"]
            proposed = r["proposed"]
            # Dispatch by field so the action is independent of the row's stored
            # fix_kind (older CSVs marked isbn/asin as "manual").
            if field == "cover":
                cover_url = proposed
            elif field in ("reading_format", "language"):
                dto[{"reading_format": "reading_format_id",
                     "language": "language_id"}[field]] = int(proposed)
            elif field == "release_date":
                dto["release_date"] = proposed
            elif field == "isbn":
                dto["isbn_10"] = ""
                dto["isbn_13"] = ""
                summary["cleared"] += 1
            elif field == "asin":
                dto["asin"] = ""
                summary["cleared"] += 1
            else:  # book_match — needs a human, no safe automatic action
                summary["skipped_manual"] += 1
        try:
            # Fold the cover link into the single verified update, so the whole
            # edition's fixes persist together (or get retried together).
            if cover_url:
                image_id = client.insert_image(edition_id, cover_url, "Edition")
                dto["image_id"] = image_id
                summary["covers"] += 1
                logger.info("Uploaded cover for edition %d (image %d)", edition_id, image_id)
            if dto:
                if update_edition_verified(client, edition_id, dto):
                    summary["updated"] += 1
                    logger.info("Updated edition %d: %s", edition_id, dto)
                else:
                    summary["errors"] += 1
                    logger.error("Edition %d update never persisted: %s", edition_id, dto)
        except Exception as exc:
            summary["errors"] += 1
            logger.error("Failed to apply fixes to edition %d: %s", edition_id, exc)

    return summary
