# se-hardcover

Sync [Standard Ebooks](https://standardebooks.org) releases into
[Hardcover.app](https://hardcover.app).

Standard Ebooks produces meticulously formatted, public-domain ebooks. This tool
adds each Standard Ebooks release to Hardcover as an **edition** on the matching
book, with the correct metadata and cover. It does three things:

1. **Audit** the Standard Ebooks editions already on Hardcover and report any
   data or cover problems for review.
2. **Backfill** the rest of the Standard Ebooks catalog as new editions.
3. **Watch** the new-releases feed and add each new release automatically (run as
   a small Docker daemon).

> This project is run by a Hardcover librarian with explicit permission from
> Standard Ebooks to add their catalog. If you are not a Hardcover librarian, the
> write operations here will not be available to your API token.

## How it works

- **Standard Ebooks is the source of truth.** Per-book metadata comes from each
  book's canonical [`content.opf`](https://standardebooks.org) on GitHub
  (`https://raw.githubusercontent.com/standardebooks/<repo>/master/src/epub/content.opf`).
  The full catalog is enumerated from the public listing at
  `https://standardebooks.org/ebooks`, and covers are taken from each book page.
  No Standard Ebooks patron credentials are required.
- **Hardcover is written via its GraphQL API** (`https://api.hardcover.app/v1/graphql`).
  The client throttles to stay under the 60 requests/minute limit and retries on
  429/5xx.
- **Matching is conservative, in both directions.** Each Standard Ebooks book
  gets one of three outcomes:
  - **Confident match** to an existing Hardcover book → the SE edition is added
    to it automatically.
  - **No plausible attach target** (no results, or only junk stubs / clearly
    different works) → a **new Hardcover book is created** automatically, with
    the SE authors, cover, and metadata.
  - **A real candidate exists but confidence falls short** → the book goes to a
    local **review queue** for a human decision. Only items genuinely worth a
    librarian's judgment land here.

## Install

Requires Python 3.12+.

```bash
# with uv (recommended)
uv sync

# or with pip
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Configuration

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
| --- | --- | --- |
| `HARDCOVER_API_TOKEN` | yes (for Hardcover writes) | Token from <https://hardcover.app/account/api>. Tokens expire every January 1. |
| `DISCORD_WEBHOOK_URL` | no | Discord webhook for notifications. Disabled if empty. |
| `STATE_DB_PATH` | no | sqlite state file (default `data/se_hardcover.sqlite3`). |
| `POLL_INTERVAL` | no | Daemon poll interval in seconds (default `3600`). |
| `AUDIT_INTERVAL` | no | Reconcile-audit cadence in seconds (`0` = every poll cycle). |
| `AUDIT_CHECK_COVERS` | no | If `true` (default), the audit also compares cover images. |
| `HEARTBEAT_PATH` | no | File touched each successful daemon cycle (Docker healthcheck). |
| `DRY_RUN` | no | If `true`, never send mutations to Hardcover. |

The `catalog` command needs no token.

## Usage

```bash
# Phase 0 — verify API access and resolve reference ids (read-only)
se-hardcover probe

# Phase 1 — build the local Standard Ebooks catalog cache (~1,480 books)
se-hardcover catalog

# Phase 2 — audit existing editions -> CSV, review, then apply approved rows
se-hardcover audit --out audit-report.csv
#   ...edit the `approve` column in audit-report.csv...
se-hardcover apply-fixes audit-report.csv

# Phase 3 — backfill the catalog (start with a dry run, then a small live run)
se-hardcover backfill --dry-run
se-hardcover backfill --limit 5
se-hardcover backfill

# Resolve anything the pipeline could not match confidently
se-hardcover review                                   # list the queue
se-hardcover review <se_url> --attach <hardcover_book_id>
se-hardcover review <se_url> --create                 # make a new Hardcover book
se-hardcover review <se_url> --skip
se-hardcover review --refresh                         # re-score the queue with the current matcher
se-hardcover review --auto-create                     # bulk-resolve items with no attach target
se-hardcover review --auto-create --attach-confident  # ...and attach confident matches too

# Phase 4 — run the daemon (one cycle, for testing)
se-hardcover watch --once

# Reconcile audit — coverage + data accuracy over all SE editions (run by hand)
se-hardcover sweep
```

### The reconcile audit

Beyond adding new releases, the daemon periodically **reconciles** Hardcover with
the Standard Ebooks catalog. Each reconcile pass:

1. Refreshes the catalog and **adds an edition for any SE book that doesn't have
   one** (confident matches attach automatically, books with no plausible match
   are created, the rest queued) — so every Standard Ebook ends up with a
   Hardcover edition.
2. **Audits every edition under the publisher** and auto-corrects the mechanical
   problems (wrong format, wrong release date, wrong or missing cover, stray
   ISBN/ASIN), with a verify-and-retry on each write. The SE cover is
   authoritative, so a cover that differs from it is reset to the SE cover.
   Cover comparison hashes are cached by URL (both sides use content-addressed
   image URLs), so after the first pass repeat audits download almost nothing.
3. **Flags what needs a human** — an edition that matches no SE book (a possible
   mis-attribution) — via a Discord summary. A healthy catalog produces no message.

It runs inside `se-hardcover watch` on the `AUDIT_INTERVAL` cadence (default: the
same cadence as the new-release check), and you can run it on demand with
`se-hardcover sweep` (`--dry-run`, `--no-covers`, `--no-refresh` available).

### Web review UI (optional)

Prefer clicking to typing? A lightweight local web app lets you work the review
queue and the audit report visually — see each book's cover next to its
candidate matches, attach/create/skip with a button, bulk-create everything
with no real match, and tick off audit fixes. SE compilation titles (Short
Fiction, Poetry, …) are badged and sorted to the top.

```bash
pip install '.[web]'
se-hardcover web            # serves http://127.0.0.1:8000 (localhost only)
```

It reuses the exact same logic as the `review` and `apply-fixes` commands, so
the CLI and UI stay in sync. The "Apply approved fixes" button writes the
approved audit rows to Hardcover; individual queue actions write immediately.

## Running the daemon (Docker)

```bash
cp .env.example .env    # fill in HARDCOVER_API_TOKEN (+ optional DISCORD_WEBHOOK_URL)
mkdir -p data && sudo chown -R 10001:10001 data   # container writes as uid 10001
docker compose up -d
docker compose logs -f
```

> The container runs as a non-root user (uid 10001). The bind-mounted `./data`
> directory must be writable by that uid, or sqlite fails with
> `attempt to write a readonly database`.

State (the sqlite database and a heartbeat file) is persisted to the `./data`
volume so restarts resume cleanly. The container runs as a non-root user and
touches `data/heartbeat` each successful cycle for healthchecks.

The compose file also starts the **review web UI** on port 8000, sharing the
daemon's state volume — open `http://<server>:8000` from any machine on your
network. It has no authentication; keep it on a trusted network. Set
`WEB_BIND_ADDR` in `.env` to control exposure: your server's LAN IP to stay
LAN-only, or `127.0.0.1` to require an SSH tunnel
(`ssh -L 8000:localhost:8000 <server>`). Note that Docker published ports
bypass ufw-style host firewalls — the bind address is the reliable control.

## Development

```bash
pip install -e ".[dev]"
ruff check .
pytest
```

Tests run fully offline against recorded fixtures (`content.opf` samples, an Atom
feed sample, and a listing-page sample) — no network or API token needed.

## Notes and caveats

- The Hardcover API is in beta; its schema can change. The `probe` command exists
  to confirm reference ids and edition/image behavior before any bulk write.
- Standard Ebooks releases are English-language, public-domain ebooks with no
  ISBN/ASIN; the audit clears any stray ISBN/ASIN automatically.
- Be a good API citizen: the tool sends a descriptive user-agent and throttles
  both Standard Ebooks and Hardcover requests.

## License

MIT — see [LICENSE](LICENSE).
