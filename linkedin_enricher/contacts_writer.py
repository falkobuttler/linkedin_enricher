"""Write approved LinkedIn data back to Apple Contacts.app via the native Contacts framework."""

from pathlib import Path
from typing import Optional

import Contacts as CN
import Foundation
from rich.console import Console

from .db import LinkedinMatch, db, get_approved_matches


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
        contact_ok = True

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
                    contact_ok = False
            else:
                console.print(f"  {label}: [yellow]photo file missing, skipping photo[/yellow]")

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
            console.print(f"  {label}: [red]save failed: {error}[/red]")
            failed += 1

    if not dry_run:
        console.print(f"\n[green]Done.[/green] Applied: {updated}, Failed: {failed}")

    return updated
