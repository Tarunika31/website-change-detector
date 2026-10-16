# Watchtower — Website Change Detector

A student-project MVP for monitoring public web pages and notifying you when meaningful text changes are detected.

## Features implemented

- Add a public URL, display name, and interval from **15 minutes to 24 hours**.
- Background scheduler checks due pages; manual **Check now** is available.
- Saves normalized text and a SHA-256 content hash.
- Stores a new snapshot only for the first successful baseline or when the content hash changes.
- Filters common volatile content (scripts, styles, timestamps, common ad elements); configurable CSS selectors to watch or ignore.
- Optional keyword filtering.
- Version history and unified text diff view.
- Dashboard status, recent change/error event log, pause/resume and delete controls.
- Optional webhook and SMTP email notifications.
- Basic public-URL validation to block localhost and non-public IP targets.
- Health endpoint at `/health`.

## Run locally

Requires Python 3.10+.

### Windows (PowerShell)
```powershell
py -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
$env:WCD_SECRET_KEY = "replace-this-with-a-long-random-secret"
python app.py
```

### macOS / Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export WCD_SECRET_KEY="replace-this-with-a-long-random-secret"
python app.py
```

Open http://127.0.0.1:5000.

The SQLite database is created automatically in the project folder. For a single-student demo this avoids requiring a database server; the schema can later be migrated to MySQL if the evaluator explicitly requires it.

## Notifications

Webhook (optional):
```bash
export WCD_WEBHOOK_URL="https://your-discord-webhook-url"
```
The app posts a Discord-compatible `content` message. Other services may require adapting the payload.

SMTP (optional): configure `WCD_SMTP_HOST`, `WCD_SMTP_PORT`, `WCD_SMTP_USER`, `WCD_SMTP_PASSWORD`, `WCD_ALERT_FROM`, and `WCD_ALERT_TO`. Use an app password where required; never commit secrets.

## How to demo

1. Add a page you are allowed to monitor (prefer a stable public demo page).
2. Click **Check now**. This stores the baseline.
3. Change the page or use a test page you control.
4. Click **Check now** again.
5. Open **History / diff** and show the saved versions and highlighted changes.
6. Optionally configure a webhook and demonstrate the alert.

## Design notes / limitations

- This MVP uses `requests` + BeautifulSoup, so it monitors server-rendered HTML. Pages whose meaningful content appears only after JavaScript runs may need a Playwright-based fetch adapter.
- The CSS selector fields accept comma-separated selectors; they are configured while adding a monitor, not with a visual point-and-click browser picker.
- Screenshots and screenshot diffs are not implemented in this MVP. The core deliverable is normalized text history and diffs. Add Playwright screenshots as a next extension if the rubric strictly requires visual diffs.
- `sensitivity` is stored for the UI and future tuning; the initial implementation uses normalized content hashes rather than different thresholds per sensitivity.
- Background scheduler runs in the same process. For production use, run one worker and add a persistent queue/locking strategy to prevent duplicate checks across multiple processes.
- The included URL validation is a basic safeguard, not a complete security boundary. Use a network egress policy and production-grade SSRF protections before deploying publicly.
- Respect each website's terms, robots.txt, rate limits, and copyright. Do not monitor private pages or bypass access controls.
- The app is intended as a local educational project; it has not been penetration-tested or load-tested.

## Requirement coverage checklist

- [x] URL/name and 15 min–24 h interval
- [x] Scheduled checks, timeout, and request errors
- [x] Public pages only (basic validation; see limitation)
- [x] Text snapshot and change-only version storage
- [x] Common noise filtering, keyword filtering, CSS include/exclude selectors
- [x] Text diff and side-by-side version timeline
- [x] Webhook/email notifications with per-change event log (duplicate changes suppressed by hash)
- [x] Per-page dashboard, last checked, last changed, status
- [ ] JavaScript-rendered pages via headless browser
- [ ] Screenshot capture and screenshot diff
- [ ] Visual point-and-click selector picker
- [ ] Daily digest / browser extension / RSS feed / AI summary bonus features

## Before submission

1. Run the app locally and take screenshots of the dashboard, change history, and diff.
2. Test with a page you control; record the steps and results in your report.
3. Include a short architecture diagram and a table of test cases in your submission.
4. Be honest in the demo about which optional features are not implemented.
