"""CLI entry point for linkedin-enricher."""

import sys
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

console = Console()


@click.group()
def cli():
    """Enrich Apple Contacts with LinkedIn photos and profile URLs."""
    pass


@cli.command()
@click.option(
    "--limit",
    "-n",
    default=None,
    type=int,
    help="Max contacts to search (for incremental runs)",
)
@click.option(
    "--min-confidence",
    default=0.40,
    show_default=True,
    type=float,
    help="Minimum confidence score to store a match (0.0–1.0)",
)
@click.option(
    "--retry-errors",
    is_flag=True,
    help="Re-scrape contacts that previously errored out",
)
def scrape(limit: Optional[int], min_confidence: float, retry_errors: bool):
    """
    Search LinkedIn for un-matched contacts and store results in the local DB.

    Safe to re-run: already-searched contacts are skipped. Use --limit to
    process contacts in increments across multiple sessions (LinkedIn rate limits).
    Use --retry-errors to re-attempt contacts that failed in a previous run.
    """
    from .contacts_reader import load_contacts_to_db
    from .db import init_db
    from .linkedin_scraper import scrape_all

    init_db()

    console.print("[bold]Step 1:[/bold] Loading contacts from Apple Contacts.app...")
    loaded = load_contacts_to_db(only_without_photo=True)
    console.print(
        f"  Contacts loaded (without photo/LinkedIn): [green]{loaded}[/green]"
    )

    console.print("\n[bold]Step 2:[/bold] Searching LinkedIn...")
    scrape_all(
        limit=limit,
        min_confidence=min_confidence,
        retry_errors=retry_errors,
        console=console,
    )


@cli.command()
@click.option(
    "--port",
    default=5000,
    show_default=True,
    type=int,
    help="Port for the review web UI",
)
@click.option("--no-browser", is_flag=True, help="Don't auto-open the browser")
def review(port: int, no_browser: bool):
    """
    Open the batch review UI in your browser.

    Displays all pending LinkedIn matches with photos, confidence scores,
    and approve/reject/skip controls. Submit decisions in one batch.

    After reviewing, run: linkedin-enricher apply
    """
    from .db import get_pending_matches, init_db
    from .review_server import run_review_server

    init_db()
    pending = list(get_pending_matches())
    if not pending:
        console.print(
            "[yellow]No pending matches to review.[/yellow]\n"
            "Run [bold]linkedin-enricher scrape[/bold] first."
        )
        sys.exit(0)

    console.print(f"[green]{len(pending)}[/green] pending matches to review.")
    run_review_server(port=port, open_browser=not no_browser)


@cli.command()
@click.option("--debug", is_flag=True, help="Print raw profile keys to diagnose extraction")
@click.option("-n", "--limit", default=None, type=int, help="Max profiles to fetch")
def enrich(debug: bool, limit: Optional[int]):
    """Fetch current title and company for already-matched contacts that are missing them.

    Useful after upgrading to a version that added these fields.
    Authenticates with LinkedIn and fetches one DASH profile per matched contact.
    """
    from .db import LinkedinMatch, db, init_db
    from .linkedin_scraper import (
        _fetch_dash_profile,
        _extract_current_position,
        _get_linkedin_client,
    )


    init_db()

    # Find matches with a URL but no current_title/current_company yet
    matches = list(
        LinkedinMatch.select()
        .where(
            LinkedinMatch.linkedin_url.is_null(False)
            & LinkedinMatch.current_title.is_null(True)
            & LinkedinMatch.current_company.is_null(True)
        )
    )

    if limit:
        matches = matches[:limit]

    if not matches:
        console.print("[yellow]No matches need enriching.[/yellow]")
        return

    console.print(f"[green]Matches to enrich:[/green] {len(matches)}")

    try:
        api = _get_linkedin_client()
    except Exception as exc:
        console.print(f"[red]LinkedIn login failed: {exc}[/red]")
        sys.exit(1)

    from .linkedin_scraper import _TokenBucket
    from .config import RATE_LIMIT_RPM, BATCH_SIZE, BATCH_PAUSE_SECONDS

    bucket = _TokenBucket(RATE_LIMIT_RPM)
    updated = 0
    failed = 0
    for i, m in enumerate(matches):
        bucket.acquire()

        try:
            if m.urn_id:
                profile = _fetch_dash_profile(api, m.urn_id)
                profile_view = None
            else:
                # No urn_id — search by name to find the urn_id, then fetch DASH profile
                public_id = m.linkedin_url.rstrip("/").split("/")[-1]
                results = api.search_people(keywords=m.linkedin_name, limit=5) or []
                if debug:
                    console.print(
                        f"  [dim]search({m.linkedin_name!r}) → {len(results)} result(s),"
                        f" want publicIdentifier={public_id!r}[/dim]"
                    )
                profile = {}
                profile_view = None
                for r in results:
                    urn_id = r.get("urn_id")
                    if not urn_id:
                        continue
                    p = _fetch_dash_profile(api, urn_id)
                    got_id = p.get("publicIdentifier")
                    if debug:
                        console.print(
                            f"  [dim]  urn={urn_id} publicIdentifier={got_id!r}[/dim]"
                        )
                    if got_id == public_id:
                        profile = p
                        with db.atomic():
                            m.urn_id = urn_id
                            m.save()
                        break
                if not profile and not results:
                    console.print(
                        f"  [yellow]{m.linkedin_name}: search returned no results"
                        f" — session may be soft-blocked[/yellow]"
                    )
                    failed += 1
                    continue
        except Exception as exc:
            console.print(f"  [red]{m.linkedin_name}: {exc}[/red]")
            failed += 1
            continue

        if not profile:
            console.print(
                f"  [yellow]{m.linkedin_name}: no profile returned[/yellow]"
            )
            failed += 1
            continue

        if debug:
            console.print(f"  [dim]profile keys: {list(profile.keys())}[/dim]")
            for pk in ("experience", "positions", "positionGroups", "profilePositionGroups"):
                val = profile.get(pk)
                if val is not None:
                    console.print(f"  [dim]{pk}: {val}[/dim]")

        title, company = _extract_current_position(profile)
        with db.atomic():
            m.current_title = title
            m.current_company = company
            m.save()

        # Write to Apple Contacts for already-applied matches
        if m.status == "applied" and (title or company):
            from .contacts_writer import write_enriched_fields
            write_enriched_fields(m, console=console)

        console.print(
            f"  [green]✓[/green] {m.linkedin_name}:"
            f" {title or '—'} @ {company or '—'}"
        )
        updated += 1

        if (i + 1) % BATCH_SIZE == 0 and (i + 1) < len(matches):
            console.print(f"  [yellow]Pausing {BATCH_PAUSE_SECONDS}s...[/yellow]")
            import time
            time.sleep(BATCH_PAUSE_SECONDS)

    console.print(f"\n[green]Done.[/green] Updated: {updated}, Failed: {failed}")


@cli.command()
@click.option(
    "--dry-run", is_flag=True, help="Preview changes without writing to Contacts"
)
@click.option(
    "--contact-id",
    default=None,
    type=str,
    help="Apply only for a specific contact GUID",
)
def apply(dry_run: bool, contact_id: Optional[str]):
    """
    Write approved LinkedIn data to Apple Contacts.app.

    Sets profile photos and adds LinkedIn URL for all approved matches.
    Use --dry-run to preview what would be changed.
    """
    from .contacts_writer import apply_approved_matches
    from .db import init_db

    init_db()
    apply_approved_matches(
        dry_run=dry_run,
        contact_id_filter=contact_id,
        console=console,
    )


@cli.command()
def status():
    """Show a summary of the current DB state."""
    from .db import init_db, summary

    init_db()
    s = summary()

    table = Table(
        title="linkedin-enricher status", show_header=True, header_style="bold"
    )
    table.add_column("Stage", style="cyan")
    table.add_column("Count", justify="right")

    table.add_row("Contacts in DB", str(s["contacts"]))
    table.add_row("Searched on LinkedIn", str(s["searched"]))
    table.add_row("Pending review", str(s["pending_review"]))
    table.add_row("Approved (not yet applied)", str(s["approved"]))
    table.add_row("Rejected", str(s["rejected"]))
    table.add_row("Skipped", str(s["skipped"]))
    table.add_row("Applied to Contacts", str(s["applied"]))
    table.add_row("Errors / no results", str(s["errors"]))

    console.print(table)


@cli.command("reset-credentials")
def reset_credentials():
    """Delete cached LinkedIn session cookies, forcing re-authentication on next scrape.

    Use this when LinkedIn silently returns empty search results (soft-block).
    Your email/password in the macOS Keychain are not affected.
    """
    from pathlib import Path
    import linkedin_api.settings as li_settings

    cookies_dir = Path(li_settings.COOKIE_PATH)
    deleted = list(cookies_dir.glob("*.jr")) if cookies_dir.exists() else []
    if not deleted:
        console.print("[yellow]No cached LinkedIn session found.[/yellow]")
        return
    for f in deleted:
        f.unlink()
    console.print(f"[green]Deleted {len(deleted)} cached session file(s).[/green]")
    console.print(
        "Re-authentication will happen automatically on the next [bold]scrape[/bold]."
    )


@cli.command()
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
def reset(yes: bool):
    """Delete all DB data and start fresh. This cannot be undone."""
    from .config import DB_PATH
    from .db import init_db

    if not yes:
        click.confirm(
            f"This will delete {DB_PATH} and all stored matches. Continue?",
            abort=True,
        )
    if DB_PATH.exists():
        DB_PATH.unlink()
        console.print(f"[green]Deleted:[/green] {DB_PATH}")
    init_db()
    console.print("[green]Fresh database initialized.[/green]")


@cli.command()
@click.argument("output", default="matches.csv")
def export(output: str):
    """Export all matches to a CSV file (default: matches.csv)."""
    import csv

    from .db import LinkedinMatch, Contact, init_db

    init_db()
    matches = (
        LinkedinMatch.select(LinkedinMatch, Contact)
        .join(Contact)
        .order_by(LinkedinMatch.status, LinkedinMatch.confidence.desc())
    )

    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "contact_name",
                "contact_org",
                "contact_email",
                "linkedin_name",
                "linkedin_url",
                "headline",
                "confidence",
                "status",
                "error",
            ]
        )
        for m in matches:
            writer.writerow(
                [
                    m.contact.full_name,
                    m.contact.organization or "",
                    m.contact.email or "",
                    m.linkedin_name or "",
                    m.linkedin_url or "",
                    m.headline or "",
                    f"{m.confidence:.2f}",
                    m.status,
                    m.error or "",
                ]
            )

    console.print(f"[green]Exported to:[/green] {output}")


if __name__ == "__main__":
    cli()
