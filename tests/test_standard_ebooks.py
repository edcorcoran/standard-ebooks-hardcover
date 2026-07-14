from se_hardcover import standard_ebooks as se
from tests.conftest import read_fixture


def test_url_to_repo_roundtrip():
    url = "https://standardebooks.org/ebooks/honore-de-balzac/modeste-mignon/clara-bell"
    assert se.url_to_repo(url) == "honore-de-balzac_modeste-mignon_clara-bell"
    assert se.repo_to_url("honore-de-balzac_modeste-mignon_clara-bell") == url


def test_long_repo_name_truncated_to_github_limit():
    # GitHub caps repo names at 100 chars; the SE cover path keeps the full slug.
    url = ("https://standardebooks.org/ebooks/hans-jakob-christoffel-von-grimmelshausen"
           "/the-adventurous-simplicissimus/alfred-thomas-scrope-goodrick")
    slug = se.url_to_slug(url)
    repo = se.url_to_repo(url)
    assert len(slug) == 102
    assert len(repo) == 100
    assert slug.startswith(repo)


def test_parse_translated_book():
    book = se.parse_content_opf(read_fixture("karamazov.opf"))
    assert book.title == "The Brothers Karamazov"
    assert book.subtitle == ""
    assert book.se_url.endswith("/the-brothers-karamazov/constance-garnett")
    assert book.repo == "fyodor-dostoevsky_the-brothers-karamazov_constance-garnett"
    assert book.release_date == "2019-02-05"
    assert book.author_names == ["Fyodor Dostoevsky"]
    trl = [c.name for c in book.translators]
    assert trl == ["Constance Garnett"]
    # Producers / transcribers / cover artists are not kept.
    kept_roles = {c.role for c in book.contributors}
    assert kept_roles <= {"aut", "trl", "ill", "edt"}
    assert book.word_count == 349853
    assert "Dmitri Karamazov" in book.description


def test_parse_single_author_book():
    book = se.parse_content_opf(read_fixture("pride-and-prejudice.opf"))
    assert book.title == "Pride and Prejudice"
    assert book.author_names == ["Jane Austen"]
    assert not book.translators


def test_parse_subtitle_book():
    book = se.parse_content_opf(read_fixture("souls-of-black-folk.opf"))
    assert book.title == "The Souls of Black Folk"
    assert book.subtitle == "Essays and Sketches"


def test_extraction_handles_underscore_multi_contributor_urls():
    # A book with multiple translators joins them with '_' in the last segment.
    html = (
        '<a href="/ebooks/leo-tolstoy/war-and-peace/louise-maude_aylmer-maude">x</a>'
        '<a href="/ebooks/jane-austen/pride-and-prejudice">y</a>'
    )
    urls = se._extract_book_urls(html)
    assert "https://standardebooks.org/ebooks/leo-tolstoy/war-and-peace/louise-maude_aylmer-maude" in urls
    assert "https://standardebooks.org/ebooks/jane-austen/pride-and-prejudice" in urls


def test_listing_extraction():
    urls = se._extract_book_urls(read_fixture("listing-page.html"))
    assert len(urls) > 20
    assert all(u.startswith("https://standardebooks.org/ebooks/") for u in urls)
    # Every URL must have at least author + slug (>= 2 path segments after /ebooks/).
    for u in urls:
        segments = u.split("/ebooks/", 1)[1].split("/")
        assert len(segments) >= 2
    # No author-only or pagination links leaked in.
    assert not any(u.rstrip("/").endswith("/ebooks") for u in urls)


def test_cover_url_extraction():
    repo = "fyodor-dostoevsky_the-brothers-karamazov_constance-garnett"
    cover = se.cover_url_for(repo, read_fixture("karamazov-book-page.html"))
    assert cover.startswith("https://standardebooks.org/images/covers/" + repo)
    assert cover.endswith("cover@2x.jpg")


def test_cover_url_missing_returns_empty():
    assert se.cover_url_for("nonexistent_repo", "<html></html>") == ""


def test_atom_feed_parsing():
    entries = se.parse_new_releases(read_fixture("new-releases.atom"))
    assert len(entries) >= 10
    for e in entries:
        assert "/ebooks/" in e.se_url
        assert e.title
