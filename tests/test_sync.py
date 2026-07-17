from se_hardcover.models import Contributor, HardcoverBookMatch, SeBook
from se_hardcover.notify import Notifier
from se_hardcover.state import Store
from se_hardcover.sync import Outcome, RefData, build_edition_dto, sync_book

REF = RefData(publisher_id=42, ebook_format_id=4, english_language_id=1)


class FakeClient:
    """Records mutations instead of calling Hardcover."""

    def __init__(self, candidates, existing_book_ids=None):
        self._candidates = candidates
        self._existing = set(existing_book_ids or [])
        self.inserted_editions = []
        self.inserted_images = []
        self.updated = []
        self._next_edition_id = 1000
        self._edition_state: dict[int, dict] = {}

    def search_books(self, query, per_page=10):
        return self._candidates

    def se_edition_book_ids(self, publisher_id):
        return self._existing

    def insert_edition(self, book_id, dto):
        self._next_edition_id += 1
        self.inserted_editions.append((book_id, dto))
        self._edition_state[self._next_edition_id] = dict(dto)
        return {"id": self._next_edition_id}

    def insert_image(self, imageable_id, url, imageable_type="Edition"):
        self.inserted_images.append((imageable_id, url))
        return 7777

    def update_edition(self, edition_id, dto):
        self.updated.append((edition_id, dto))
        self._edition_state.setdefault(edition_id, {}).update(dto)
        return {"id": edition_id}

    def edition_contributions(self, edition_id):
        return (self._edition_state.get(edition_id) or {}).get("contributions") or []

    def edition_fields(self, edition_id):
        return self._edition_state.get(edition_id)

    def resolve_author_id(self, name):
        # Deterministic fake ids keyed off the name.
        return 900 + (abs(hash(name)) % 100)

    def insert_book_with_edition(self, dto):
        self._next_edition_id += 1
        eid = self._next_edition_id
        self._edition_state[eid] = dict(dto)
        return {"id": eid, "edition": {"id": eid, "book_id": 7000 + eid}}


def _se(title="The Brothers Karamazov", author="Fyodor Dostoevsky"):
    return SeBook(
        se_url="https://standardebooks.org/ebooks/fyodor-dostoevsky/the-brothers-karamazov",
        repo="fyodor-dostoevsky_the-brothers-karamazov",
        title=title,
        contributors=[Contributor(name=author, role="aut")],
        release_date="2019-02-05",
        cover_url="https://standardebooks.org/images/covers/x/abc/cover@2x.jpg",
    )


def test_build_edition_dto():
    book = _se()
    book.subtitle = "A Novel"
    dto = build_edition_dto(book, REF)
    assert dto["title"] == "The Brothers Karamazov"
    assert dto["subtitle"] == "A Novel"
    assert dto["publisher_id"] == 42
    assert dto["reading_format_id"] == 4
    assert dto["language_id"] == 1
    assert dto["release_date"] == "2019-02-05"
    assert "isbn_13" not in dto  # SE books never carry ISBNs


def test_confident_match_adds_edition_and_cover(tmp_path):
    cands = [HardcoverBookMatch(id=55, title="The Brothers Karamazov", slug="tbk",
                                author_names=["Fyodor Dostoevsky"])]
    client = FakeClient(cands)
    with Store(tmp_path / "s.sqlite3") as store:
        result = sync_book(_se(), client, store, REF, Notifier())
        assert result.outcome == Outcome.ADDED
        assert result.book_id == 55
        assert client.inserted_editions[0][0] == 55
        assert client.inserted_images[0][0] == result.edition_id
        # The confirmed recipe: a final update_edition re-applies the full field
        # set (which insert_edition drops) AND links the cover via image_id.
        assert len(client.updated) == 1
        upd_edition_id, upd_dto = client.updated[0]
        assert upd_edition_id == result.edition_id
        assert upd_dto["image_id"] == 7777
        assert upd_dto["publisher_id"] == 42
        assert upd_dto["release_date"] == "2019-02-05"
        assert upd_dto["reading_format_id"] == 4
        assert store.is_done(_se().se_url)


def test_finalize_retries_when_update_does_not_persist(tmp_path):
    # Simulate Hardcover dropping the first update_edition after insert.
    cands = [HardcoverBookMatch(id=55, title="The Brothers Karamazov", slug="tbk",
                                author_names=["Fyodor Dostoevsky"])]
    client = FakeClient(cands)
    real_update = client.update_edition
    calls = {"n": 0}

    def flaky_update(edition_id, dto):
        calls["n"] += 1
        if calls["n"] == 1:
            client.updated.append((edition_id, dto))  # recorded but not persisted
            return {"id": edition_id}
        return real_update(edition_id, dto)

    client.update_edition = flaky_update
    with Store(tmp_path / "s.sqlite3") as store:
        result = sync_book(_se(), client, store, REF, Notifier())
        assert result.outcome == Outcome.ADDED
        # The verify caught the dropped write and retried; state is now correct.
        assert client.edition_fields(result.edition_id)["publisher_id"] == 42
        assert calls["n"] >= 2


def test_title_only_fallback_rescues_bad_combined_search(tmp_path):
    from se_hardcover.matching import CONFIDENT_SCORE
    from se_hardcover.sync import find_match

    class TwoQueryClient(FakeClient):
        def search_books(self, query, per_page=10):
            # Combined query (title + author) returns an unrelated popular book;
            # title-only returns the correct one.
            if "Fyodor" in query:
                return [HardcoverBookMatch(id=999, title="The Odyssey", slug="odyssey",
                                           author_names=["Homer"], users_count=4000)]
            return [HardcoverBookMatch(id=55, title="The Brothers Karamazov", slug="tbk",
                                       author_names=["Fyodor Dostoevsky"], users_count=200)]

    client = TwoQueryClient([])
    result = find_match(_se(), client)
    assert result.decision.value == "confident"
    assert result.best.id == 55
    assert result.score >= CONFIDENT_SCORE


def test_attach_target_but_unsure_goes_to_review_queue(tmp_path):
    # An exact-title record with real readers but no author on file is a plausible
    # attach target -> a human should decide, so it lands in the review queue.
    cands = [HardcoverBookMatch(id=1, title="The Brothers Karamazov",
                                slug="x", author_names=[], users_count=500)]
    client = FakeClient(cands)
    with Store(tmp_path / "s.sqlite3") as store:
        result = sync_book(_se(), client, store, REF, Notifier())
        assert result.outcome == Outcome.QUEUED
        assert not client.inserted_editions
        assert len(store.pending_reviews()) == 1


def test_no_attach_target_creates_new_book(tmp_path):
    # Only junk / clearly-different candidates -> nothing to attach to or review,
    # so the pipeline creates a fresh Hardcover book (with authors) directly.
    cands = [HardcoverBookMatch(id=1, title="A Completely Different Book",
                                slug="x", author_names=["Someone Else"])]
    client = FakeClient(cands)
    with Store(tmp_path / "s.sqlite3") as store:
        result = sync_book(_se(), client, store, REF, Notifier())
        assert result.outcome == Outcome.CREATED
        assert result.book_id is not None
        assert result.edition_id is not None
        assert not store.pending_reviews()  # nothing queued
        # The created edition carries the SE book's authors.
        assert client.edition_contributions(result.edition_id)


def test_existing_edition_is_skipped(tmp_path):
    cands = [HardcoverBookMatch(id=55, title="The Brothers Karamazov", slug="tbk",
                                author_names=["Fyodor Dostoevsky"])]
    client = FakeClient(cands, existing_book_ids={55})
    with Store(tmp_path / "s.sqlite3") as store:
        result = sync_book(_se(), client, store, REF, Notifier())
        assert result.outcome == Outcome.SKIPPED_EXISTING
        assert not client.inserted_editions


def test_already_processed_is_skipped(tmp_path):
    client = FakeClient([])
    with Store(tmp_path / "s.sqlite3") as store:
        store.mark_processed(_se().se_url, "added", book_id=1, edition_id=2)
        result = sync_book(_se(), client, store, REF, Notifier())
        assert result.outcome == Outcome.SKIPPED_EXISTING
