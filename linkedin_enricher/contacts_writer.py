"""Write approved LinkedIn data back to Apple Contacts.app via the native framework."""

import subprocess
from pathlib import Path
from typing import Optional

import Contacts as CN
import Foundation
from rich.console import Console

from .db import db, get_approved_matches


def _applescript_apply(
    contact_id: str, linkedin_url: Optional[str], photo_path: Optional[str]
) -> Optional[str]:
    """
    Fallback writer using AppleScript (Apple Events), bypassing Core Data.
    Returns None on success, error string on failure.
    """
    script_parts = [
        f'tell application "Contacts"\n'
        f'set p to (first person whose id is "{contact_id}")'
    ]

    if photo_path and Path(photo_path).exists():
        script_parts.append(
            f'set image of p to (read posix file "{photo_path}" as JPEG picture)'
        )

    if linkedin_url:
        safe_url = linkedin_url.replace('"', '\\"')
        script_parts.append(
            f"set hasLinkedIn to false\n"
            f"repeat with u in urls of p\n"
            f'    if value of u contains "linkedin.com" then\n'
            f"        set hasLinkedIn to true\n"
            f"        exit repeat\n"
            f"    end if\n"
            f"end repeat\n"
            f"if not hasLinkedIn then\n"
            f"    make new url at end of urls of p"
            f' with properties {{label:"LinkedIn", value:"{safe_url}"}}\n'
            f"end if"
        )

    script_parts.append("save\nend tell")
    script = "\n".join(script_parts)

    with __import__("tempfile").NamedTemporaryFile(
        mode="w", suffix=".applescript", delete=False
    ) as f:
        f.write(script)
        script_path = f.name

    try:
        result = subprocess.run(
            ["osascript", script_path], capture_output=True, text=True
        )
    finally:
        Path(script_path).unlink(missing_ok=True)

    if result.returncode != 0:
        return result.stderr.strip() or "unknown AppleScript error"
    return None


def apply_approved_matches(
    dry_run: bool = False,
    contact_id_filter: Optional[str] = None,
    console: Optional[Console] = None,
) -> int:
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

    store = CN.CNContactStore.alloc().init()
    keys_needed = [
        CN.CNContactUrlAddressesKey,
        CN.CNContactImageDataKey,
    ]

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
            console.print(f"  {label}: {', '.join(actions) or 'nothing to do'}")
            updated += 1
            continue

        # Fetch the mutable contact
        cn_contact, error = store.unifiedContactWithIdentifier_keysToFetch_error_(
            contact.id, keys_needed, None
        )
        if error or cn_contact is None:
            console.print(f"  {label}: [red]could not fetch contact ({error})[/red]")
            failed += 1
            continue

        mutable = cn_contact.mutableCopy()

        # Set photo
        if m.photo_local:
            photo_path = Path(m.photo_local)
            if photo_path.exists():
                ns_data = Foundation.NSData.dataWithContentsOfFile_(str(photo_path))
                if ns_data:
                    mutable.setImageData_(ns_data)
                    console.print(f"  {label}: photo set ✓")
                else:
                    console.print(f"  {label}: [red]could not read photo file[/red]")
                    pass  # continue — URL may still be applied
            else:
                console.print(
                    f"  {label}: [yellow]photo file missing, skipping photo[/yellow]"
                )

        # Add LinkedIn URL (skip if already present)
        if m.linkedin_url:
            existing_urls = list(mutable.urlAddresses() or [])
            already_has = any(
                "linkedin.com" in str(lv.value()).lower() for lv in existing_urls
            )
            if not already_has:
                new_lv = CN.CNLabeledValue.labeledValueWithLabel_value_(
                    "LinkedIn", m.linkedin_url
                )
                mutable.setUrlAddresses_(existing_urls + [new_lv])
                console.print(f"  {label}: LinkedIn URL added ✓")

        # Save
        save_request = CN.CNSaveRequest.alloc().init()
        save_request.updateContact_(mutable)
        success, error = store.executeSaveRequest_error_(save_request, None)

        if success:
            with db.atomic():
                m.status = "applied"
                m.save()
            updated += 1
        else:
            # Error 134092 = Core Data faulting failure on the contact's backing store.
            # Fall back to AppleScript which uses Apple Events instead of Core Data.
            error_code = error.code() if error else 0
            if error_code == 134092:
                console.print(
                    f"  {label}: [yellow]PyObjC save failed (Core Data fault),"
                    " trying AppleScript fallback...[/yellow]"
                )
                as_err = _applescript_apply(
                    contact.id,
                    m.linkedin_url if m.linkedin_url else None,
                    m.photo_local if m.photo_local else None,
                )
                if as_err is None:
                    console.print(f"  {label}: applied via AppleScript fallback ✓")
                    with db.atomic():
                        m.status = "applied"
                        m.save()
                    updated += 1
                else:
                    console.print(
                        f"  {label}: [red]AppleScript fallback also failed:"
                        f" {as_err}[/red]"
                    )
                    failed += 1
            else:
                console.print(f"  {label}: [red]save failed: {error}[/red]")
                failed += 1

    if not dry_run:
        console.print(f"\n[green]Done.[/green] Applied: {updated}, Failed: {failed}")

    return updated
