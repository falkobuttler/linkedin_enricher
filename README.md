# linkedin-enricher

Enrich Apple Contacts with LinkedIn profile photos and URLs.

## Setup

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install
pip install -e .
```

### macOS Contacts permission

The tool accesses your Contacts via AppleScript. You must grant permission to **Terminal** (or iTerm, whichever you use):

> System Settings → Privacy & Security → Contacts → enable Terminal

### LinkedIn credentials

Stored securely in macOS Keychain. You'll be prompted on first run.

> **Note:** Uses an unofficial LinkedIn API. Use your personal account and keep rate limits conservative to avoid blocks.

---

## Workflow

### 1. Scrape LinkedIn

```bash
linkedin-enricher scrape --limit 50
```

- Exports contacts from Apple Contacts (those without a photo or LinkedIn URL)
- Searches LinkedIn for each, scoring matches by name + company + email domain
- Stores results in `~/.linkedin_enricher/state.db`
- Safe to re-run — already-searched contacts are skipped
- Use `--limit` to process in batches across sessions (avoids LinkedIn rate limits)
- Use `--min-confidence 0.7` to only keep higher-confidence matches

### 2. Review matches

```bash
linkedin-enricher review
```

Opens a browser UI showing all pending matches with:
- Contact photo thumbnail
- Your contact info vs LinkedIn match
- Confidence score (colour-coded bar)
- Approve / Reject / Skip radio buttons

Bulk controls: "Approve high-confidence (≥80%)", "Approve all", "Skip all"

Click **Submit decisions** when done. The server exits automatically.

### 3. Apply to Contacts

```bash
# Preview first
linkedin-enricher apply --dry-run

# Write changes
linkedin-enricher apply
```

For each approved match:
- Sets the contact photo in Contacts.app
- Adds a LinkedIn URL (label: "LinkedIn")

---

## Other commands

```bash
linkedin-enricher status          # Show DB summary counts
linkedin-enricher export          # Export all matches to matches.csv
linkedin-enricher reset           # Wipe DB and start fresh (confirms first)
```

---

## Rate limiting

LinkedIn's unofficial API has limits. The scraper:
- Waits ~7–15 seconds between requests (8 RPM with jitter)
- Pauses 60 seconds every 20 contacts
- ~100 contacts/hour at default settings
- If a CAPTCHA challenge occurs, you'll be prompted to solve it in a browser

---

## Data location

```
~/.linkedin_enricher/
├── state.db        # SQLite database (all contacts, matches, status)
└── photos/         # Downloaded + resized profile photos (512×512 JPEG)
```
