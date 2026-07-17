"""Match a Standard Ebooks book to an existing Hardcover book.

Pure, unit-testable scoring. The pipeline uses :func:`match_book` to decide
whether to auto-add an edition (CONFIDENT) or route to the review queue.
"""

from __future__ import annotations

import re
import unicodedata

from .models import (
    HardcoverBookMatch,
    MatchDecision,
    MatchResult,
    SeBook,
)

_ARTICLES = {"a", "an", "the"}
# Confidence thresholds (deliberately conservative — bias toward review).
CONFIDENT_SCORE = 0.90
REVIEW_FLOOR = 0.55
# A candidate only counts as something a librarian might *attach* to if its title
# genuinely matches this strongly. Below this, a shared title word or two is not a
# real overlap — the candidate is a different book, not an attach target.
STRONG_TITLE = 0.80
# A Hardcover record with at least this many readers is treated as an
# "established" work — real enough that two distinct established works both
# matching means genuine ambiguity (send to review). Junk/auto-generated
# duplicates of popular titles sit at ~0 users and are ignored as rivals.
ESTABLISHED_USERS = 15


def normalize_title(title: str) -> str:
    """Lowercase, strip diacritics/punctuation, drop a leading article."""
    text = _strip_accents(title).lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = text.split()
    if words and words[0] in _ARTICLES:
        words = words[1:]
    return " ".join(words)


def surname(name: str) -> str:
    """Best-effort surname from a display name ('Fyodor Dostoevsky' -> 'dostoevsky')."""
    cleaned = _strip_accents(name).lower().strip()
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", cleaned)
    parts = cleaned.split()
    return parts[-1] if parts else ""


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def _strip_subtitle(title: str) -> str:
    """Drop a trailing ``: subtitle`` from a title.

    Hardcover frequently stores the subtitle inline in the title
    ("Flatland: A Romance of Many Dimensions") while Standard Ebooks keeps a clean
    title ("Flatland"). Removing the colon-delimited tail lets those match as the
    same work. This is deliberately narrow: only a colon delimits a subtitle, so a
    genuinely different, omnibus work like "The Monster and Other Stories" (no
    colon) is left intact and does not get promoted.
    """
    head = title.split(":", 1)[0].strip()
    return head if len(head) >= 3 else title.strip()


def _title_score(se_title: str, cand_title: str) -> float:
    a, b = normalize_title(se_title), normalize_title(cand_title)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Same base title once a colon-delimited subtitle is removed — i.e. the
    # candidate is the SE work plus a subtitle ("Flatland" vs "Flatland: A
    # Romance…"). Treat as an all-but-certain title match.
    base_a, base_b = normalize_title(_strip_subtitle(se_title)), normalize_title(
        _strip_subtitle(cand_title)
    )
    if base_a and base_a == base_b:
        return 0.95
    # Token Jaccard as a softer signal for subtitle noise / minor differences.
    sa, sb = set(a.split()), set(b.split())
    inter, union = len(sa & sb), len(sa | sb)
    jaccard = inter / union if union else 0.0
    # One being a prefix/subset of the other is a strong-ish signal.
    if a in b or b in a:
        return max(0.85, jaccard)
    return jaccard


def _author_matches(se: SeBook, cand: HardcoverBookMatch) -> bool:
    """True when the SE book and candidate share an author surname."""
    se_surnames = {surname(a.name) for a in se.authors if surname(a.name)}
    cand_surnames = {surname(a) for a in cand.author_names if surname(a)}
    return bool(se_surnames and cand_surnames and (se_surnames & cand_surnames))


def score_candidate(se: SeBook, cand: HardcoverBookMatch) -> float:
    """Combine title and author agreement into a 0..1 confidence."""
    title = _title_score(se.title, cand.title)
    se_surnames = {surname(a.name) for a in se.authors if surname(a.name)}
    cand_surnames = {surname(a) for a in cand.author_names if surname(a)}
    if se_surnames and cand_surnames:
        author = 1.0 if (se_surnames & cand_surnames) else 0.0
    else:
        # No author info on the candidate — neutral, lean on title alone.
        author = 0.5
    # Title dominates; author confirms.
    return round(0.7 * title + 0.3 * author, 4)


def is_attach_target(se: SeBook, cand: HardcoverBookMatch) -> bool:
    """Would a librarian plausibly *attach* the SE edition to this existing book?

    Only if its title genuinely matches AND it is either by the same author or an
    established record (real reader count). This deliberately rejects the two
    things that clutter the review queue with nothing to decide:

    - **authorless junk stubs** — an exact title but no author and ~0 readers
      (e.g. the lone "Beyond Thirty" stub). Not a book anyone would attach to.
    - **clearly-different books** — a shared word or two in the title but a
      different work and a different author.

    When *no* candidate is an attach target, there is nothing for a human to
    weigh — the pipeline just creates a fresh Hardcover book.
    """
    if _title_score(se.title, cand.title) < STRONG_TITLE:
        return False
    return _author_matches(se, cand) or cand.users_count >= ESTABLISHED_USERS


def has_attach_target(se: SeBook, candidates: list[HardcoverBookMatch]) -> bool:
    """True when at least one candidate is worth a human's attach decision."""
    return any(is_attach_target(se, c) for c in candidates)


def match_book(se: SeBook, candidates: list[HardcoverBookMatch]) -> MatchResult:
    """Score candidates and classify the match.

    Popular public-domain titles have many duplicate/auto-generated records on
    Hardcover. We rank by title+author score, then within the confident tier
    pick the *canonical* record by reader count — so a junk duplicate scoring
    high does not by itself force a review. A review is only raised when two
    genuinely distinct, established works both match.
    """
    if not candidates:
        return MatchResult(
            decision=MatchDecision.CREATE,
            reason="no search results — nothing to attach to, will create",
        )

    scored = sorted(
        ((score_candidate(se, c), c) for c in candidates),
        key=lambda x: (x[0], x[1].users_count),
        reverse=True,
    )
    best_score = scored[0][0]
    ranked = [c for _, c in scored]

    high = [(s, c) for s, c in scored if s >= CONFIDENT_SCORE]
    if high:
        # Canonical = the confident-tier record with the most readers.
        best = max((c for _, c in high), key=lambda c: c.users_count)
        # Genuine ambiguity: two or more *distinct* established works both match.
        established_titles = {
            normalize_title(c.title) for _, c in high if c.users_count >= ESTABLISHED_USERS
        }
        if len(established_titles) >= 2:
            return MatchResult(
                decision=MatchDecision.REVIEW,
                best=best,
                score=best_score,
                candidates=ranked[:5],
                reason=f"ambiguous: {len(established_titles)} distinct established works match",
            )
        return MatchResult(
            decision=MatchDecision.CONFIDENT,
            best=best,
            score=best_score,
            candidates=ranked[:5],
            reason=f"title+author match ({best_score:.2f}), {best.users_count} readers",
        )

    # Not confident. A human only needs to weigh in if there is a candidate worth
    # attaching to; otherwise the "candidates" are junk stubs / different books and
    # there is nothing to decide — create a fresh Hardcover book.
    if has_attach_target(se, ranked):
        return MatchResult(
            decision=MatchDecision.REVIEW,
            best=scored[0][1],
            score=best_score,
            candidates=ranked[:5],
            reason=f"uncertain match — needs review ({best_score:.2f})",
        )
    return MatchResult(
        decision=MatchDecision.CREATE,
        best=scored[0][1],
        score=best_score,
        candidates=ranked[:5],
        reason=f"no attach target (best {best_score:.2f}) — will create new book",
    )
