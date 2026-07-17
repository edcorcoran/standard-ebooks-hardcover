from se_hardcover.matching import (
    match_book,
    normalize_title,
    score_candidate,
    surname,
)
from se_hardcover.models import Contributor, HardcoverBookMatch, MatchDecision, SeBook


def _se(title, authors, subtitle=""):
    return SeBook(
        se_url="https://standardebooks.org/ebooks/x/y",
        repo="x_y",
        title=title,
        subtitle=subtitle,
        contributors=[Contributor(name=a, role="aut") for a in authors],
    )


def _cand(id, title, authors, subtitle=None, users=0):
    return HardcoverBookMatch(
        id=id, title=title, slug=f"book-{id}", subtitle=subtitle,
        author_names=authors, users_count=users,
    )


def test_normalize_title_drops_article_and_punctuation():
    assert normalize_title("The Brothers Karamazov!") == "brothers karamazov"
    assert normalize_title("A Tale of Two Cities") == "tale of two cities"
    assert normalize_title("Les Misérables") == "les miserables"


def test_surname():
    assert surname("Fyodor Dostoevsky") == "dostoevsky"
    assert surname("W. E. B. Du Bois") == "bois"
    assert surname("") == ""


def test_exact_match_is_confident():
    se = _se("The Brothers Karamazov", ["Fyodor Dostoevsky"])
    cands = [_cand(1, "The Brothers Karamazov", ["Fyodor Dostoevsky"])]
    result = match_book(se, cands)
    assert result.decision == MatchDecision.CONFIDENT
    assert result.best.id == 1


def test_wrong_author_not_confident():
    se = _se("The Brothers Karamazov", ["Fyodor Dostoevsky"])
    cands = [_cand(1, "The Brothers Karamazov", ["Somebody Else"])]
    result = match_book(se, cands)
    # Perfect title but wrong author -> below the confident threshold.
    assert result.decision != MatchDecision.CONFIDENT


def test_no_candidates_creates_new_book():
    # Nothing on Hardcover to attach to -> create a fresh book, no review needed.
    se = _se("Obscure Title", ["Nobody"])
    result = match_book(se, [])
    assert result.decision == MatchDecision.CREATE


def test_weak_title_creates_new_book():
    # A candidate that is clearly a different book is not an attach target.
    se = _se("The Brothers Karamazov", ["Fyodor Dostoevsky"])
    cands = [_cand(1, "War and Peace", ["Leo Tolstoy"])]
    result = match_book(se, cands)
    assert result.decision == MatchDecision.CREATE


def test_authorless_stub_creates_new_book():
    # The "Beyond Thirty" case: an exact-title stub with no author and ~0 readers
    # is junk, not something to attach to. It must not trip a human review.
    se = _se("Beyond Thirty", ["Edgar Rice Burroughs"])
    cands = [_cand(1, "Beyond Thirty", [], users=0)]
    result = match_book(se, cands)
    assert result.decision == MatchDecision.CREATE


def test_established_rival_without_author_is_review():
    # An exact-title record with a real readership but no author on file is a
    # plausible attach target -> a human should decide, not auto-create.
    se = _se("The Republic", ["Plato"])
    cands = [_cand(1, "The Republic", [], users=500)]
    result = match_book(se, cands)
    assert result.decision == MatchDecision.REVIEW


def test_colon_subtitle_with_author_is_confident():
    # SE keeps a clean title; Hardcover carries the subtitle inline. Same base
    # title + same author -> confident auto-attach, not a review.
    se = _se("Flatland", ["Edwin A. Abbott"])
    cands = [_cand(1, "Flatland: A Romance of Many Dimensions", ["Edwin A. Abbott"], users=300)]
    result = match_book(se, cands)
    assert result.decision == MatchDecision.CONFIDENT
    assert result.best.id == 1


def test_omnibus_without_colon_is_not_promoted():
    # "The Monster" vs "The Monster and Other Stories" is a different (omnibus)
    # work — no colon, so it must NOT be promoted to confident.
    se = _se("The Monster", ["Edgar Saltus"])
    cands = [_cand(1, "The Monster and Other Stories", ["Edgar Saltus"], users=300)]
    result = match_book(se, cands)
    assert result.decision != MatchDecision.CONFIDENT


def test_subtitle_noise_still_scores_high():
    se = _se("The Souls of Black Folk", ["W. E. B. Du Bois"])
    cands = [_cand(1, "The Souls of Black Folk: Essays and Sketches", ["W. E. B. Du Bois"])]
    score = score_candidate(se, cands[0])
    assert score >= 0.85


def test_junk_duplicate_does_not_block_confident_match():
    # Popular titles attract auto-generated stubs (0 readers) alongside the
    # real record. The canonical (high-readers) record should win, confidently.
    se = _se("The Good Soldier", ["Ford Madox Ford"])
    cands = [
        _cand(727921, "The Good Soldier By Ford Madox Ford", ["Ford Madox Ford"], users=0),
        _cand(447883, "The Good Soldier", ["Ford Madox Ford"], users=208),
    ]
    result = match_book(se, cands)
    assert result.decision == MatchDecision.CONFIDENT
    assert result.best.id == 447883  # picked the canonical record by readership


def test_same_title_duplicates_pick_most_read_and_stay_confident():
    # Two records of the same work (same title/author) -> confident, and the
    # one with more readers wins.
    se = _se("Poems", ["John Keats"])
    cands = [
        _cand(1, "Poems", ["John Keats"], users=120),
        _cand(2, "Poems", ["John Keats"], users=90),
    ]
    result = match_book(se, cands)
    assert result.decision == MatchDecision.CONFIDENT
    assert result.best.id == 1  # most readers
