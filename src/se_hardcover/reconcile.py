"""Reconcile Hardcover with the Standard Ebooks catalog.

The automated audit the daemon runs on a cadence. It answers two questions:

1. **Does every Standard Ebooks book have a Hardcover edition?** (coverage) — it
   refreshes the catalog, then adds an SE edition for any catalogued book that
   doesn't have one yet (confident matches auto-added, the rest queued).
2. **Is the data on the existing SE editions accurate, and do they all
   correspond to real SE books?** (integrity) — it audits every edition under the
   publisher, auto-fixes the mechanical problems, and flags anything needing a
   human (a swapped cover, or an edition that matches no SE book).

A Discord summary is sent only when something actually happened, so a healthy
catalog stays quiet.
"""

from __future__ import annotations

import logging

from .audit import audit_editions, auto_fix_discrepancies
from .catalog import build_catalog
from .hardcover import HardcoverClient
from .notify import Notifier
from .state import Store
from .sync import Outcome, RefData, sync_book

logger = logging.getLogger(__name__)


def reconcile(
    client: HardcoverClient,
    store: Store,
    ref: RefData,
    notifier: Notifier,
    *,
    check_covers: bool = True,
    refresh_catalog: bool = True,
) -> dict:
    """Run one full reconcile pass. Returns a summary dict."""
    summary: dict = {
        "catalog_new": 0,
        "coverage_added": 0,
        "coverage_created": 0,
        "coverage_queued": 0,
        "coverage_errors": 0,
        "pending_review": 0,
        "data_fixed": 0,
        "covers_fixed": 0,
        "mis_attributed": 0,
    }

    # 1. Discover any newly published SE books (belt-and-suspenders with the feed).
    if refresh_catalog:
        cat = build_catalog(store, refresh=False)
        summary["catalog_new"] = cat["fetched"]

    # 2. Coverage: ensure every catalogued book has an SE edition. Only touch
    #    books never attempted or previously errored — queued items await a human
    #    and already-done items are skipped, so this never re-notifies them.
    existing = client.se_edition_book_ids(ref.publisher_id)
    for book in store.all_books():
        status = store.processed_status(book.se_url)
        if status not in (None, "error"):
            continue
        result = sync_book(book, client, store, ref, notifier, existing_book_ids=existing)
        if result.outcome == Outcome.ADDED:
            existing.add(result.book_id)
            summary["coverage_added"] += 1
        elif result.outcome == Outcome.CREATED:
            existing.add(result.book_id)
            summary["coverage_created"] += 1
        elif result.outcome == Outcome.QUEUED:
            summary["coverage_queued"] += 1
        elif result.outcome == Outcome.ERROR:
            summary["coverage_errors"] += 1
    summary["pending_review"] = len(store.pending_reviews())

    # 3. Integrity: audit existing editions, auto-fix the mechanical issues.
    discrepancies = audit_editions(client, store, ref, check_covers=check_covers)
    triage = auto_fix_discrepancies(client, discrepancies)
    summary["data_fixed"] = triage["fixed"]
    summary["covers_fixed"] = triage["covers_fixed"]
    summary["mis_attributed"] = len(triage["mis_attributed"])

    _notify(notifier, summary, triage)
    logger.info("Reconcile complete: %s", summary)
    return summary


def _notify(notifier: Notifier, summary: dict, triage: dict) -> None:
    """Send a Discord summary, but only when something noteworthy happened."""
    noteworthy = any(
        summary[k]
        for k in ("catalog_new", "coverage_added", "coverage_created", "coverage_queued",
                  "coverage_errors", "data_fixed", "covers_fixed", "mis_attributed")
    )
    if not noteworthy:
        return

    lines = ["🔁 **Standard Ebooks reconcile**"]
    if summary["catalog_new"]:
        lines.append(f"• {summary['catalog_new']} new SE book(s) discovered")
    if summary["coverage_added"]:
        lines.append(f"• {summary['coverage_added']} edition(s) added for missing books")
    if summary["coverage_created"]:
        lines.append(f"• {summary['coverage_created']} new Hardcover book(s) created")
    if summary["coverage_queued"]:
        lines.append(f"• {summary['coverage_queued']} book(s) queued for review")
    if summary["data_fixed"]:
        lines.append(f"• {summary['data_fixed']} edition(s) auto-corrected")
    if summary["covers_fixed"]:
        lines.append(f"• {summary['covers_fixed']} cover(s) reset to the SE cover")
    if summary["coverage_errors"]:
        lines.append(f"• ⚠️ {summary['coverage_errors']} coverage error(s)")
    if summary["mis_attributed"]:
        lines.append(
            f"• ⚠️ {summary['mis_attributed']} edition(s) match no SE book "
            f"(possible mis-attribution) — needs review:"
        )
        for d in triage["mis_attributed"][:10]:
            lines.append(f"    {d.hardcover_url} ({d.current})")
    notifier.send("\n".join(lines))
