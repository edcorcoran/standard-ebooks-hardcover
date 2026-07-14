from se_hardcover.models import Contributor, SeBook
from se_hardcover.state import Store


def _book(se_url="https://standardebooks.org/ebooks/a/b", title="Book B"):
    return SeBook(
        se_url=se_url,
        repo="a_b",
        title=title,
        subtitle="Sub",
        contributors=[Contributor(name="Jane Doe", file_as="Doe, Jane", role="aut")],
        subjects=["Fiction"],
        release_date="2020-01-01",
        cover_url="https://example.com/cover.jpg",
        word_count=1000,
    )


def test_catalog_roundtrip(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        store.upsert_book(_book())
        got = store.get_book("https://standardebooks.org/ebooks/a/b")
        assert got.title == "Book B"
        assert got.subtitle == "Sub"
        assert got.author_names == ["Jane Doe"]
        assert got.contributors[0].file_as == "Doe, Jane"
        assert got.release_date == "2020-01-01"
        assert store.catalog_count() == 1
        assert store.has_book(got.se_url)


def test_upsert_is_idempotent(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        store.upsert_book(_book())
        store.upsert_book(_book(title="Updated"))
        assert store.catalog_count() == 1
        assert store.get_book("https://standardebooks.org/ebooks/a/b").title == "Updated"


def test_processed_and_is_done(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        url = "https://standardebooks.org/ebooks/a/b"
        assert not store.is_done(url)
        store.mark_processed(url, "added", book_id=5, edition_id=9)
        assert store.is_done(url)
        assert store.processed_status(url) == "added"
        store.mark_processed(url, "error", detail="boom")
        assert not store.is_done(url)


def test_review_queue(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        url = "https://standardebooks.org/ebooks/a/b"
        store.enqueue_review(url, "Book B", "ambiguous", [{"id": 1, "title": "X"}])
        pending = store.pending_reviews()
        assert len(pending) == 1
        assert pending[0]["candidates"][0]["title"] == "X"
        store.resolve_review(url)
        assert store.pending_reviews() == []
