"""Standard Ebooks data sources: catalog listing, content.opf, cover URL, Atom feed.

All sources are public. We derive per-book metadata from each book's
``content.opf`` on GitHub (the canonical source) and the cover URL from the
book page, and we drive the daemon from the public new-releases Atom feed.
"""

from __future__ import annotations

import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import feedparser
import httpx

from . import USER_AGENT
from .models import Contributor, SeBook

logger = logging.getLogger(__name__)

SITE = "https://standardebooks.org"
RAW = "https://raw.githubusercontent.com/standardebooks"
ATOM_NEW_RELEASES = f"{SITE}/feeds/atom/new-releases"

# MARC relator roles we care about; everything else (producers, transcribers,
# cover artists) is ignored for Hardcover purposes.
_ROLE_KEEP = {"aut": "aut", "trl": "trl", "ill": "ill", "edt": "edt"}


def _client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        headers={"user-agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    )


# -- URL / repo helpers ---------------------------------------------------


# GitHub truncates repository names to this many characters.
GITHUB_REPO_NAME_MAX = 100


def url_to_slug(se_url: str) -> str:
    """https://standardebooks.org/ebooks/a/b/c -> 'a_b_c' (full, untruncated).

    This is the identifier Standard Ebooks uses in cover image paths.
    """
    path = se_url.split("/ebooks/", 1)[-1].strip("/")
    return path.replace("/", "_")


def url_to_repo(se_url: str) -> str:
    """Full slug truncated to GitHub's 100-char repo-name limit.

    GitHub caps repo names at 100 chars, so Standard Ebooks' longest slugs are
    truncated there (e.g. a 102-char name loses its last 2 chars). The cover
    path on standardebooks.org keeps the full slug — use :func:`url_to_slug`
    for covers, this for the GitHub ``content.opf``.
    """
    return url_to_slug(se_url)[:GITHUB_REPO_NAME_MAX]


def repo_to_url(repo: str) -> str:
    return f"{SITE}/ebooks/" + repo.replace("_", "/")


def content_opf_url(repo: str) -> str:
    return f"{RAW}/{repo}/master/src/epub/content.opf"


# -- catalog listing ------------------------------------------------------


def iter_catalog_urls(
    client: httpx.Client | None = None, *, polite_delay: float = 0.5
) -> list[str]:
    """Scrape every book URL from the paginated public listing.

    Walks ``/ebooks?page=N`` until a page yields no new book links.
    """
    owns = client is None
    client = client or _client()
    seen: list[str] = []
    seen_set: set[str] = set()
    try:
        page = 1
        while True:
            resp = client.get(f"{SITE}/ebooks", params={"page": page, "per-page": 48})
            resp.raise_for_status()
            urls = _extract_book_urls(resp.text)
            fresh = [u for u in urls if u not in seen_set]
            if not fresh:
                break
            for u in fresh:
                seen_set.add(u)
                seen.append(u)
            logger.info("Listing page %d: +%d books (%d total)", page, len(fresh), len(seen))
            page += 1
            time.sleep(polite_delay)
    finally:
        if owns:
            client.close()
    return seen


# Segments may contain underscores: a book with multiple contributors joins them
# with '_' in the last path segment, e.g. .../war-and-peace/louise-maude_aylmer-maude.
_BOOK_HREF = re.compile(r'href="(/ebooks/[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*){1,3})"')


def _extract_book_urls(page_html: str) -> list[str]:
    """Extract distinct book paths (2-4 URL segments) from a listing page."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _BOOK_HREF.finditer(page_html):
        path = m.group(1)
        # A book path has at least author + slug; author-only pages are excluded
        # by the {1,3} quantifier requiring >=1 extra segment.
        if path in seen:
            continue
        seen.add(path)
        out.append(SITE + path)
    return out


# -- content.opf parsing --------------------------------------------------


def _localname(tag: str) -> str:
    return tag.rpartition("}")[2]


def parse_content_opf(xml_text: str, *, repo: str = "", cover_url: str = "") -> SeBook:
    """Parse a Standard Ebooks ``content.opf`` into an :class:`SeBook`."""
    root = ET.fromstring(xml_text)
    metadata = next((c for c in root.iter() if _localname(c.tag) == "metadata"), None)
    if metadata is None:
        raise ValueError("content.opf has no <metadata> element")

    # First pass: index refinements by the id they refine (#foo -> {property: value}).
    file_as: dict[str, str] = {}
    roles: dict[str, list[str]] = {}
    title_types: dict[str, str] = {}
    for el in metadata:
        if _localname(el.tag) != "meta":
            continue
        prop = el.get("property")
        refines = (el.get("refines") or "").lstrip("#")
        text = (el.text or "").strip()
        if not refines:
            continue
        if prop == "file-as":
            file_as[refines] = text
        elif prop == "role":
            roles.setdefault(refines, []).append(text)
        elif prop == "title-type":
            title_types[refines] = text

    title = ""
    subtitle = ""
    contributors: list[Contributor] = []
    subjects: list[str] = []
    se_url = ""
    release_date = ""
    short_description = ""
    long_description = ""
    word_count: int | None = None

    for el in metadata:
        name = _localname(el.tag)
        prop = el.get("property")
        el_id = el.get("id") or ""
        text = (el.text or "").strip()

        if name == "identifier" and text.startswith("http"):
            se_url = text
        elif name == "title":
            ttype = title_types.get(el_id, "main")
            if ttype == "subtitle":
                subtitle = text
            elif not title or ttype == "main":
                title = text
        elif name == "date" and not release_date:
            release_date = text[:10]  # ISO date part
        elif name == "subject":
            subjects.append(text)
        elif name == "description":
            short_description = text
        elif name == "creator":
            contributors.append(_contributor(el_id, text, "aut", file_as, roles))
        elif name == "contributor":
            role = _primary_role(roles.get(el_id, []))
            if role in _ROLE_KEEP:
                contributors.append(_contributor(el_id, text, role, file_as, roles))
        elif name == "meta" and prop == "se:long-description":
            long_description = html.unescape(text).strip()
        elif name == "meta" and prop == "schema:wordCount" and text.isdigit():
            word_count = int(text)

    if not repo and se_url:
        repo = url_to_repo(se_url)

    return SeBook(
        se_url=se_url,
        repo=repo,
        title=title,
        subtitle=subtitle,
        contributors=contributors,
        description=long_description,
        short_description=short_description,
        subjects=subjects,
        release_date=release_date,
        cover_url=cover_url,
        word_count=word_count,
    )


def _contributor(
    el_id: str,
    name: str,
    default_role: str,
    file_as: dict[str, str],
    roles: dict[str, list[str]],
) -> Contributor:
    role = default_role
    if el_id in roles:
        role = _primary_role(roles[el_id]) or default_role
    return Contributor(name=name, file_as=file_as.get(el_id, ""), role=role)


def _primary_role(role_list: list[str]) -> str:
    """Pick the most meaningful role from a contributor's MARC relators."""
    for preferred in ("aut", "trl", "edt", "ill"):
        if preferred in role_list:
            return preferred
    return role_list[0] if role_list else ""


# -- cover URL ------------------------------------------------------------

_COVER_RE_TMPL = r"/images/covers/{slug}/([a-f0-9]{{8,}})/cover(?:@2x)?\.jpg"


def cover_url_for(slug: str, page_html: str) -> str:
    """Find the book's own full-size cover (cover@2x.jpg) on its book page.

    ``slug`` is the full (untruncated) identifier — see :func:`url_to_slug` —
    which is what standardebooks.org uses in cover paths. Matching on it ignores
    the sidebar's related-book thumbnails; we then prefer the @2x JPG variant.
    """
    m = re.search(_COVER_RE_TMPL.format(slug=re.escape(slug)), page_html)
    if not m:
        return ""
    sha = m.group(1)
    return f"{SITE}/images/covers/{slug}/{sha}/cover@2x.jpg"


def fetch_book(se_url: str, client: httpx.Client | None = None) -> SeBook:
    """Fetch content.opf + cover URL for a single book URL."""
    owns = client is None
    client = client or _client()
    try:
        repo = url_to_repo(se_url)
        opf = client.get(content_opf_url(repo))
        opf.raise_for_status()
        page = client.get(se_url)
        page.raise_for_status()
        cover = cover_url_for(url_to_slug(se_url), page.text)
        book = parse_content_opf(opf.text, repo=repo, cover_url=cover)
        if not book.se_url:
            book.se_url = se_url
        return book
    finally:
        if owns:
            client.close()


# -- Atom feed ------------------------------------------------------------


@dataclass
class FeedEntry:
    se_url: str
    title: str
    updated: str


def parse_new_releases(atom_text: str) -> list[FeedEntry]:
    """Parse the new-releases Atom feed into (se_url, title, updated) entries."""
    parsed = feedparser.parse(atom_text)
    entries: list[FeedEntry] = []
    for e in parsed.entries:
        se_url = getattr(e, "id", "") or getattr(e, "link", "")
        # Entry ids are the canonical ebook URLs; normalize to the /ebooks/ form.
        if "/ebooks/" not in se_url:
            link = getattr(e, "link", "")
            se_url = link if "/ebooks/" in link else se_url
        entries.append(
            FeedEntry(
                se_url=se_url,
                title=getattr(e, "title", ""),
                updated=getattr(e, "updated", "") or getattr(e, "published", ""),
            )
        )
    return entries


def fetch_new_releases(client: httpx.Client | None = None) -> list[FeedEntry]:
    owns = client is None
    client = client or _client()
    try:
        resp = client.get(ATOM_NEW_RELEASES)
        resp.raise_for_status()
        return parse_new_releases(resp.text)
    finally:
        if owns:
            client.close()


def cover_bytes(cover_url: str, client: httpx.Client | None = None) -> bytes:
    """Download raw cover image bytes (used by the audit's image comparison)."""
    owns = client is None
    client = client or _client()
    try:
        resp = client.get(cover_url)
        resp.raise_for_status()
        return resp.content
    finally:
        if owns:
            client.close()
