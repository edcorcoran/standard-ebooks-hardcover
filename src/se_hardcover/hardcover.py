"""Hardcover GraphQL client.

Wraps the single GraphQL endpoint with:
- a client-side throttle (Hardcover allows 60 req/min; we stay at ~1 req/sec),
- retry with backoff on 429 / 5xx / transport errors,
- a ``dry_run`` guard that refuses to send mutations,
- typed helpers for the queries and mutations this project needs.

The API is beta; field availability can drift. Helpers keep GraphQL documents
inline and close to their callers so they are easy to adjust.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from . import USER_AGENT
from .models import (
    Contributor,
    HardcoverBookMatch,
    HardcoverContribution,
    HardcoverEdition,
)

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.hardcover.app/v1/graphql"
MIN_INTERVAL = 1.05  # seconds between requests (~57/min, under the 60/min cap)
MAX_RETRIES = 5


class HardcoverError(RuntimeError):
    """A GraphQL-level or HTTP-level error from Hardcover."""


class HardcoverAuthError(HardcoverError):
    """401 / token problem — surfaced separately so the daemon can alert."""


class DryRunMutation(Exception):
    """Raised internally to short-circuit a mutation in dry-run mode."""


class HardcoverClient:
    def __init__(
        self,
        token: str,
        *,
        dry_run: bool = False,
        endpoint: str = ENDPOINT,
        min_interval: float = MIN_INTERVAL,
        timeout: float = 35.0,
    ) -> None:
        if not token:
            raise HardcoverAuthError("Hardcover API token is required.")
        self.dry_run = dry_run
        self._min_interval = min_interval
        self._last_call = 0.0
        # Hardcover accepts the raw token in the `authorization` header. It also
        # accepts a "Bearer " prefix; we add it if the user did not.
        auth = token if token.lower().startswith("bearer ") else f"Bearer {token}"
        self._client = httpx.Client(
            base_url=endpoint,
            headers={
                "authorization": auth,
                "content-type": "application/json",
                "user-agent": USER_AGENT,
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HardcoverClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- core transport ----------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def execute(
        self, query: str, variables: dict[str, Any] | None = None, *, is_mutation: bool = False
    ) -> dict[str, Any]:
        """Run a GraphQL document and return its ``data`` payload.

        Mutations are blocked when ``dry_run`` is set.
        """
        if is_mutation and self.dry_run:
            raise DryRunMutation()

        payload = {"query": query, "variables": variables or {}}
        backoff = 2.0
        last_exc: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()
            try:
                resp = self._client.post("", json=payload)
            except httpx.TransportError as exc:  # network hiccup
                last_exc = exc
                logger.warning("Transport error (attempt %d): %s", attempt, exc)
                time.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code == 401:
                raise HardcoverAuthError("401 Unauthorized — token expired or invalid.")
            # 408 (request timeout) and 429/5xx are transient — back off and retry.
            if resp.status_code in (408, 429) or resp.status_code >= 500:
                retry_after = float(resp.headers.get("retry-after", backoff))
                logger.warning(
                    "HTTP %d from Hardcover (attempt %d); sleeping %.1fs",
                    resp.status_code,
                    attempt,
                    retry_after,
                )
                time.sleep(retry_after)
                backoff *= 2
                continue
            if resp.status_code != 200:
                raise HardcoverError(f"HTTP {resp.status_code}: {resp.text[:300]}")

            body = resp.json()
            if body.get("errors"):
                msg = "; ".join(e.get("message", str(e)) for e in body["errors"])
                # Rate-limit errors sometimes arrive as 200 + errors.
                if "throttl" in msg.lower():
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise HardcoverError(msg)
            return body.get("data", {})

        raise HardcoverError(
            f"Exhausted {MAX_RETRIES} retries. Last error: {last_exc}"
        )

    # -- reference-data lookups -------------------------------------------

    def whoami(self) -> dict[str, Any]:
        data = self.execute("query { me { id username } }")
        me = data.get("me")
        # `me` may come back as a list depending on schema version.
        if isinstance(me, list):
            me = me[0] if me else None
        if not me:
            raise HardcoverAuthError("Token accepted but no user returned.")
        return me

    def publisher_id(self, slug: str = "standard-ebooks") -> int | None:
        data = self.execute(
            "query ($slug: String!) { publishers(where: {slug: {_eq: $slug}}) { id name slug } }",
            {"slug": slug},
        )
        rows = data.get("publishers", [])
        return rows[0]["id"] if rows else None

    def reading_formats(self) -> dict[str, int]:
        data = self.execute("query { reading_formats { id format } }")
        return {r["format"].lower(): r["id"] for r in data.get("reading_formats", [])}

    def languages_by_code(self, codes: tuple[str, ...] = ("en", "eng")) -> dict[str, int]:
        """Return a {code/label: id} map for the given language codes.

        The languages table exposes ``code2``/``code3``/``language`` columns;
        we fetch English and map every identifier we find to its id.
        """
        data = self.execute(
            """
            query ($codes: [String!]) {
              languages(where: {_or: [
                {code2: {_in: $codes}}, {code3: {_in: $codes}}
              ]}) { id language code2 code3 }
            }
            """,
            {"codes": list(codes)},
        )
        out: dict[str, int] = {}
        for row in data.get("languages", []):
            for key in ("code2", "code3", "language"):
                if row.get(key):
                    out[row[key].lower()] = row["id"]
        return out

    # -- search / lookup ---------------------------------------------------

    def search_books(self, query: str, per_page: int = 10) -> list[HardcoverBookMatch]:
        """Fuzzy book search via Hardcover's Typesense-backed ``search`` query."""
        data = self.execute(
            """
            query ($q: String!, $per: Int!) {
              search(query: $q, query_type: "Book", per_page: $per) {
                results
              }
            }
            """,
            {"q": query, "per": per_page},
        )
        results = (data.get("search") or {}).get("results") or {}
        hits = results.get("hits", []) if isinstance(results, dict) else []
        out: list[HardcoverBookMatch] = []
        for hit in hits:
            doc = hit.get("document", hit)
            out.append(_book_match_from_search_doc(doc))
        return out

    def editions_for_publisher(self, publisher_id: int) -> list[HardcoverEdition]:
        """All editions attributed to a publisher, with book + image + contributions."""
        data = self.execute(
            """
            query ($pid: Int!) {
              editions(where: {publisher_id: {_eq: $pid}}, order_by: {id: asc}) {
                id book_id title subtitle release_date
                reading_format_id language_id publisher_id
                isbn_10 isbn_13 asin image_id
                edition_format edition_information
                reading_format { format }
                image { url }
                book { id title slug contributions { author { name } } }
                contributions { contribution author { name } }
              }
            }
            """,
            {"pid": publisher_id},
        )
        return [_edition_from_row(r) for r in data.get("editions", [])]

    def edition_fields(self, edition_id: int) -> dict[str, Any] | None:
        """Fetch the few fields used to verify a write actually persisted."""
        data = self.execute(
            """
            query ($id: Int!) {
              editions(where: {id: {_eq: $id}}) {
                id publisher_id language_id release_date reading_format_id image_id
                isbn_10 isbn_13 asin
              }
            }
            """,
            {"id": edition_id},
        )
        rows = data.get("editions", [])
        return rows[0] if rows else None

    def book_by_id(self, book_id: int) -> dict[str, Any] | None:
        data = self.execute(
            """
            query ($id: Int!) {
              books(where: {id: {_eq: $id}}) {
                id title slug subtitle release_year editions_count
                contributions { contribution author { name } }
              }
            }
            """,
            {"id": book_id},
        )
        rows = data.get("books", [])
        return rows[0] if rows else None

    def books_with_covers(self, book_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Batch-fetch {id: {slug, title, image_url}} for review-UI candidates."""
        if not book_ids:
            return {}
        data = self.execute(
            """
            query ($ids: [Int!]) {
              books(where: {id: {_in: $ids}}) {
                id slug title
                image { url }
              }
            }
            """,
            {"ids": book_ids},
        )
        return {
            b["id"]: {
                "slug": b.get("slug"),
                "title": b.get("title"),
                "image_url": (b.get("image") or {}).get("url"),
            }
            for b in data.get("books", [])
        }

    def se_edition_book_ids(self, publisher_id: int) -> set[int]:
        """book_ids that already have an edition under this publisher."""
        data = self.execute(
            "query ($pid: Int!) { editions(where: {publisher_id: {_eq: $pid}}) { book_id } }",
            {"pid": publisher_id},
        )
        return {r["book_id"] for r in data.get("editions", [])}

    # -- mutations ---------------------------------------------------------

    def insert_edition(self, book_id: int, dto: dict[str, Any]) -> dict[str, Any]:
        """Add an edition to an existing book. Returns {id, warnings}."""
        data = self.execute(
            """
            mutation ($book_id: Int!, $edition: EditionInput!) {
              insert_edition(book_id: $book_id, edition: $edition) {
                id errors
                edition { id }
              }
            }
            """,
            {"book_id": book_id, "edition": {"book_id": book_id, "dto": dto}},
            is_mutation=True,
        )
        result = data.get("insert_edition") or {}
        if result.get("errors"):
            raise HardcoverError(f"insert_edition: {result['errors']}")
        return result

    def update_edition(self, edition_id: int, dto: dict[str, Any]) -> dict[str, Any]:
        data = self.execute(
            """
            mutation ($id: Int!, $edition: EditionInput!) {
              update_edition(id: $id, edition: $edition) { id errors }
            }
            """,
            {"id": edition_id, "edition": {"dto": dto}},
            is_mutation=True,
        )
        result = data.get("update_edition") or {}
        if result.get("errors"):
            raise HardcoverError(f"update_edition: {result['errors']}")
        return result

    def insert_book_with_edition(self, dto: dict[str, Any]) -> dict[str, Any]:
        """Create a new book together with its first edition (review-queue path only)."""
        data = self.execute(
            """
            mutation ($edition: EditionInput!) {
              insert_book(edition: $edition) { id errors edition { id book_id } }
            }
            """,
            {"edition": {"dto": dto}},
            is_mutation=True,
        )
        result = data.get("insert_book") or {}
        if result.get("errors"):
            raise HardcoverError(f"insert_book: {result['errors']}")
        return result

    def insert_image(self, imageable_id: int, url: str, imageable_type: str = "Edition") -> int:
        """Attach an image to an edition/book. Hardcover fetches and rehosts ``url``."""
        data = self.execute(
            """
            mutation ($image: ImageInput!) {
              insert_image(image: $image) { id }
            }
            """,
            {
                "image": {
                    "imageable_id": imageable_id,
                    "imageable_type": imageable_type,
                    "url": url,
                }
            },
            is_mutation=True,
        )
        result = data.get("insert_image") or {}
        image_id = result.get("id")
        if not image_id:
            raise HardcoverError(f"insert_image returned no id: {result}")
        return image_id


# -- row/doc adapters -----------------------------------------------------


def _edition_from_row(r: dict[str, Any]) -> HardcoverEdition:
    contribs = [
        HardcoverContribution(
            author_name=(c.get("author") or {}).get("name", ""),
            contribution=c.get("contribution"),
        )
        for c in (r.get("contributions") or [])
    ]
    book = r.get("book") or {}
    book_authors = [
        (c.get("author") or {}).get("name", "")
        for c in (book.get("contributions") or [])
    ]
    return HardcoverEdition(
        id=r["id"],
        book_id=r["book_id"],
        book_title=book.get("title", ""),
        book_slug=book.get("slug", ""),
        book_author_names=[a for a in book_authors if a],
        title=r.get("title"),
        subtitle=r.get("subtitle"),
        release_date=r.get("release_date"),
        reading_format_id=r.get("reading_format_id"),
        reading_format=(r.get("reading_format") or {}).get("format"),
        language_id=r.get("language_id"),
        publisher_id=r.get("publisher_id"),
        isbn_10=r.get("isbn_10"),
        isbn_13=r.get("isbn_13"),
        asin=r.get("asin"),
        image_id=r.get("image_id"),
        image_url=(r.get("image") or {}).get("url"),
        edition_format=r.get("edition_format"),
        edition_information=r.get("edition_information"),
        contributions=contribs,
    )


def _book_match_from_search_doc(doc: dict[str, Any]) -> HardcoverBookMatch:
    """Adapt a Typesense search document into a HardcoverBookMatch.

    Hardcover's search documents expose author names under a few possible keys
    depending on the index version; we try the common ones.
    """
    authors: list[str] = []
    for key in ("author_names", "authors", "contributions"):
        val = doc.get(key)
        if isinstance(val, list) and val:
            if isinstance(val[0], str):
                authors = val
            elif isinstance(val[0], dict):
                authors = [a.get("name") or a.get("author") or "" for a in val]
            break
    release_year = doc.get("release_year")
    if isinstance(release_year, str) and release_year.isdigit():
        release_year = int(release_year)
    return HardcoverBookMatch(
        id=int(doc.get("id")),
        title=doc.get("title", ""),
        slug=doc.get("slug", ""),
        subtitle=doc.get("subtitle"),
        author_names=[a for a in authors if a],
        release_year=release_year if isinstance(release_year, int) else None,
        editions_count=doc.get("editions_count"),
        users_count=int(doc.get("users_count") or 0),
    )


def se_contributions_to_dto(
    contributors: list[Contributor], resolve_author_id
) -> list[dict[str, Any]]:
    """Build ContributionInputType entries, resolving names to Hardcover author ids.

    ``resolve_author_id`` is a callable(name) -> int | None. Contributors that
    cannot be resolved are dropped (the book's own contributions still stand).
    MARC roles are mapped to Hardcover contribution labels.
    """
    role_labels = {"aut": None, "trl": "Translator", "ill": "Illustrator", "edt": "Editor"}
    out: list[dict[str, Any]] = []
    for c in contributors:
        author_id = resolve_author_id(c.name)
        if not author_id:
            continue
        entry: dict[str, Any] = {"author_id": author_id}
        label = role_labels.get(c.role, None)
        if label:
            entry["contribution"] = label
        out.append(entry)
    return out
