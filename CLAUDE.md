# CLAUDE.md

Guidance for AI agents working in this repo. Read the README first for what the
tool does; this file covers how to work on it safely.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # add ,web] for the local review UI
cp .env.example .env             # fill HARDCOVER_API_TOKEN to talk to Hardcover
```

- Python 3.12+. Tests and lint need no network and no token: `pytest && ruff check .`
- `se-hardcover probe` is the read-only "is my token/setup working" check.
- Never commit `.env`, `data/`, `*.log`, or `audit-report*.csv` (gitignored).

## Running

```bash
se-hardcover --help              # all commands
se-hardcover catalog             # build the local SE catalog cache (sqlite)
se-hardcover web                 # review UI at http://127.0.0.1:8000
se-hardcover watch --once        # one daemon cycle, for testing
se-hardcover sweep --dry-run     # reconcile audit without writing
docker compose up -d             # production daemon (state in ./data)
```

Anything that writes to Hardcover honors `DRY_RUN=true` — use it liberally.

## Architecture (one line per module)

- `cli.py` — Typer entry point; thin, all logic lives in the modules below.
- `standard_ebooks.py` — SE scraping: listing pages, `content.opf` parsing, cover URLs, Atom feed.
- `catalog.py` — builds/refreshes the local SE catalog cache (resumable).
- `hardcover.py` — GraphQL client: throttle, retries, all queries/mutations.
- `matching.py` — pure scoring functions; decides CONFIDENT / CREATE / REVIEW.
- `sync.py` — the add-an-edition pipeline shared by backfill, daemon, reconcile.
- `review.py` — resolving queue items (attach/create/skip) + bulk auto-resolve.
- `audit.py` — discrepancy detection + fixes for existing editions.
- `reconcile.py` — periodic coverage + data-integrity sweep run by the daemon.
- `state.py` — sqlite: catalog cache, processed log, review queue.
- `webapp.py` + `web/index.html` — FastAPI review UI (optional `[web]` extra).

Tests mirror modules (`tests/test_*.py`) and run fully offline against fixtures.

## Hard-won invariants — do not break these

**Hardcover API quirks (all confirmed by live probing):**

1. `insert_edition` / `insert_book` only persist `title` + `reading_format_id`;
   every other dto field is silently dropped. The pipeline is always
   insert → `insert_image` → `update_edition(full dto + image_id)`. Do not
   "simplify" it to a single insert.
2. `update_edition` occasionally reports success but does not persist (~1/14).
   All writes that matter go through `update_edition_verified` (read back +
   retry). Keep new write paths on it.
3. **Never retry a create mutation** (`insert_book`, `insert_edition`,
   `insert_author`, `insert_image`). They are non-idempotent: a timeout may
   land *after* the server committed, and a retry creates a duplicate. The
   client enforces this via `execute(..., idempotent=False)` — a transient
   error on a create raises instead of retrying. This once produced 4 duplicate
   books in production before the guard existed.
4. All requests go through one `HardcoverClient` whose lock + throttle keep us
   under the 60 req/min limit across threads (the web UI runs endpoints in a
   threadpool). Don't create per-request clients or bypass `execute`.
5. There is **no delete** in the API. A wrongly created book/edition can only be
   fixed by a human merging in the Hardcover UI. Bias every write decision
   accordingly.
6. `_ilike` etc. are disabled; use exact `_eq` filters or the `search` query.

**Matching rules:**

7. `matching.py` is pure and unit-tested — put new signals there, not inline in
   the pipeline. Three outcomes: CONFIDENT (attach), CREATE (nothing plausible
   to attach to), REVIEW (real candidate, short of confidence).
8. A candidate is only an *attach target* if its title genuinely matches AND it
   shares an author surname or has real readership (`ESTABLISHED_USERS`).
   Authorless zero-reader stubs are junk, not matches.
9. Author resolution (`resolve_author_id`): exact name match, pick the record
   with the most books, create a clean author if none. **Never fuzzy-search
   authors** — Hardcover has junk comma-joined author records and fuzzy search
   attaches wildly wrong people.
10. Newly created books get their authors via `update_edition(contributions=…)`
    on the edition (surfaces on the book). `update_book` cannot set
    contributions. Without this step created books are authorless.

**State semantics:**

11. `processed` (sqlite) is keyed by SE URL; `status="added"` means the edition
    exists on Hardcover. `resolve_review_item` treats a repeat attach/create for
    an already-added item as a no-op — that idempotency guard prevents
    double-clicks/retries from duplicating. Keep it.
12. The state DB is the daemon's memory. Deploying without it makes the
    coverage sweep re-evaluate everything — and re-CREATE books it can't match.
    Ship `data/se_hardcover.sqlite3` with the deployment.

## Conventions

- Line length 100 (`ruff`), no type-checker configured, docstrings explain *why*.
- Every new feature needs an offline test; extend the `FakeClient` in
  `tests/test_sync.py` rather than mocking HTTP.
- Be a good citizen to both APIs: keep throttles, descriptive user-agent, and
  caching as they are.
