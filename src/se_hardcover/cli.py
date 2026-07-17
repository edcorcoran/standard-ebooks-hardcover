"""Command-line interface for se-hardcover.

Subcommands:
  probe        one-off Phase 0 check of the API (reference ids + optional test write)
  catalog      build/refresh the local Standard Ebooks catalog cache
  audit        report discrepancies on existing Hardcover editions (writes CSV)
  apply-fixes  execute approved rows from an audit CSV
  backfill     add editions for catalog books not yet on Hardcover
  review       list / resolve the review queue
  watch        daemon: poll the Atom feed and sync new releases
"""

from __future__ import annotations

import logging
import signal
import time
from pathlib import Path

import typer

from . import standard_ebooks as se
from .audit import apply_fixes as apply_fixes_impl
from .audit import audit_editions, summarize, write_report
from .catalog import build_catalog
from .config import load_settings
from .hardcover import HardcoverAuthError, HardcoverClient
from .notify import Notifier
from .reconcile import reconcile
from .review import auto_create_orphans, refresh_queue, resolve_review_item
from .state import Store
from .sync import Outcome, resolve_ref_data, sync_book

app = typer.Typer(add_completion=False, help="Sync Standard Ebooks releases into Hardcover.")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
# httpx logs every request URL at INFO — which would print the Discord webhook
# URL (a write-credential) into the daemon logs. Warnings only.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("se_hardcover")


def _client(dry_run: bool = False) -> HardcoverClient:
    settings = load_settings(dry_run=dry_run or None)
    return HardcoverClient(settings.require_token(), dry_run=settings.dry_run)


def _store() -> Store:
    return Store(load_settings().state_db_path)


# -- probe (Phase 0) ------------------------------------------------------


@app.command()
def probe(
    test_url: str = typer.Option(
        None, help="SE book URL to attempt a live insert_edition + cover on."
    ),
    do_write: bool = typer.Option(
        False, "--write", help="Actually perform the test mutation (default: read-only)."
    ),
):
    """Verify API access and resolve reference ids. With --test-url --write, add one edition."""
    client = _client()
    me = client.whoami()
    typer.echo(f"Authenticated as: {me.get('username')} (id {me.get('id')})")

    ref = resolve_ref_data(client)
    typer.echo(
        f"publisher_id(standard-ebooks) = {ref.publisher_id}\n"
        f"ebook reading_format_id       = {ref.ebook_format_id}\n"
        f"english language_id           = {ref.english_language_id}"
    )
    typer.echo(f"reading_formats: {client.reading_formats()}")

    existing = client.se_edition_book_ids(ref.publisher_id)
    typer.echo(f"Existing editions under publisher: {len(existing)}")

    if not test_url:
        typer.echo("\nNo --test-url given; skipping test write. (Phase 0 read checks passed.)")
        return

    book = se.fetch_book(test_url)
    typer.echo(f"\nSE book: {book.title!r} by {', '.join(book.author_names)}")
    typer.echo(f"  release_date={book.release_date} cover={book.cover_url}")
    candidates = client.search_books(f"{book.title} {book.author_names[0] if book.author_names else ''}")
    typer.echo(f"  top search hits: {[(c.id, c.title) for c in candidates[:3]]}")

    if not do_write:
        typer.echo("\n(read-only; pass --write to perform the test insert_edition)")
        return

    if not candidates:
        raise typer.Exit("No candidates to attach a test edition to.")
    from .sync import create_se_edition

    book_id = candidates[0].id
    typer.echo(f"\nInserting test edition on book {book_id} ({candidates[0].slug})...")
    edition_id, cover_detail = create_se_edition(book, client, ref, book_id)
    typer.echo(f"  -> edition id {edition_id}, {cover_detail}")
    typer.echo(f"\nCheck https://hardcover.app/books/{candidates[0].slug}/editions")


# -- catalog (Phase 1) ----------------------------------------------------


@app.command()
def catalog(
    refresh: bool = typer.Option(False, help="Re-fetch books already cached."),
    limit: int = typer.Option(None, help="Only process the first N listed books."),
):
    """Build/refresh the local Standard Ebooks catalog cache."""
    with _store() as store:
        summary = build_catalog(store, refresh=refresh, limit=limit)
        typer.echo(f"Catalog: {summary}. Total cached: {store.catalog_count()}")


# -- audit (Phase 2) ------------------------------------------------------


@app.command()
def audit(
    out: Path = typer.Option(Path("audit-report.csv"), help="CSV report output path."),
    no_covers: bool = typer.Option(False, help="Skip the (slow) cover image comparison."),
):
    """Audit existing Hardcover editions and write a discrepancy CSV."""
    client = _client()
    with _store() as store:
        if store.catalog_count() == 0:
            raise typer.Exit("Catalog is empty. Run `se-hardcover catalog` first.")
        ref = resolve_ref_data(client)
        discrepancies = audit_editions(client, store, ref, check_covers=not no_covers)
        write_report(discrepancies, out)
        typer.echo(summarize(discrepancies))
        typer.echo(f"\nReport written to {out}. Review it, set the `approve` column, "
                   f"then run `se-hardcover apply-fixes {out}`.")


@app.command(name="apply-fixes")
def apply_fixes_cmd(
    report: Path = typer.Argument(..., help="Audit CSV with approved rows."),
    dry_run: bool = typer.Option(False, help="Show what would change without writing."),
):
    """Execute approved rows from an audit CSV."""
    client = _client(dry_run=dry_run)
    summary = apply_fixes_impl(client, report)
    typer.echo(f"Apply-fixes: {summary}")


# -- backfill (Phase 3) ---------------------------------------------------


@app.command()
def backfill(
    dry_run: bool = typer.Option(False, help="Plan only; no writes to Hardcover."),
    limit: int = typer.Option(None, help="Process at most N books this run."),
    force: bool = typer.Option(False, help="Reprocess books already marked done."),
):
    """Add Standard Ebooks editions for catalog books not yet on Hardcover."""
    settings = load_settings(dry_run=dry_run or None)
    client = HardcoverClient(settings.require_token(), dry_run=settings.dry_run)
    notifier = Notifier(settings.discord_webhook_url)
    counts: dict[str, int] = {}
    with Store(settings.state_db_path) as store:
        books = store.all_books()
        if not books:
            raise typer.Exit("Catalog is empty. Run `se-hardcover catalog` first.")
        ref = resolve_ref_data(client)
        # Fetch the existing-editions set once and refresh it as we add, so we
        # never re-query it per book but still avoid duplicates within a run.
        existing = client.se_edition_book_ids(ref.publisher_id)
        processed = 0
        for book in books:
            if limit and processed >= limit:
                break
            if not force and store.is_done(book.se_url):
                continue
            result = sync_book(book, client, store, ref, notifier, force=force,
                               existing_book_ids=existing)
            if result.book_id and result.outcome in (Outcome.ADDED, Outcome.CREATED):
                existing.add(result.book_id)
            counts[result.outcome.value] = counts.get(result.outcome.value, 0) + 1
            processed += 1
            if result.outcome in (Outcome.ADDED, Outcome.CREATED, Outcome.QUEUED, Outcome.ERROR):
                log.info("%s -> %s (%s)", book.title[:50], result.outcome.value, result.detail)
    typer.echo(f"Backfill complete: {counts}")


# -- review ---------------------------------------------------------------


@app.command()
def review(
    se_url: str = typer.Argument(None, help="Resolve this queued book."),
    attach: int = typer.Option(None, help="Hardcover book id to add the SE edition to."),
    create: bool = typer.Option(False, help="Create a NEW Hardcover book + edition."),
    skip: bool = typer.Option(False, help="Mark this item resolved without acting."),
    refresh: bool = typer.Option(False, help="Re-score all pending items with the current matcher."),
    auto_create: bool = typer.Option(
        False, "--auto-create",
        help="Create new books for every queued item with no real attach target.",
    ),
    attach_confident: bool = typer.Option(
        False, "--attach-confident",
        help="With --auto-create, also auto-attach items that now match confidently.",
    ),
):
    """List the review queue, or resolve one item by attaching / creating / skipping."""
    settings = load_settings()
    with Store(settings.state_db_path) as store:
        if refresh:
            client = HardcoverClient(settings.require_token())
            summary = refresh_queue(store, client)
            typer.echo(f"Re-scored queue: {summary}")
            return
        if auto_create:
            client = HardcoverClient(settings.require_token())
            ref = resolve_ref_data(client)
            summary = auto_create_orphans(store, client, ref, attach_confident=attach_confident)
            typer.echo(
                f"Auto-created {summary['created_count']} new book(s); "
                f"attached {summary['attached_count']} confident match(es); "
                f"{summary['left']} left for review."
            )
            return
        if not se_url:
            pending = store.pending_reviews()
            if not pending:
                typer.echo("Review queue is empty.")
                return
            typer.echo(f"{len(pending)} item(s) awaiting review:\n")
            for item in pending:
                typer.echo(f"• {item['title']}  ({item['se_url']})")
                typer.echo(f"    reason: {item['reason']}")
                for c in item["candidates"]:
                    typer.echo(
                        f"    candidate book {c['id']}: {c['title']} "
                        f"[{', '.join(c.get('authors', []))}] score={c.get('score')}"
                    )
                typer.echo("")
            typer.echo("Resolve with: se-hardcover review <se_url> --attach <book_id> | "
                       "--create | --skip")
            return

        if not store.get_book(se_url):
            raise typer.Exit(f"{se_url} is not in the catalog.")

        action = "attach" if attach else "create" if create else "skip" if skip else None
        if action is None:
            raise typer.Exit("Pass one of --attach <book_id>, --create, or --skip.")

        client = HardcoverClient(settings.require_token())
        ref = resolve_ref_data(client)
        res = resolve_review_item(store, client, ref, se_url, action, book_id=attach)
        typer.echo(f"{res.action}: book {res.book_id}, edition {res.edition_id} ({res.detail})")


# -- web (local review UI) ------------------------------------------------


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", help="Bind address (localhost by default)."),
    port: int = typer.Option(8000, help="Port to serve on."),
    audit_report: Path = typer.Option(
        Path("audit-report.csv"), help="Audit CSV to review in the UI."
    ),
):
    """Launch the local web UI for reviewing the queue and the audit CSV."""
    try:
        import uvicorn

        from .webapp import create_app
    except ImportError as exc:
        raise typer.Exit(
            "Web UI needs the optional extra. Install with: pip install '.[web]'"
        ) from exc

    app_ = create_app(load_settings(), audit_report)
    typer.echo(f"se-hardcover review UI → http://{host}:{port}")
    uvicorn.run(app_, host=host, port=port, log_level="warning")


# -- watch (Phase 4) ------------------------------------------------------


@app.command()
def watch(
    once: bool = typer.Option(False, help="Run a single cycle and exit."),
    dry_run: bool = typer.Option(False, help="No writes to Hardcover."),
):
    """Daemon: poll the new-releases Atom feed and sync new books."""
    settings = load_settings(dry_run=dry_run or None)
    notifier = Notifier(settings.discord_webhook_url)
    stopping = {"flag": False}

    def _stop(signum, _frame):
        log.info("Received signal %s; will stop after this cycle.", signum)
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    audit_interval = settings.audit_interval or settings.poll_interval
    last_audit = 0.0
    backoff = 60.0
    while not stopping["flag"]:
        try:
            due = (time.monotonic() - last_audit) >= audit_interval
            _watch_cycle(settings, notifier, run_audit=due)
            if due:
                last_audit = time.monotonic()
            backoff = 60.0
            _touch(settings.heartbeat_path)
        except HardcoverAuthError as exc:
            notifier.error("watch", f"Auth failure — token expired? {exc}")
            log.error("Auth failure: %s", exc)
            time.sleep(min(backoff, 3600))
            backoff = min(backoff * 2, 3600)
        except Exception as exc:
            notifier.error("watch", f"Cycle failed: {exc}")
            log.exception("Watch cycle failed")
            time.sleep(min(backoff, 3600))
            backoff = min(backoff * 2, 3600)

        if once or stopping["flag"]:
            break
        _sleep_interruptibly(settings.poll_interval, stopping)

    log.info("Watch loop exited.")


def _watch_cycle(settings, notifier: Notifier, *, run_audit: bool = False) -> None:
    client = HardcoverClient(settings.require_token(), dry_run=settings.dry_run)
    with Store(settings.state_db_path) as store:
        ref = resolve_ref_data(client)
        entries = se.fetch_new_releases()
        log.info("Feed: %d entries", len(entries))
        for entry in entries:
            if store.is_done(entry.se_url) or store.processed_status(entry.se_url) == "queued":
                continue
            try:
                book = se.fetch_book(entry.se_url)
                store.upsert_book(book)
                result = sync_book(book, client, store, ref, notifier)
                log.info("%s -> %s", entry.title[:50], result.outcome.value)
            except Exception as exc:  # one bad book must not kill the cycle
                log.exception("Failed on %s", entry.se_url)
                store.mark_processed(entry.se_url, "error", detail=str(exc))
                notifier.error("watch", f"{entry.title}: {exc}")

        if run_audit:
            log.info("Running reconcile audit")
            reconcile(client, store, ref, notifier,
                      check_covers=settings.audit_check_covers)


# -- sweep (manual reconcile audit) ---------------------------------------


@app.command()
def sweep(
    dry_run: bool = typer.Option(False, help="No writes to Hardcover."),
    no_covers: bool = typer.Option(False, help="Skip the cover image comparison."),
    no_refresh: bool = typer.Option(False, help="Skip refreshing the SE catalog first."),
):
    """Run the reconcile audit once: coverage + data accuracy over all SE editions.

    This is the same pass the daemon runs on a cadence — safe to run by hand.
    """
    settings = load_settings(dry_run=dry_run or None)
    client = HardcoverClient(settings.require_token(), dry_run=settings.dry_run)
    notifier = Notifier(settings.discord_webhook_url)
    with Store(settings.state_db_path) as store:
        ref = resolve_ref_data(client)
        summary = reconcile(client, store, ref, notifier,
                            check_covers=not no_covers, refresh_catalog=not no_refresh)
    typer.echo(f"Reconcile: {summary}")


def _sleep_interruptibly(seconds: int, stopping: dict) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end and not stopping["flag"]:
        time.sleep(min(5.0, end - time.monotonic()))


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


if __name__ == "__main__":
    app()
