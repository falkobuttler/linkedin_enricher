"""Search LinkedIn for contacts and store best matches in DB."""

import getpass
import random
import sys
import time
from dataclasses import dataclass
from typing import Optional

import keyring
from thefuzz import fuzz

from .config import BATCH_PAUSE_SECONDS, BATCH_SIZE, RATE_LIMIT_RPM
from .db import Contact, LinkedinMatch, db

KEYRING_SERVICE = "linkedin_enricher"
KEYRING_EMAIL_KEY = "email"
KEYRING_PASS_KEY = "password"

# Words that indicate a relationship label rather than a real person's name
_RELATIONSHIP_WORDS = {
    "mom",
    "dad",
    "mother",
    "father",
    "sister",
    "brother",
    "wife",
    "husband",
    "son",
    "daughter",
    "aunt",
    "uncle",
    "grandma",
    "grandpa",
    "grandmother",
    "grandfather",
    "friend",
    "boss",
    "neighbor",
    "neighbour",
}

# Suffixes that indicate a company entry rather than a person
_COMPANY_SUFFIXES = {
    "inc",
    "inc.",
    "llc",
    "llc.",
    "ltd",
    "ltd.",
    "corp",
    "corp.",
    "co.",
    "company",
    "group",
    "services",
    "solutions",
    "consulting",
    "consulting.",
    "support",
    "auto",
    "body",
    "shop",
    "store",
    "restaurant",
    "cafe",
}


def _should_skip(contact: Contact) -> Optional[str]:
    """Return a skip reason if the contact is not a real LinkedIn person, else None."""
    name = contact.full_name.strip()
    # Normalise curly apostrophe → straight so endswith checks are consistent
    name_lower = name.lower().replace("\u2019", "'")
    words = name_lower.split()

    # Any name ending in a relationship word: "John's Mom", "Anna - Lisas Dad", "Mom"
    if words[-1] in _RELATIONSHIP_WORDS:
        return f"relationship label: {name!r}"

    # Name identical to organization (company entered as a contact)
    if contact.organization and name_lower == contact.organization.strip().lower():
        return f"name matches organization: {name!r}"

    # Name ends with a company suffix
    if words and words[-1] in _COMPANY_SUFFIXES:
        return f"looks like a company: {name!r}"

    # Single name with no email to disambiguate (org alone is too vague)
    if len(words) == 1 and not contact.email:
        return f"single name with no email: {name!r}"

    return None


@dataclass
class MatchCandidate:
    linkedin_url: str
    linkedin_name: str
    urn_id: str
    headline: str
    current_title: Optional[str]
    current_company: Optional[str]
    photo_url: Optional[str]
    confidence: float


class _TokenBucket:
    def __init__(self, rpm: int):
        self._min_interval = 60.0 / rpm
        self._last = 0.0

    def acquire(self):
        now = time.monotonic()
        wait = self._min_interval - (now - self._last)
        if wait > 0:
            jitter = random.uniform(0.3, 1.5)
            time.sleep(wait + jitter)
        self._last = time.monotonic()


def setup_credentials() -> tuple[str, str]:
    """Return (email, password) from keychain, prompting if not set."""
    print("Checking macOS Keychain for LinkedIn credentials...", flush=True)
    email = keyring.get_password(KEYRING_SERVICE, KEYRING_EMAIL_KEY)
    password = keyring.get_password(KEYRING_SERVICE, KEYRING_PASS_KEY)
    if not email or not password:
        print(
            "Credentials not found.\n"
            "Note: macOS may show a Keychain dialog — check behind other windows."
        )
        email = input("LinkedIn email: ").strip()
        password = getpass.getpass("LinkedIn password: ")
        keyring.set_password(KEYRING_SERVICE, KEYRING_EMAIL_KEY, email)
        keyring.set_password(KEYRING_SERVICE, KEYRING_PASS_KEY, password)
        print("Credentials saved to macOS Keychain.")
    else:
        print(f"Found credentials for {email}.", flush=True)
    return email, password


def _get_linkedin_client():
    import socket
    from linkedin_api import Linkedin  # lazy import to allow --help without auth

    email, password = setup_credentials()
    print(
        "Authenticating with LinkedIn (this makes a few HTTP requests)...", flush=True
    )
    # The linkedin-api library has no request timeout; set a socket-level default
    # so auth doesn't hang indefinitely if LinkedIn blocks/throttles.
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(30)
    try:
        client = Linkedin(email, password)
    finally:
        socket.setdefaulttimeout(old_timeout)
    print("Authenticated.", flush=True)
    return client


_DASH_PROFILE_URL = (
    "https://www.linkedin.com/voyager/api/identity/dash/profiles"
    "/urn:li:fsd_profile:{urn_id}"
    "?decorationId=com.linkedin.voyager.dash.deco.identity.profile"
    ".FullProfileWithEntities-93"
)


def _fetch_dash_profile(api, urn_id: str) -> dict:
    """Fetch a profile via the DASH API (replaces deprecated profileView endpoint)."""
    url = _DASH_PROFILE_URL.format(urn_id=urn_id)
    resp = api.client.session.get(url)
    if resp.status_code != 200:
        return {}
    return resp.json()


def _fetch_dash_profile_by_public_id(api, public_id: str) -> dict:
    """Fetch a DASH profile using the LinkedIn public identifier (vanity URL slug)."""
    decoration = (
        "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-93"
    )
    # Try vanityName query (more reliable than memberIdentity for public profiles)
    for q, param in [("vanityName", "vanityName"), ("memberIdentity", "memberIdentity")]:
        url = (
            "https://www.linkedin.com/voyager/api/identity/dash/profiles"
            f"?q={q}&{param}={public_id}"
            f"&decorationId={decoration}"
        )
        resp = api.client.session.get(url)
        data = resp.json() if resp.status_code == 200 else {}
        # LinkedIn returns {"status": N} for errors even on HTTP 200
        if data.get("status"):
            continue
        elements = data.get("elements", [])
        if elements:
            return elements[0]
        included = data.get("included", [])
        for item in included:
            if item.get("publicIdentifier") == public_id:
                return item
    return {}


def _extract_current_position(profile: dict) -> tuple:
    """Return (current_title, current_company) from a DASH profile, or (None, None)."""

    def _is_current(pos: dict) -> bool:
        """True if the position has no end date (i.e. it's a current role)."""
        for tp_key in ("timePeriod", "dateRange"):
            tp = pos.get(tp_key) or {}
            end = tp.get("endDate") or tp.get("end")
            if end is not None:
                return False
        return True

    def _company_name(pos: dict, group: dict = None) -> Optional[str]:
        name = pos.get("companyName")
        if not name:
            name = (pos.get("company") or {}).get("name")
        if not name and group:
            name = group.get("companyName")
            if not name:
                name = (
                    (group.get("profilePositionInPositionGroup") or {})
                    .get("companyName")
                )
        return name or None

    # Flat list: some library versions expose experience/positions directly
    for key in ("experience", "positions"):
        positions = profile.get(key)
        if not isinstance(positions, list):
            continue
        for pos in positions:
            if not _is_current(pos):
                continue
            title = pos.get("title") or pos.get("roleName")
            company = _company_name(pos)
            if title or company:
                return title, company

    # LinkedIn DASH API: profilePositionGroups → elements → profilePositionInPositionGroup → elements
    pg = profile.get("profilePositionGroups") or profile.get("positionGroups") or {}
    elements = pg.get("elements", []) if isinstance(pg, dict) else (pg if isinstance(pg, list) else [])
    for group in elements:
        pos_container = group.get("profilePositionInPositionGroup") or group.get("positions") or {}
        pos_list = (
            pos_container.get("elements", [])
            if isinstance(pos_container, dict)
            else (pos_container if isinstance(pos_container, list) else [])
        )
        for pos in pos_list:
            if not _is_current(pos):
                continue
            title = pos.get("title") or pos.get("roleName")
            company = _company_name(pos, group)
            if title or company:
                return title, company

    return None, None


def _extract_photo_url(profile: dict) -> Optional[str]:
    """Extract the largest available photo URL from a DASH profile dict."""
    pic = profile.get("profilePicture", {}) or {}
    vi = (pic.get("displayImageReference", {}) or {}).get("vectorImage", {}) or {}
    root_url = vi.get("rootUrl", "")
    artifacts = vi.get("artifacts", [])
    # Artifacts are ordered smallest → largest; take the last one
    for art in reversed(artifacts):
        seg = art.get("fileIdentifyingUrlPathSegment", "")
        if root_url and seg:
            return root_url + seg
    return None


def _score_candidate(contact: Contact, result: dict, profile: dict) -> float:
    """
    Score a LinkedIn candidate against a contact (0.0–1.0).
    result  = search_people row  (keys: name, jobtitle, location, urn_id)
    profile = DASH profile dict  (keys: firstName, lastName, headline, …)
    """
    score = 0.0

    # Name similarity (weight: 0.5)
    # search result 'name' is already the full display name
    result_name = (
        profile.get("firstName", "") + " " + profile.get("lastName", "")
    ).strip() or result.get("name", "")
    # Strip credentials appended after a comma ("Jane Smith, PhD, MBA" → "Jane Smith")
    result_name_clean = result_name.split(",")[0].strip()
    # Normalise: lowercase, strip diacritics (ñ→n, é→e, ó→o …), remove apostrophes
    def _norm(s: str) -> str:
        import unicodedata
        nfkd = unicodedata.normalize("NFKD", s.lower())
        ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
        return ascii_only.replace("'", "").replace("\u2019", "")

    contact_words = set(_norm(contact.full_name).split())
    result_words = set(_norm(result_name_clean).split())
    contact_tokens = _norm(contact.full_name).split()
    result_tokens = _norm(result_name_clean).split()
    contact_first = contact_tokens[0] if contact_tokens else ""
    result_first = result_tokens[0] if result_tokens else ""
    contact_last = contact_tokens[-1] if contact_tokens else ""
    result_last = result_tokens[-1] if result_tokens else ""

    if contact_words and contact_words.issubset(result_words):
        # All contact name tokens found in result — middle name / suffix difference only
        name_ratio = 0.95
    elif (
        contact_last == result_last
        and contact_last != contact_first  # at least two tokens
        and (
            contact_first.startswith(result_first)
            or result_first.startswith(contact_first)
        )
    ):
        # Last name matches exactly and first name is a prefix of the other
        # e.g. "Bob Smith" vs "Robert Smith"
        name_ratio = 0.92
    else:
        name_ratio = (
            fuzz.token_sort_ratio(_norm(contact.full_name), _norm(result_name_clean))
            / 100.0
        )
    if name_ratio >= 0.85:
        score += 0.5
    elif name_ratio >= 0.70:
        score += 0.3
    elif name_ratio >= 0.55:
        score += 0.1

    # Company match (weight: 0.3) — check headline, jobtitle, and full profile text
    if contact.organization:
        org_lower = contact.organization.lower()
        headline = (profile.get("headline", "") or result.get("jobtitle", "") or "").lower()
        profile_text = str(profile).lower()
        if org_lower in headline:
            score += 0.3
        elif org_lower in profile_text:
            score += 0.2
        elif fuzz.partial_ratio(org_lower, headline) >= 70:
            score += 0.15

    # Email domain match (weight: 0.2)
    if contact.email and "@" in contact.email:
        domain = contact.email.split("@")[1].lower()
        generic = {
            "gmail.com",
            "yahoo.com",
            "hotmail.com",
            "outlook.com",
            "icloud.com",
            "me.com",
        }
        if domain not in generic:
            profile_str = str(profile).lower()
            if domain in profile_str:
                score += 0.2

    return min(score, 1.0)


def search_contact(
    api, contact: Contact, min_confidence: float = 0.60, console=None
) -> Optional[MatchCandidate]:
    """Search LinkedIn for a contact and return the best match above threshold."""
    from rich.console import Console

    if console is None:
        console = Console()

    def _search(keywords: str) -> list:
        try:
            raw = api.search_people(keywords=keywords, limit=5)
            console.print(f"    [dim]api.search_people({keywords!r}) → {raw}[/dim]")
            return raw or []
        except Exception as exc:
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc

    # Strategy 1: name + company
    keywords_used = contact.full_name
    if contact.organization:
        keywords_used += f" {contact.organization}"
    results = _search(keywords_used)

    # Strategy 2: name only (company may confuse the API)
    if not results and contact.organization:
        keywords_used = contact.full_name
        results = _search(keywords_used)

    # Strategy 3: name + email domain
    if not results and contact.email and "@" in contact.email:
        domain = contact.email.split("@")[1]
        keywords_used = f"{contact.full_name} {domain}"
        try:
            results = _search(keywords_used)
        except Exception:
            results = []

    if not results:
        console.print(f"    [dim]no search results for: {contact.full_name!r}[/dim]")
        return None

    console.print(
        f"    [dim]found {len(results)} result(s) via: {keywords_used!r}[/dim]"
    )

    best: Optional[MatchCandidate] = None
    for r in results:
        urn_id = r.get("urn_id")
        if not urn_id:
            continue

        # Fetch full DASH profile (needed for publicIdentifier, photo, headline)
        try:
            profile = _fetch_dash_profile(api, urn_id)
        except Exception as exc:
            console.print(
                f"    [dim]DASH fetch failed for urn:{urn_id}:"
                f" {type(exc).__name__}: {exc}[/dim]"
            )
            profile = {}

        public_id = profile.get("publicIdentifier")
        if not public_id:
            console.print(
                f"    [dim]no publicIdentifier for urn:{urn_id}"
                f" (name={r.get('name', '?')})[/dim]"
            )
            continue

        score = _score_candidate(contact, r, profile)
        result_name = (
            profile.get("firstName", "") + " " + profile.get("lastName", "")
        ).strip() or r.get("name", "")
        console.print(
            f"    [dim]candidate: {result_name!r}  score={score:.2f}"
            f"  threshold={min_confidence:.2f}[/dim]"
        )

        if score < min_confidence:
            continue

        photo_url = _extract_photo_url(profile)
        current_title, current_company = _extract_current_position(profile)
        linkedin_url = f"https://www.linkedin.com/in/{public_id}"
        headline = profile.get("headline") or r.get("jobtitle", "")

        candidate = MatchCandidate(
            linkedin_url=linkedin_url,
            linkedin_name=result_name,
            urn_id=urn_id,
            headline=headline or "",
            current_title=current_title,
            current_company=current_company,
            photo_url=photo_url,
            confidence=score,
        )
        if best is None or score > best.confidence:
            best = candidate

    return best


def scrape_all(
    limit: Optional[int] = None,
    min_confidence: float = 0.60,
    retry_errors: bool = False,
    console=None,
) -> int:
    """
    Scrape LinkedIn for all un-searched contacts in DB.
    Returns number of contacts processed.
    """
    from rich.console import Console
    from rich.progress import (
        Progress,
        SpinnerColumn,
        TextColumn,
        BarColumn,
        TaskProgressColumn,
    )

    if console is None:
        console = Console()

    if retry_errors:
        # Delete existing error records so those contacts are re-queued
        deleted = (
            LinkedinMatch.delete().where(LinkedinMatch.status == "error").execute()
        )
        if deleted:
            console.print(
                f"[yellow]Cleared {deleted} previous error(s) for retry.[/yellow]"
            )

    # Find contacts not yet searched (no LinkedinMatch row)
    searched_ids = {
        m.contact_id for m in LinkedinMatch.select(LinkedinMatch.contact_id)
    }
    contacts_qs = Contact.select().where(Contact.id.not_in(searched_ids))
    if limit:
        contacts_qs = contacts_qs.limit(limit)

    contacts = list(contacts_qs)
    if not contacts:
        console.print("[yellow]No un-searched contacts found.[/yellow]")
        return 0

    console.print(f"[green]Contacts to search:[/green] {len(contacts)}")

    try:
        api = _get_linkedin_client()
    except Exception as exc:
        console.print(f"[red]LinkedIn login failed: {exc}[/red]")
        sys.exit(1)

    bucket = _TokenBucket(RATE_LIMIT_RPM)
    processed = 0
    errors = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Searching LinkedIn...", total=len(contacts))

        for i, contact in enumerate(contacts):
            progress.update(
                task,
                description=f"[cyan]{contact.full_name}[/cyan]",
                advance=1,
            )

            skip_reason = _should_skip(contact)
            if skip_reason:
                console.print(
                    f"    [dim]skipping {contact.full_name}: {skip_reason}[/dim]"
                )
                with db.atomic():
                    LinkedinMatch.create(
                        contact=contact, status="skipped", error=skip_reason
                    )
                processed += 1
                continue

            bucket.acquire()

            try:
                match = search_contact(api, contact, min_confidence, console=console)
            except Exception as exc:
                err_str = str(exc)
                console.print(f"[red]✗ {contact.full_name}: {err_str}[/red]")

                # Handle LinkedIn challenge / CAPTCHA
                if "challenge" in err_str.lower() or "captcha" in err_str.lower():
                    console.print(
                        "[yellow]LinkedIn challenge detected. "
                        "Please open https://www.linkedin.com in your browser, "
                        "solve any CAPTCHA, then press Enter to continue...[/yellow]"
                    )
                    input()
                    # Reinitialize client after user solved challenge
                    try:
                        api = _get_linkedin_client()
                    except Exception:
                        pass
                    continue

                with db.atomic():
                    LinkedinMatch.create(
                        contact=contact,
                        status="error",
                        error=err_str[:500],
                    )
                errors += 1
                processed += 1
                continue

            # Download photo outside the DB transaction
            photo_local = None
            if match and match.photo_url:
                from .image_processor import download_and_resize

                local = download_and_resize(match.photo_url, contact.id)
                if local:
                    photo_local = str(local)

            with db.atomic():
                if match:
                    LinkedinMatch.create(
                        contact=contact,
                        linkedin_url=match.linkedin_url,
                        linkedin_name=match.linkedin_name,
                        urn_id=match.urn_id,
                        headline=match.headline,
                        current_title=match.current_title,
                        current_company=match.current_company,
                        photo_url=match.photo_url,
                        photo_local=photo_local,
                        confidence=match.confidence,
                        status="pending",
                    )
                else:
                    LinkedinMatch.create(
                        contact=contact,
                        status="error",
                        error="no_results",
                    )

            processed += 1

            # Batch pause every BATCH_SIZE requests
            if (i + 1) % BATCH_SIZE == 0 and (i + 1) < len(contacts):
                progress.update(
                    task,
                    description=f"[yellow]Pausing {BATCH_PAUSE_SECONDS}s...[/yellow]",
                )
                time.sleep(BATCH_PAUSE_SECONDS)

    console.print(
        f"[green]Done.[/green] Processed {processed} contacts, {errors} errors."
    )
    return processed
