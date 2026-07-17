"""Domain models shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


@dataclass(frozen=True)
class Contributor:
    """A person credited on an SE book, with their MARC role."""

    name: str  # display name, e.g. "Fyodor Dostoevsky"
    file_as: str = ""  # sort name, e.g. "Dostoevsky, Fyodor"
    role: str = "aut"  # MARC relator: aut, trl, ill, etc.


@dataclass
class SeBook:
    """Canonical Standard Ebooks metadata for a single release.

    Parsed from a book's ``content.opf`` plus the book page (for the cover URL).
    ``se_url`` (the canonical identifier) is the primary key throughout.
    """

    se_url: str  # https://standardebooks.org/ebooks/<author>/<slug>[/<translator>]
    repo: str  # GitHub repo name (path with '/' -> '_')
    title: str
    subtitle: str = ""
    contributors: list[Contributor] = field(default_factory=list)
    description: str = ""  # long HTML description
    short_description: str = ""
    subjects: list[str] = field(default_factory=list)
    release_date: str = ""  # ISO date (YYYY-MM-DD) of first SE release
    cover_url: str = ""  # full-size cover on standardebooks.org
    word_count: int | None = None

    @property
    def authors(self) -> list[Contributor]:
        return [c for c in self.contributors if c.role == "aut"]

    @property
    def translators(self) -> list[Contributor]:
        return [c for c in self.contributors if c.role == "trl"]

    @property
    def author_names(self) -> list[str]:
        return [c.name for c in self.authors]


@dataclass
class HardcoverContribution:
    author_name: str
    contribution: str | None  # None/"" = primary author, else "Translator", etc.


@dataclass
class HardcoverEdition:
    """An edition already present on Hardcover (used by the audit)."""

    id: int
    book_id: int
    book_title: str
    book_slug: str
    book_author_names: list[str] = field(default_factory=list)
    title: str | None = None
    subtitle: str | None = None
    release_date: str | None = None
    reading_format_id: int | None = None
    reading_format: str | None = None
    language_id: int | None = None
    publisher_id: int | None = None
    isbn_10: str | None = None
    isbn_13: str | None = None
    asin: str | None = None
    image_id: int | None = None
    image_url: str | None = None
    edition_format: str | None = None
    edition_information: str | None = None
    contributions: list[HardcoverContribution] = field(default_factory=list)


@dataclass
class HardcoverBookMatch:
    """A candidate book returned from a Hardcover search."""

    id: int
    title: str
    slug: str
    subtitle: str | None = None
    author_names: list[str] = field(default_factory=list)
    release_year: int | None = None
    editions_count: int | None = None
    # Adoption signal from search — high on the canonical record, ~0 on the
    # duplicate/auto-generated stubs that clutter popular titles.
    users_count: int = 0


class MatchDecision(StrEnum):
    CONFIDENT = "confident"  # auto-add edition to the matched book
    CREATE = "create"  # nothing plausible to attach to -> create a new book
    REVIEW = "review"  # a real attach target exists but confidence is short -> ask a human
    NONE = "none"  # (legacy) no plausible candidate found


@dataclass
class MatchResult:
    decision: MatchDecision
    best: HardcoverBookMatch | None = None
    score: float = 0.0
    candidates: list[HardcoverBookMatch] = field(default_factory=list)
    reason: str = ""


@dataclass
class Discrepancy:
    """A single audit finding against an existing Hardcover edition."""

    edition_id: int
    hardcover_url: str
    se_url: str | None
    field: str  # e.g. "cover", "release_date", "reading_format", "isbn", "book_match"
    problem: str
    current: str
    proposed: str
    fix_kind: str  # "update_edition", "insert_image", "manual", "none"
