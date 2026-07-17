import csv

from fastapi.testclient import TestClient

from se_hardcover.audit import CSV_FIELDS
from se_hardcover.config import Settings
from se_hardcover.models import Contributor, SeBook
from se_hardcover.state import Store
from se_hardcover.sync import RefData
from se_hardcover.webapp import create_app
from tests.test_sync import FakeClient

REF = RefData(publisher_id=42, ebook_format_id=4, english_language_id=1)


class QueueFakeClient(FakeClient):
    def books_with_covers(self, ids):
        return {i: {"slug": f"book-{i}", "title": f"Book {i}",
                    "image_url": f"https://img/{i}.jpg"} for i in ids}


def _app(tmp_path, client):
    db = tmp_path / "s.sqlite3"
    store = Store(db)
    store.upsert_book(SeBook(
        se_url="https://standardebooks.org/ebooks/a/b", repo="a_b", title="A Book",
        contributors=[Contributor(name="Jane Doe", role="aut")],
        release_date="2020-01-01", cover_url="https://ex/c.jpg",
    ))
    store.enqueue_review("https://standardebooks.org/ebooks/a/b", "A Book",
                         "ambiguous", [{"id": 7, "title": "A Book", "slug": "a-book",
                                        "authors": ["Jane Doe"], "score": 0.8}])
    store.close()
    settings = Settings(hardcover_api_token="x", state_db_path=db)
    csv_path = tmp_path / "audit.csv"
    return create_app(settings, csv_path, client_provider=lambda: (client, REF)), csv_path


def test_queue_lists_items_with_cover(tmp_path):
    app, _ = _app(tmp_path, QueueFakeClient([]))
    c = TestClient(app)
    data = c.get("/api/queue").json()
    assert data["count"] == 1
    item = data["items"][0]
    assert item["title"] == "A Book"
    assert item["candidates"][0]["image_url"] == "https://img/7.jpg"
    assert item["candidates"][0]["hardcover_url"].endswith("/a-book")


def test_resolve_attach_removes_from_queue(tmp_path):
    client = QueueFakeClient([])
    app, _ = _app(tmp_path, client)
    c = TestClient(app)
    r = c.post("/api/queue/resolve", json={
        "se_url": "https://standardebooks.org/ebooks/a/b", "action": "attach", "book_id": 7})
    assert r.status_code == 200
    assert client.inserted_editions[0][0] == 7
    assert c.get("/api/queue").json()["count"] == 0


def test_refresh_rescans_queue_candidates(tmp_path):
    # Combined query returns a wrong book; title-only returns the right one.
    from se_hardcover.models import HardcoverBookMatch

    class RefreshClient(QueueFakeClient):
        def search_books(self, query, per_page=10):
            if "Jane Doe" in query:
                return [HardcoverBookMatch(id=99, title="Wrong Book", slug="wrong",
                                           author_names=["Someone"], users_count=500)]
            return [HardcoverBookMatch(id=7, title="A Book", slug="a-book",
                                       author_names=["Jane Doe"], users_count=50)]

    app, _ = _app(tmp_path, RefreshClient([]))
    c = TestClient(app)
    # Before refresh, stored candidate is the seeded wrong one (id 7 but score low).
    r = c.post("/api/queue/refresh")
    assert r.status_code == 200
    assert r.json()["updated"] == 1
    # After refresh, the queue's top candidate is the title-only match.
    item = c.get("/api/queue").json()["items"][0]
    assert item["candidates"][0]["id"] == 7


def test_auto_create_orphans_creates_no_match_items(tmp_path):
    # Search returns only an authorless junk stub -> no attach target -> the
    # endpoint creates a new book and clears the item from the queue.
    from se_hardcover.models import HardcoverBookMatch

    class OrphanClient(QueueFakeClient):
        def search_books(self, query, per_page=10):
            return [HardcoverBookMatch(id=1, title="A Book", slug="stub",
                                       author_names=[], users_count=0)]

    client = OrphanClient([])
    app, _ = _app(tmp_path, client)
    c = TestClient(app)
    r = c.post("/api/queue/auto-create")
    assert r.status_code == 200
    assert r.json()["created_count"] == 1
    assert r.json()["left"] == 0
    assert c.get("/api/queue").json()["count"] == 0


def test_auto_create_orphans_leaves_real_candidates(tmp_path):
    # A genuine same-author, same-title candidate is an attach target -> it stays
    # in the queue for a human rather than being auto-created.
    from se_hardcover.models import HardcoverBookMatch

    class RealCandidateClient(QueueFakeClient):
        def search_books(self, query, per_page=10):
            return [HardcoverBookMatch(id=7, title="A Book", slug="a-book",
                                       author_names=["Jane Doe"], users_count=300)]

    app, _ = _app(tmp_path, RealCandidateClient([]))
    c = TestClient(app)
    r = c.post("/api/queue/auto-create")
    assert r.json()["created_count"] == 0
    assert c.get("/api/queue").json()["count"] == 1


def test_index_html_served(tmp_path):
    app, _ = _app(tmp_path, QueueFakeClient([]))
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200
    assert "se-hardcover review" in r.text


def test_audit_read_save_roundtrip(tmp_path):
    app, csv_path = _app(tmp_path, QueueFakeClient([]))
    # seed an audit CSV
    with open(csv_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerow({"edition_id": "100", "hardcover_url": "u", "se_url": "s",
                    "field": "release_date", "problem": "p", "current": "1999",
                    "proposed": "2020", "fix_kind": "update_edition", "approve": ""})
    c = TestClient(app)
    assert c.get("/api/audit").json()["count"] == 1
    r = c.post("/api/audit/save", json={"approvals": {"100": {"release_date": True}}})
    assert r.json()["approved"] == 1
    # persisted to disk
    rows = list(csv.DictReader(open(csv_path)))
    assert rows[0]["approve"] == "yes"
