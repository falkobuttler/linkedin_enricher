"""Write approved LinkedIn data back to Apple Contacts.app via AppleScript."""

import subprocess
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console

from .db import LinkedinMatch, db, get_approved_matches


def _run_applescript(script: str) -> bool:
    """Run AppleScript; return True on success."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        print(f"  [AppleScript error] {err}", file=sys.stderr)
        return False
    return True


def _set_photo(contact_id: str, photo_path: str) -> bool:
    script = f"""
tell application "Contacts"
    set p to (first person whose id is "{contact_id}")
    set image of p to (read posix file "{photo_path}" as JPEG picture)
end tell
"""
    return _run_applescript(script)


def _add_linkedin_url(contact_id: str, linkedin_url: str) -> bool:
    # Escape any double quotes in the URL (shouldn't happen but be safe)
    safe_url = linkedin_url.replace('"', '\\"')
    script = f"""
tell application "Contacts"
    set p to (first person whose id is "{contact_id}")
    -- Check if LinkedIn URL already exists
    set hasLinkedIn to false
    repeat with u in urls of p
        if value of u contains "linkedin.com" then
            set hasLinkedIn to true
            exit repeat
        end if
    end repeat
    if not hasLinkedIn then
        make new url at end of urls of p with properties {{label:"LinkedIn", value:"{safe_url}"}}
    end if
end tell
"""
    return _run_applescript(script)


def _save_contacts() -> bool:
    return _run_applescript('tell application "Contacts" to save')


def apply_approved_matches(
    dry_run: bool = False,
    contact_id_filter: Optional[str] = None,
    console: Optional[Console] = None,
) -> int:
    """
    Write approved LinkedIn data to Apple Contacts.
    Returns number of contacts successfully updated.
    """
    if console is None:
        console = Console()

    matches = list(get_approved_matches())
    if contact_id_filter:
        matches = [m for m in matches if m.contact.id == contact_id_filter]

    if not matches:
        console.print("[yellow]No approved matches to apply.[/yellow]")
        return 0

    console.print(f"[green]Approved matches to apply:[/green] {len(matches)}")
    if dry_run:
        console.print("[yellow]DRY RUN – no changes will be made.[/yellow]\n")

    updated = 0
    failed = 0

    for m in matches:
        contact = m.contact
        label = f"[cyan]{contact.full_name}[/cyan]"

        if dry_run:
            actions = []
            if m.photo_local and Path(m.photo_local).exists():
                actions.append("set photo")
            if m.linkedin_url:
                actions.append(f"add LinkedIn URL ({m.linkedin_url})")
            console.print(f"  {label}: {', '.join(actions) if actions else 'nothing to do'}")
            updated += 1
            continue

        contact_ok = True

        # Set photo
        if m.photo_local:
            photo_path = Path(m.photo_local)
            if photo_path.exists():
                ok = _set_photo(contact.id, str(photo_path))
                if ok:
                    console.print(f"  {label}: photo set ✓")
                else:
                    console.print(f"  {label}: [red]photo failed[/red]")
                    contact_ok = False
            else:
                console.print(f"  {label}: [yellow]photo file missing, skipping photo[/yellow]")

        # Add LinkedIn URL
        if m.linkedin_url:
            ok = _add_linkedin_url(contact.id, m.linkedin_url)
            if ok:
                console.print(f"  {label}: LinkedIn URL added ✓")
            else:
                console.print(f"  {label}: [red]URL failed[/red]")
                contact_ok = False

        # Save after each contact so failures don't roll back others
        _save_contacts()

        if contact_ok:
            with db.atomic():
                m.status = "applied"
                m.save()
            updated += 1
        else:
            failed += 1

    if not dry_run:
        console.print(
            f"\n[green]Done.[/green] Applied: {updated}, Failed: {failed}"
        )

    return updated
