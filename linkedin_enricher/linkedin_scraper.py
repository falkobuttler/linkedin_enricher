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


@dataclass
class MatchCandidate:
    linkedin_url: str
    linkedin_name: str
    headline: str
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
            "Note: macOS may show a Keychain access dialog — check behind other windows."
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
    print("Authenticating with LinkedIn (this makes a few HTTP requests)...", flush=True)
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
    "?decorationId=com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-93"
)


def _fetch_dash_profile(api, urn_id: str) -> dict:
    """Fetch a profile via the DASH API (replaces deprecated profileView endpoint)."""
    url = _DASH_PROFILE_URL.format(urn_id=urn_id)
    resp = api.client.session.get(url)
    if resp.status_code != 200:
        return {}
    return resp.json()


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
    name_ratio = fuzz.token_sort_ratio(contact.full_name, result_name) / 100.0
    if name_ratio >= 0.85:
        score += 0.5
    elif name_ratio >= 0.70:
        score += 0.3
    elif name_ratio >= 0.55:
        score += 0.1

    # Company / headline match (weight: 0.3)
    if contact.organization:
        headline = profile.get("headline", "") or result.get("jobtitle", "") or ""
        org_lower = contact.organization.lower()
        if org_lower in headline.lower():
            score += 0.3
        elif fuzz.partial_ratio(org_lower, headline.lower()) >= 70:
            score += 0.15

    # Email domain match (weight: 0.2)
    if contact.email and "@" in contact.email:
        domain = contact.email.split("@")[1].lower()
        generic = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "me.com"}
        if domain not in generic:
            profile_str = str(profile).lower()
            if domain in profile_str:
                score += 0.2

    return min(score, 1.0)


def search_contact(api, contact: Contact, min_confidence: float = 0.60) -> Optional[MatchCandidate]:
    """Search LinkedIn for a contact and return the best match above threshold."""
    keywords = contact.full_name
    if contact.organization:
        keywords += f" {contact.organization}"

    try:
        results = api.search_people(keywords=keywords, limit=5)
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    if not results:
        # Fallback: name + email domain
        if contact.email and "@" in contact.email:
            domain = contact.email.split("@")[1]
            try:
                results = api.search_people(
                    keywords=f"{contact.full_name} {domain}", limit=3
                )
            except Exception:
                results = []

    best: Optional[MatchCandidate] = None
    for r in results:
        urn_id = r.get("urn_id")
        if not urn_id:
            continue

        # Fetch full DASH profile (needed for publicIdentifier, photo, headline)
        try:
            profile = _fetch_dash_profile(api, urn_id)
        except Exception:
            profile = {}

        score = _score_candidate(contact, r, profile)
        if score < min_confidence:
            continue

        public_id = profile.get("publicIdentifier")
        if not public_id:
            continue

        photo_url = _extract_photo_url(profile)
        linkedin_url = f"https://www.linkedin.com/in/{public_id}"
        result_name = (
            profile.get("firstName", "") + " " + profile.get("lastName", "")
        ).strip() or r.get("name", "")
        headline = profile.get("headline") or r.get("jobtitle", "")

        candidate = MatchCandidate(
            linkedin_url=linkedin_url,
            linkedin_name=result_name,
            headline=headline or "",
            photo_url=photo_url,
            confidence=score,
        )
        if best is None or score > best.confidence:
            best = candidate

    return best


def scrape_all(
    limit: Optional[int] = None,
    min_confidence: float = 0.60,
    console=None,
) -> int:
    """
    Scrape LinkedIn for all un-searched contacts in DB.
    Returns number of contacts processed.
    """
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

    if console is None:
        console = Console()

    # Find contacts not yet searched (no LinkedinMatch row)
    searched_ids = {m.contact_id for m in LinkedinMatch.select(LinkedinMatch.contact_id)}
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

            bucket.acquire()

            try:
                match = search_contact(api, contact, min_confidence)
            except Exception as exc:
                err_str = str(exc)
                console.print(f"[red]Error for {contact.full_name}: {err_str}[/red]")

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

            with db.atomic():
                if match:
                    LinkedinMatch.create(
                        contact=contact,
                        linkedin_url=match.linkedin_url,
                        linkedin_name=match.linkedin_name,
                        headline=match.headline,
                        photo_url=match.photo_url,
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
                progress.update(task, description=f"[yellow]Pausing {BATCH_PAUSE_SECONDS}s...[/yellow]")
                time.sleep(BATCH_PAUSE_SECONDS)

    console.print(
        f"[green]Done.[/green] Processed {processed} contacts, {errors} errors."
    )
    return processed
