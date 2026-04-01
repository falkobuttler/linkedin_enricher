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

Grant **Terminal** (or iTerm) access to Contacts:

> System Settings → Privacy & Security → Contacts → enable Terminal

### LinkedIn credentials

Stored securely in macOS Keychain. You'll be prompted on first run.

> **Note:** Uses an unofficial LinkedIn API. Use your personal account and keep rate limits conservative to avoid blocks.

---

## Workflow

### 1. Scrape LinkedIn

```bash
linkedin-enricher scrape -n 50
```

- Exports contacts from Apple Contacts (those without a photo or LinkedIn URL)
- Skips contacts that are clearly not LinkedIn profiles (relationship labels like "Ethan's Mom", single first names without email, company names)
- Searches LinkedIn for each, scoring matches by name + company + email domain
- Downloads profile photos immediately when a match is found
- Stores results in `~/.linkedin_enricher/state.db`
- Safe to re-run — already-searched contacts are skipped
- Use `-n` to process in batches across sessions (avoids LinkedIn rate limits)
- Use `--retry-errors` to re-attempt contacts that errored in a previous run

### 2. Review & Apply

```bash
linkedin-enricher review
```

Opens a browser UI showing all pending matches with:
- Profile photo thumbnail
- Your contact info vs LinkedIn match
- Confidence score (colour-coded bar)
- Approve / Reject / Skip radio buttons

Bulk controls: "Approve high-confidence (≥80%)", "Approve all", "Skip all"

Click **Submit decisions** — approved matches are written to Apple Contacts immediately. No separate apply step needed.

---

## Other commands

```bash
linkedin-enricher status              # Show DB summary counts
linkedin-enricher export              # Export all matches to matches.csv
linkedin-enricher reset-credentials  # Clear cached LinkedIn session (fixes soft-blocks)
linkedin-enricher reset               # Wipe DB and start fresh (confirms first)
linkedin-enricher apply               # Manually apply approved matches (e.g. to retry failures)
```

---

## Rate limiting

LinkedIn's unofficial API has limits. The scraper:
- Waits ~7–15 seconds between requests (8 RPM with jitter)
- Pauses 60 seconds every 20 contacts
- ~100 contacts/hour at default settings
- If a CAPTCHA challenge occurs, you'll be prompted to solve it in a browser
- If searches silently return empty results, LinkedIn has soft-blocked the session — run `reset-credentials` and wait 30–60 minutes before retrying

---

## Disclaimer

This tool uses an **unofficial, reverse-engineered LinkedIn API** and is not affiliated with or endorsed by LinkedIn. Use it responsibly and at your own risk — it may violate [LinkedIn's Terms of Service](https://www.linkedin.com/legal/user-agreement). The author assumes no liability for account restrictions or any other consequences arising from its use.

---

## Data location

```
~/.linkedin_enricher/
├── state.db        # SQLite database (all contacts, matches, status)
└── photos/         # Downloaded + resized profile photos (512×512 JPEG)
```
