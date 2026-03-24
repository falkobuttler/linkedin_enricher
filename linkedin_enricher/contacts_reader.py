"""Read contacts from Apple Contacts.app via AppleScript."""

import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .db import Contact, db, init_db

_APPLESCRIPT_TEMPLATE = """\
set outFile to open for access POSIX file "{path}" with write permission
tell application "Contacts"
    set allPeople to every person
    repeat with p in allPeople
        try
            set cid to id of p
            set cname to name of p

            set corg to ""
            try
                set corg to organization of p
                if corg is missing value then set corg to ""
            end try

            set cemail to ""
            try
                set eList to value of emails of p
                if (count of eList) > 0 then set cemail to item 1 of eList
            end try

            set clinkedin to ""
            try
                repeat with u in urls of p
                    set uval to value of u
                    if uval contains "linkedin.com" then
                        set clinkedin to uval
                        exit repeat
                    end if
                end repeat
            end try

            set dataLine to cid & tab & cname & tab & corg & tab & cemail & tab & clinkedin & linefeed
            tell me to write dataLine to outFile
        end try
    end repeat
end tell
close access outFile
"""


@dataclass
class ContactRecord:
    id: str
    full_name: str
    organization: Optional[str] = None
    email: Optional[str] = None
    has_photo: bool = False
    linkedin_url: Optional[str] = None


def _run_applescript(script: str) -> None:
    """Write script to a temp file and run via osascript. Raises on error."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".applescript", delete=False, encoding="utf-8"
    ) as f:
        f.write(script)
        script_path = f.name

    try:
        result = subprocess.run(
            ["osascript", script_path],
            capture_output=True,
            text=True,
        )
    finally:
        Path(script_path).unlink(missing_ok=True)

    if result.returncode != 0:
        err = result.stderr.strip()
        if "-1743" in err or (
            "privacy" in err.lower() and "contacts" in err.lower()
        ):
            print(
                "\n[ERROR] Contacts access denied.\n"
                "Please grant Terminal (or your shell) access to Contacts:\n"
                "  System Settings > Privacy & Security > Contacts\n"
                "Then re-run this command.",
                file=sys.stderr,
            )
            sys.exit(1)
        raise RuntimeError(f"AppleScript error: {err}")


def export_contacts() -> list[ContactRecord]:
    """Fetch all contacts from Contacts.app and return parsed records."""
    print("Running AppleScript to export contacts (may take 10–30s for large books)...", flush=True)

    # Write output to a temp file to avoid AppleScript string-concat limits
    with tempfile.NamedTemporaryFile(suffix=".tsv", delete=False) as f:
        tmp_path = f.name

    try:
        script = _APPLESCRIPT_TEMPLATE.format(path=tmp_path)
        _run_applescript(script)

        raw = Path(tmp_path).read_text(encoding="utf-8", errors="replace")
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    print("AppleScript done, parsing output...", flush=True)
    records = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        cid, name, org, email, linkedin = parts[:5]
        cid = cid.strip()
        name = name.strip()
        if not cid or not name:
            continue
        records.append(
            ContactRecord(
                id=cid,
                full_name=name,
                organization=org.strip() or None,
                email=email.strip() or None,
                has_photo=False,
                linkedin_url=linkedin.strip() or None,
            )
        )
    return records


def load_contacts_to_db(only_without_photo: bool = True) -> int:
    """Export contacts and upsert into DB. Returns count of contacts loaded."""
    init_db()
    records = export_contacts()
    loaded = 0
    with db.atomic():
        for r in records:
            # Skip contacts that already have a LinkedIn URL
            if r.linkedin_url:
                continue
            Contact.get_or_create(
                id=r.id,
                defaults={
                    "full_name": r.full_name,
                    "organization": r.organization,
                    "email": r.email,
                    "has_photo": r.has_photo,
                },
            )
            loaded += 1
    return loaded
