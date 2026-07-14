"""Build and refresh the local Standard Ebooks catalog cache."""

from __future__ import annotations

import logging
import time

import httpx

from . import standard_ebooks as se
from .standard_ebooks import _client
from .state import Store

logger = logging.getLogger(__name__)


def build_catalog(
    store: Store,
    *,
    refresh: bool = False,
    limit: int | None = None,
    polite_delay: float = 0.5,
) -> dict[str, int]:
    """Scrape the listing and fetch metadata for each book into the cache.

    Resumable: books already cached are skipped unless ``refresh`` is set.
    Returns a summary dict with counts.
    """
    client = _client()
    summary = {"listed": 0, "fetched": 0, "skipped": 0, "errors": 0}
    try:
        urls = se.iter_catalog_urls(client, polite_delay=polite_delay)
        summary["listed"] = len(urls)
        logger.info("Catalog listing: %d books", len(urls))

        if limit:
            urls = urls[:limit]

        for i, url in enumerate(urls, 1):
            if not refresh and store.has_book(url):
                summary["skipped"] += 1
                continue
            try:
                book = se.fetch_book(url, client)
                store.upsert_book(book)
                summary["fetched"] += 1
                if i % 25 == 0:
                    logger.info(
                        "Fetched %d/%d (%s)", i, len(urls), book.title[:40]
                    )
            except httpx.HTTPError as exc:
                summary["errors"] += 1
                logger.warning("Failed to fetch %s: %s", url, exc)
            time.sleep(polite_delay)
    finally:
        client.close()

    logger.info("Catalog build complete: %s", summary)
    return summary
