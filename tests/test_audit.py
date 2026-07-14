import csv

from se_hardcover.audit import (
    CSV_FIELDS,
    _check_edition,
    build_catalog_index,
    match_edition_to_catalog,
    write_report,
)
from se_hardcover.models import (
    Contributor,
    HardcoverContribution,
    HardcoverEdition,
    SeBook,
)
from se_hardcover.state import Store
from se_hardcover.sync import RefData

REF = RefData(publisher_id=42, ebook_format_id=4, english_language_id=1)


def _se_book():
    return SeBook(
        se_url="https://standardebooks.org/ebooks/jane-austen/pride-and-prejudice",
        repo="jane-austen_pride-and-prejudice",
        title="Pride and Prejudice",
        contributors=[Contributor(name="Jane Austen", role="aut")],
        release_date="2015-05-01",
        cover_url="https://standardebooks.org/images/covers/x/abc/cover@2x.jpg",
    )


def _edition(**over):
    base = dict(
        id=100, book_id=9, book_title="Pride and Prejudice", book_slug="pride-and-prejudice",
        title="Pride and Prejudice", release_date="2015-05-01",
        reading_format_id=4, language_id=1, image_url="https://img/x.jpg",
        contributions=[HardcoverContribution("Jane Austen", None)],
    )
    base.update(over)
    return HardcoverEdition(**base)


def _index(store, tmp_path):
    store.upsert_book(_se_book())
    return build_catalog_index(store)


def test_catalog_index_and_match(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        index = _index(store, tmp_path)
        ed = _edition()
        assert match_edition_to_catalog(ed, index) is not None


def test_clean_edition_has_no_discrepancies(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        index = _index(store, tmp_path)
        ed = _edition()
        se = match_edition_to_catalog(ed, index)
        d = _check_edition(ed, se, REF, http=None, check_covers=False)
        assert d == []


def test_wrong_format_and_date_flagged(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        index = _index(store, tmp_path)
        ed = _edition(reading_format_id=1, release_date="1999-01-01")
        se = match_edition_to_catalog(ed, index)
        fields = {x.field for x in _check_edition(ed, se, REF, None, False)}
        assert "reading_format" in fields
        assert "release_date" in fields


def test_isbn_and_missing_cover_flagged(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        index = _index(store, tmp_path)
        ed = _edition(isbn_13="9781234567890", image_url=None)
        se = match_edition_to_catalog(ed, index)
        d = _check_edition(ed, se, REF, None, False)
        fields = {x.field: x for x in d}
        assert "isbn" in fields and fields["isbn"].fix_kind == "clear_field"
        assert "cover" in fields and fields["cover"].fix_kind == "insert_image"


def test_unmatched_edition_flagged(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        _index(store, tmp_path)
        ed = _edition(title="Some Book Not In SE", book_title="Some Book Not In SE",
                      contributions=[])
        d = _check_edition(ed, None, REF, None, False)
        assert len(d) == 1 and d[0].field == "book_match"


def test_apply_fixes_retries_dropped_update(tmp_path):
    from se_hardcover.audit import apply_fixes
    from tests.test_sync import FakeClient

    client = FakeClient([])
    real_update = client.update_edition
    calls = {"n": 0}

    def flaky(edition_id, dto):
        calls["n"] += 1
        if calls["n"] == 1:  # first write silently doesn't persist
            client.updated.append((edition_id, dto))
            return {"id": edition_id}
        return real_update(edition_id, dto)

    client.update_edition = flaky
    csv_path = tmp_path / "r.csv"
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_FIELDS)
        w.writerow([200, "u", "s", "reading_format", "not ebook", "1", "4",
                    "update_edition", "yes"])
    summary = apply_fixes(client, csv_path)
    assert summary["updated"] == 1 and summary["errors"] == 0
    assert calls["n"] >= 2  # retried after the dropped write
    assert client.edition_fields(200)["reading_format_id"] == 4


def test_apply_fixes_clears_isbn_and_asin(tmp_path):
    from se_hardcover.audit import apply_fixes
    from tests.test_sync import FakeClient

    client = FakeClient([])
    client._edition_state[300] = {"asin": "B0XYZ", "isbn_13": "9781234567890"}
    csv_path = tmp_path / "r.csv"
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_FIELDS)
        # Rows as an OLDER audit wrote them (fix_kind "manual") — must still clear.
        w.writerow([300, "u", "s", "asin", "asin set", "B0XYZ", "(clear)", "manual", "yes"])
        w.writerow([300, "u", "s", "isbn", "isbn set", "9781234567890", "(clear)", "manual", "yes"])
    summary = apply_fixes(client, csv_path)
    assert summary["cleared"] == 2 and summary["errors"] == 0
    state = client.edition_fields(300)
    assert state["asin"] == "" and state["isbn_13"] == "" and state["isbn_10"] == ""


def test_match_uses_book_authors_when_edition_has_none(tmp_path):
    # Our backfilled editions carry no edition-level contributions; the book's
    # authors must still let the audit match them.
    with Store(tmp_path / "s.sqlite3") as store:
        index = _index(store, tmp_path)
        ed = _edition(title="Pride and Prejudice", contributions=[],
                      book_author_names=["Jane Austen"])
        assert match_edition_to_catalog(ed, index) is not None


def test_auto_fix_triage(tmp_path):
    from se_hardcover.audit import auto_fix_discrepancies
    from se_hardcover.models import Discrepancy
    from tests.test_sync import FakeClient

    client = FakeClient([])
    client._edition_state[1] = {}
    discs = [
        Discrepancy(1, "u", "s", "reading_format", "not ebook", "Read", "4", "update_edition"),
        Discrepancy(1, "u", "s", "asin", "asin set", "B0X", "(clear)", "clear_field"),
        Discrepancy(2, "u", "s", "cover", "no cover", "(none)",
                    "https://se/cover.jpg", "insert_image"),
        Discrepancy(3, "u", "s", "cover", "cover differs", "https://hc/x.jpg",
                    "https://se/y.jpg", "insert_image"),
        Discrepancy(4, "u2", None, "book_match", "no SE match", "Some Book", "", "manual"),
    ]
    result = auto_fix_discrepancies(client, discs)
    assert result["fixed"] == 3  # editions 1, 2 and 3 got a verified update
    assert client.edition_fields(1)["reading_format_id"] == 4
    assert client.edition_fields(1)["asin"] == ""
    # Both a missing and a mismatched cover are reset to the SE cover.
    assert result["covers_fixed"] == 2
    # An edition matching no SE book is surfaced, never auto-changed.
    assert len(result["mis_attributed"]) == 1


def test_write_report_has_approve_column(tmp_path):
    with Store(tmp_path / "s.sqlite3") as store:
        index = _index(store, tmp_path)
        ed = _edition(reading_format_id=1)
        se = match_edition_to_catalog(ed, index)
        d = _check_edition(ed, se, REF, None, False)
        out = tmp_path / "report.csv"
        write_report(d, out)
        with open(out) as fh:
            rows = list(csv.DictReader(fh))
        assert rows and set(rows[0].keys()) == set(CSV_FIELDS)
        assert rows[0]["approve"] == ""
