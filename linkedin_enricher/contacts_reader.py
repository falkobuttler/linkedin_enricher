"""Read contacts from Apple Contacts.app via the native Contacts framework (PyObjC)."""

import sys
from dataclasses import dataclass
from typing import Optional

import Contacts as CN

from .db import Contact, db, init_db


@dataclass
class ContactRecord:
    id: str
    full_name: str
    organization: Optional[str] = None
    email: Optional[str] = None
    has_photo: bool = False
    linkedin_url: Optional[str] = None


def _ensure_access() -> None:
    """Check Contacts authorization; exit with a clear message if denied."""
    status = CN.CNContactStore.authorizationStatusForEntityType_(
        CN.CNEntityTypeContacts
    )
    if status == CN.CNAuthorizationStatusAuthorized:
        return
    if status == CN.CNAuthorizationStatusNotDetermined:
        store = CN.CNContactStore.alloc().init()
        result = [None]

        def handler(granted, error):
            result[0] = granted

        store.requestAccessForEntityType_completionHandler_(
            CN.CNEntityTypeContacts, handler
        )
        # Spin briefly while the system shows the permission dialog
        import time

        for _ in range(300):
            if result[0] is not None:
                break
            time.sleep(0.1)
        if result[0]:
            return
    print(
        "\n[ERROR] Contacts access denied.\n"
        "Please grant Terminal access to Contacts:\n"
        "  System Settings > Privacy & Security > Contacts\n"
        "Then re-run this command.",
        file=sys.stderr,
    )
    sys.exit(1)


_KEYS_TO_FETCH = [
    CN.CNContactIdentifierKey,
    CN.CNContactGivenNameKey,
    CN.CNContactFamilyNameKey,
    CN.CNContactOrganizationNameKey,
    CN.CNContactEmailAddressesKey,
    CN.CNContactUrlAddressesKey,
    CN.CNContactImageDataAvailableKey,  # boolean — does NOT load image bytes
]


def export_contacts() -> list[ContactRecord]:
    """Fetch all contacts from the Contacts framework and return parsed records."""
    _ensure_access()
    print("Fetching contacts via Contacts framework...", flush=True)

    store = CN.CNContactStore.alloc().init()
    fetch_request = CN.CNContactFetchRequest.alloc().initWithKeysToFetch_(
        _KEYS_TO_FETCH
    )
    fetch_request.setUnifyResults_(True)

    records = []

    def handler(contact, stop):
        given = contact.givenName() or ""
        family = contact.familyName() or ""
        name = f"{given} {family}".strip() or contact.organizationName() or ""
        if not name:
            return

        org = contact.organizationName() or None

        email = None
        for lv in contact.emailAddresses():
            email = str(lv.value())
            break

        linkedin_url = None
        for lv in contact.urlAddresses():
            url = str(lv.value())
            if "linkedin.com" in url.lower():
                linkedin_url = url
                break

        records.append(
            ContactRecord(
                id=str(contact.identifier()),
                full_name=name,
                organization=org,
                email=email,
                has_photo=bool(contact.imageDataAvailable()),
                linkedin_url=linkedin_url,
            )
        )

    success, error = store.enumerateContactsWithFetchRequest_error_usingBlock_(
        fetch_request, None, handler
    )
    if error:
        raise RuntimeError(f"Contacts fetch error: {error}")

    print(f"Fetched {len(records)} contacts.", flush=True)
    return records


def load_contacts_to_db(only_without_photo: bool = True) -> int:
    """Export contacts and upsert into DB. Returns count of contacts loaded."""
    init_db()
    records = export_contacts()
    loaded = 0
    with db.atomic():
        for r in records:
            if r.linkedin_url:
                continue
            if only_without_photo and r.has_photo:
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
