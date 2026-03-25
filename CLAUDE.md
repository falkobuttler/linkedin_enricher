# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup & Installation

```bash
# Create and activate virtualenv
python3 -m venv .venv
source .venv/bin/activate

# Install in editable mode
pip install -e .
```

The package installs a `linkedin-enricher` CLI entry point.

## Common Commands

```bash
# Full workflow
linkedin-enricher scrape              # Search LinkedIn for all un-matched contacts
linkedin-enricher scrape -n 20        # Process only 20 contacts (for incremental runs)
linkedin-enricher scrape --retry-errors  # Re-attempt contacts that previously errored
linkedin-enricher review              # Open batch review UI in browser (port 5000)
                                      # Submitting the UI also applies changes immediately

# Utilities
linkedin-enricher status              # Show DB summary table
linkedin-enricher export matches.csv  # Export all matches to CSV
linkedin-enricher reset-credentials  # Delete cached LinkedIn session cookies (fixes soft-blocks)
linkedin-enricher reset --yes         # Wipe DB and start fresh
linkedin-enricher apply               # Manually apply approved matches (retry failures)
linkedin-enricher apply --dry-run     # Preview what would be written
```

## Architecture

The tool runs as a macOS-only pipeline with two user-facing stages: **scrape → review** (apply is now merged into review).

### Data flow

1. **`contacts_reader.py`** — reads Apple Contacts via PyObjC `CNContactStore`. Skips contacts that already have a photo or LinkedIn URL. Upserts into SQLite via `Contact.get_or_create`.

2. **`linkedin_scraper.py`** — authenticates with the unofficial `linkedin-api` library (credentials stored in macOS Keychain via `keyring`). For each un-searched contact it:
   - Filters out non-person contacts via `_should_skip()` (relationship labels, single names without email, company names)
   - Calls `api.search_people()` with up to 3 fallback strategies (name+company → name only → name+email domain)
   - Fetches the full profile via the LinkedIn DASH API (`/voyager/api/identity/dash/profiles/urn:li:fsd_profile:{urn_id}`)
   - Scores candidates with fuzzy name matching + subset check (handles middle names) + company/email signals (0.0–1.0)
   - Downloads and resizes the photo immediately via `image_processor.py`
   - Stores matches in `LinkedinMatch` with `status="pending"`

3. **`review_server.py`** — Flask server. On `GET /` it lazily downloads any photos not yet fetched during scraping, then renders `templates/review.html`. On `POST /approve` it saves decisions to DB and immediately calls `apply_approved_matches()` — no separate apply step needed.

4. **`contacts_writer.py`** — reads `status="approved"` matches and writes back via PyObjC `CNSaveRequest`. Falls back to AppleScript (`osascript`) for contacts failing with Core Data fault error 134092.

### State machine

`LinkedinMatch.status`: `pending` → `approved` / `rejected` / `skipped` → `applied` (or `error`)

Skipped-by-heuristic contacts also get `status="skipped"` so they are never re-queued.

### Persistent state

All state lives in `~/.linkedin_enricher/`:
- `state.db` — SQLite database (WAL mode)
- `photos/` — downloaded+resized JPEG thumbnails (512×512), named `{contact_id_sanitized}.jpg`

LinkedIn session cookies are cached by `linkedin-api` at `~/.linkedin_api/cookies/{email}.jr`. `reset-credentials` deletes these to force re-authentication.

### Key constraints

- **macOS only**: uses PyObjC `Contacts` framework and macOS Keychain
- **LinkedIn rate limiting**: token bucket at 8 RPM + 60s pause every 20 contacts; LinkedIn may trigger CAPTCHA challenges (handled interactively)
- **LinkedIn soft-blocks**: when LinkedIn silently returns empty search results for all contacts, the session is soft-blocked — run `reset-credentials`, wait 30–60 minutes, then retry with `--retry-errors`
- **`lxml` must stay at 5.x**: `linkedin-api` requires `lxml<6.0.0`
- **Confidence threshold**: default 0.40 (a perfect name-only match scores 0.50; middle-name-only differences are detected via token subset check and score 0.95)
- **Photo key**: fetch `CNContactImageDataAvailableKey` (boolean), never `CNContactImageDataKey`, to avoid loading all image bytes during the contact scan

### Templates

`templates/` is outside the `linkedin_enricher/` package. `review_server.py` resolves the path via `Path(__file__).parent.parent / "templates"`.
