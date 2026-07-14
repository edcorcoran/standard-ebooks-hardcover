from se_hardcover.models import Contributor, SeBook
from se_hardcover.review import resolve_review_item
from se_hardcover.state import Store
from se_hardcover.sync import RefData
from tests.test_sync import FakeClient

REF = RefData(publisher_id=42, ebook_format_id=4, english_language_id=1)


def _seed(store, se_url="https://standardebooks.org/ebooks/a/b"):
    store.upsert_book(SeBook(
        se_url=se_url, repo="a_b", title="A Book",
        contributors=[Contributor(name="Jane Doe", role="aut")],
        release_date="2020-01-01", cover_url="https://ex/c.jpg",
    ))
    store.enqueue_review(se_url, "A Book", "ambiguous", [{"id": 7, "title": "A Book"}])
    return se_url


def test_attach_resolves_and_adds_edition(tmp_path):
    client = FakeClient([])
    with Store(tmp_path / "s.sqlite3") as store:
        se_url = _seed(store)
        res = resolve_review_item(store, client, REF, se_url, "attach", book_id=7)
        assert res.action == "attach" and res.book_id == 7
        assert client.inserted_editions[0][0] == 7
        assert store.is_done(se_url)
        assert store.pending_reviews() == []


def test_skip_resolves_without_writing(tmp_path):
    client = FakeClient([])
    with Store(tmp_path / "s.sqlite3") as store:
        se_url = _seed(store)
        res = resolve_review_item(store, client, REF, se_url, "skip")
        assert res.action == "skip"
        assert not client.inserted_editions
        assert store.pending_reviews() == []


def test_attach_without_book_id_raises(tmp_path):
    client = FakeClient([])
    with Store(tmp_path / "s.sqlite3") as store:
        se_url = _seed(store)
        import pytest
        with pytest.raises(ValueError):
            resolve_review_item(store, client, REF, se_url, "attach")
