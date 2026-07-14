from se_hardcover.models import (
    Contributor,
    HardcoverBookMatch,
    HardcoverEdition,
    SeBook,
)
from se_hardcover.notify import Notifier
from se_hardcover.reconcile import reconcile
from se_hardcover.state import Store
from se_hardcover.sync import RefData
from tests.test_sync import FakeClient

REF = RefData(publisher_id=42, ebook_format_id=4, english_language_id=1)


class ReconcileFake(FakeClient):
    def __init__(self, candidates_by_query, existing_book_ids, editions):
        super().__init__([], existing_book_ids)
        self._by_query = candidates_by_query
        self._editions = editions

    def search_books(self, query, per_page=10):
        for key, cands in self._by_query.items():
            if key in query:
                return cands
        return []

    def editions_for_publisher(self, publisher_id):
        return self._editions


def test_reconcile_adds_missing_and_fixes_data(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        # Book A: catalogued, never processed -> coverage should add it.
        store.upsert_book(SeBook(
            se_url="https://standardebooks.org/ebooks/a/alpha", repo="a_alpha",
            title="Alpha", contributors=[Contributor(name="Author A", role="aut")],
            release_date="2020-01-01", cover_url="https://se/a.jpg"))
        # Book B: already has an edition (marked done) -> coverage skips it, but
        # its existing edition has the wrong format -> data audit fixes it.
        store.upsert_book(SeBook(
            se_url="https://standardebooks.org/ebooks/b/beta", repo="b_beta",
            title="Beta", contributors=[Contributor(name="Author B", role="aut")],
            release_date="2021-02-02", cover_url="https://se/b.jpg"))
        store.mark_processed("https://standardebooks.org/ebooks/b/beta", "added",
                             book_id=200, edition_id=500)

        edition_b = HardcoverEdition(
            id=500, book_id=200, book_title="Beta", book_slug="beta",
            book_author_names=["Author B"], title="Beta", release_date="2021-02-02",
            reading_format_id=1, language_id=1, image_url="https://hc/b.jpg",
            contributions=[])

        client = ReconcileFake(
            candidates_by_query={
                "Alpha": [HardcoverBookMatch(id=100, title="Alpha", slug="alpha",
                                             author_names=["Author A"], users_count=50)],
            },
            existing_book_ids={200},
            editions=[edition_b],
        )
        client._edition_state[500] = {"reading_format_id": 1}

        summary = reconcile(client, store, REF, Notifier(),
                            check_covers=False, refresh_catalog=False)

        assert summary["coverage_added"] == 1        # Book A added
        assert client.inserted_editions[0][0] == 100
        assert summary["data_fixed"] == 1            # Book B format corrected
        assert client.edition_fields(500)["reading_format_id"] == 4
        assert summary["mis_attributed"] == 0


def test_reconcile_flags_mis_attributed_edition(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        # An edition under the publisher that matches no catalogued SE book.
        edition = HardcoverEdition(
            id=900, book_id=999, book_title="Not An SE Book", book_slug="nope",
            title="Not An SE Book", reading_format_id=4, contributions=[])
        client = ReconcileFake({}, existing_book_ids={999}, editions=[edition])
        summary = reconcile(client, store, REF, Notifier(),
                            check_covers=False, refresh_catalog=False)
        assert summary["mis_attributed"] == 1
        assert summary["data_fixed"] == 0
