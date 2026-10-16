import os, re, json, hashlib, sqlite3, difflib, threading, time
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, redirect, url_for, render_template, flash, abort
from apscheduler.schedulers.background import BackgroundScheduler

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("WCD_DB_PATH", os.path.join(APP_DIR, "wcd.sqlite3"))
SECRET_KEY = os.environ.get("WCD_SECRET_KEY", "change-this-secret-before-deploying")
USER_AGENT = "WebsiteChangeDetector/1.0 (respectful monitoring; configure contact details)"
FETCH_TIMEOUT = 20
MAX_TEXT_CHARS = 500_000
FETCH_LOCKS = {}
LOCKS_GUARD = threading.Lock()

app = Flask(__name__)
app.secret_key = SECRET_KEY
scheduler = BackgroundScheduler(daemon=True)

def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS pages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
          interval_minutes INTEGER NOT NULL DEFAULT 60,
          sensitivity TEXT NOT NULL DEFAULT 'medium',
          watch_selectors TEXT DEFAULT '',
          ignore_selectors TEXT DEFAULT '',
          keyword_filter TEXT DEFAULT '',
          enabled INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'new',
          last_checked TEXT, last_changed TEXT, last_error TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
          checked_at TEXT NOT NULL, content_hash TEXT NOT NULL,
          text_content TEXT NOT NULL, screenshot_path TEXT,
          change_summary TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_page_time ON snapshots(page_id, checked_at DESC);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
          created_at TEXT NOT NULL, event_type TEXT NOT NULL,
          message TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0
        );
        """)
init_db()

def valid_public_url(url):
    try:
        p = urlparse(url.strip())
        if p.scheme not in ("http", "https") or not p.hostname:
            return False, "Use a complete public http:// or https:// URL."
        host = p.hostname.lower()
        # Avoid localhost and obvious private/local targets (basic SSRF protection).
        if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
            return False, "Local and localhost URLs are not allowed."
        import ipaddress, socket
        try:
            infos = socket.getaddrinfo(host, None)
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if not ip.is_global:
                    return False, "Private, loopback, and non-public IP addresses are not allowed."
        except socket.gaierror:
            return False, "The hostname could not be resolved."
        return True, ""
    except Exception:
        return False, "Invalid URL."

def clean_text(html, watch_selectors="", ignore_selectors=""):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "canvas"]):
        tag.decompose()
    # Remove common volatile elements/attributes from text representation.
    for el in soup.select("[aria-live], time, .timestamp, .advertisement, .ads, [data-ad]"):
        el.decompose()
    for selector in filter(None, [s.strip() for s in (ignore_selectors or "").split(",")]):
        try:
            for el in soup.select(selector):
                el.decompose()
        except Exception:
            pass
    watch = [s.strip() for s in (watch_selectors or "").split(",") if s.strip()]
    if watch:
        selected = []
        for selector in watch:
            try:
                selected.extend(soup.select(selector))
            except Exception:
                continue
        text = "\n".join(el.get_text(" ", strip=True) for el in selected)
    else:
        text = soup.get_text(" ", strip=True)
    text = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", " TIME ", text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?\b", " DATE ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_TEXT_CHARS]

def digest(text):
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()

def make_summary(old, new):
    a = (old or "").split()
    b = (new or "").split()
    diff = list(difflib.unified_diff(a, b, fromfile="previous", tofile="current", n=3))
    if not diff:
        return "Content changed."
    body = " ".join(diff[2:])[:1000]
    return body or "Content changed."

def send_notifications(page, summary, old_hash, new_hash):
    # Email is opt-in via environment variables; webhook can be Telegram/Discord-compatible.
    webhook = os.environ.get("WCD_WEBHOOK_URL", "").strip()
    if webhook:
        try:
            requests.post(webhook, json={"content": f"Website change: {page['name']}\\n{page['url']}\\n{summary[:1500]}"},
                          timeout=8, headers={"User-Agent": USER_AGENT})
        except requests.RequestException:
            pass
    host = os.environ.get("WCD_SMTP_HOST", "").strip()
    to_addr = os.environ.get("WCD_ALERT_TO", "").strip()
    from_addr = os.environ.get("WCD_ALERT_FROM", "").strip()
    if host and to_addr and from_addr:
        import smtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = f"Website changed: {page['name']}"
        msg["From"], msg["To"] = from_addr, to_addr
        msg.set_content(f"{page['name']} changed at {utcnow()}\\n{page['url']}\\n\\n{summary}")
        try:
            with smtplib.SMTP(host, int(os.environ.get("WCD_SMTP_PORT", "587")), timeout=8) as s:
                s.starttls()
                user = os.environ.get("WCD_SMTP_USER", "")
                password = os.environ.get("WCD_SMTP_PASSWORD", "")
                if user:
                    s.login(user, password)
                s.send_message(msg)
        except Exception:
            pass

def check_page(page_id):
    with LOCKS_GUARD:
        lock = FETCH_LOCKS.setdefault(page_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    try:
        with db() as c:
            page = c.execute("SELECT * FROM pages WHERE id=? AND enabled=1", (page_id,)).fetchone()
            if not page:
                return
            page = dict(page)
        ok, reason = valid_public_url(page["url"])
        if not ok:
            raise ValueError(reason)
        response = requests.get(page["url"], timeout=FETCH_TIMEOUT, allow_redirects=True,
                                headers={"User-Agent": USER_AGENT})
        if response.status_code in (401, 403, 429):
            raise ValueError(f"Site blocked the request or rate-limited it (HTTP {response.status_code}).")
        response.raise_for_status()
        content = clean_text(response.text, page["watch_selectors"], page["ignore_selectors"])
        if page["keyword_filter"]:
            keywords = [k.strip().lower() for k in page["keyword_filter"].split(",") if k.strip()]
            if keywords:
                content = "\n".join(line for line in content.splitlines()
                                    if any(k in line.lower() for k in keywords))
        new_hash = digest(content)
        with db() as c:
            prior = c.execute("SELECT * FROM snapshots WHERE page_id=? ORDER BY id DESC LIMIT 1", (page_id,)).fetchone()
            now = utcnow()
            changed = bool(prior and prior["content_hash"] != new_hash)
            summary = make_summary(prior["text_content"], content) if changed else ("Initial baseline stored." if not prior else "No meaningful change.")
            # A snapshot is stored only for the first check or when normalized content changes.
            if not prior or changed:
                c.execute("INSERT INTO snapshots(page_id,checked_at,content_hash,text_content,change_summary) VALUES(?,?,?,?,?)",
                          (page_id, now, new_hash, content, summary))
            c.execute("UPDATE pages SET last_checked=?, last_error=NULL, status=?, last_changed=CASE WHEN ? THEN ? ELSE last_changed END WHERE id=?",
                      (now, "changed" if changed else ("ok" if prior else "baseline"), int(changed), now, page_id))
            if changed:
                c.execute("INSERT INTO events(page_id,created_at,event_type,message) VALUES(?,?,?,?)",
                          (page_id, now, "change", summary[:1500]))
        if changed:
            send_notifications(page, summary, prior["content_hash"], new_hash)
    except Exception as e:
        with db() as c:
            c.execute("UPDATE pages SET last_checked=?, last_error=?, status='error' WHERE id=?",
                      (utcnow(), str(e)[:500], page_id))
            c.execute("INSERT INTO events(page_id,created_at,event_type,message) VALUES(?,?,?,?)",
                      (page_id, utcnow(), "error", str(e)[:1000]))
    finally:
        lock.release()

def scheduled_check():
    with db() as c:
        rows = c.execute("SELECT id FROM pages WHERE enabled=1 AND (last_checked IS NULL OR last_checked <= datetime('now', '-' || interval_minutes || ' minutes'))").fetchall()
    for row in rows:
        threading.Thread(target=check_page, args=(row["id"],), daemon=True).start()

@app.route("/")
def index():
    with db() as c:
        pages = c.execute("""
          SELECT p.*, (SELECT COUNT(*) FROM snapshots s WHERE s.page_id=p.id) AS versions
          FROM pages p ORDER BY p.created_at DESC
        """).fetchall()
        events = c.execute("""SELECT e.*, p.name, p.url FROM events e JOIN pages p ON p.id=e.page_id
                              ORDER BY e.id DESC LIMIT 10""").fetchall()
    return render_template("index.html", pages=pages, events=events)

@app.route("/pages/add", methods=["POST"])
def add_page():
    name = request.form.get("name", "").strip()[:100]
    url = request.form.get("url", "").strip()
    try:
        interval = int(request.form.get("interval_minutes", "60"))
    except ValueError:
        interval = 60
    if not name or not url:
        flash("Name and URL are required.", "error")
        return redirect(url_for("index"))
    if not 15 <= interval <= 1440:
        flash("Check interval must be between 15 minutes and 24 hours.", "error")
        return redirect(url_for("index"))
    ok, reason = valid_public_url(url)
    if not ok:
        flash(reason, "error")
        return redirect(url_for("index"))
    try:
        with db() as c:
            c.execute("""INSERT INTO pages(name,url,interval_minutes,sensitivity,watch_selectors,ignore_selectors,
                      keyword_filter,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                      (name, url, interval, request.form.get("sensitivity","medium"),
                       request.form.get("watch_selectors","").strip()[:500],
                       request.form.get("ignore_selectors","").strip()[:500],
                       request.form.get("keyword_filter","").strip()[:500], utcnow()))
        flash("Page added. Run a first check to save its baseline.", "success")
    except sqlite3.IntegrityError:
        flash("That URL is already being monitored.", "error")
    return redirect(url_for("index"))

@app.route("/pages/<int:page_id>/check", methods=["POST"])
def manual_check(page_id):
    check_page(page_id)
    flash("Check finished. Review the status and event list.", "success")
    return redirect(url_for("index"))

@app.route("/pages/<int:page_id>/toggle", methods=["POST"])
def toggle_page(page_id):
    with db() as c:
        c.execute("UPDATE pages SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (page_id,))
    return redirect(url_for("index"))

@app.route("/pages/<int:page_id>/delete", methods=["POST"])
def delete_page(page_id):
    with db() as c:
        c.execute("DELETE FROM pages WHERE id=?", (page_id,))
    flash("Monitor and its history deleted.", "success")
    return redirect(url_for("index"))

@app.route("/pages/<int:page_id>/history")
def history(page_id):
    with db() as c:
        page = c.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()
        if not page:
            abort(404)
        snaps = c.execute("SELECT * FROM snapshots WHERE page_id=? ORDER BY id DESC", (page_id,)).fetchall()
        events = c.execute("SELECT * FROM events WHERE page_id=? ORDER BY id DESC LIMIT 50", (page_id,)).fetchall()
    return render_template("history.html", page=page, snaps=snaps, events=events)

@app.route("/compare/<int:old_id>/<int:new_id>")
def compare(old_id, new_id):
    with db() as c:
        old = c.execute("SELECT * FROM snapshots WHERE id=?", (old_id,)).fetchone()
        new = c.execute("SELECT * FROM snapshots WHERE id=?", (new_id,)).fetchone()
    if not old or not new or old["page_id"] != new["page_id"]:
        abort(404)
    diff = difflib.unified_diff(old["text_content"].splitlines(), new["text_content"].splitlines(),
                                fromfile=old["checked_at"], tofile=new["checked_at"], lineterm="")
    return render_template("compare.html", old=old, new=new, diff="\n".join(diff))

@app.route("/health")
def health():
    return {"status": "ok", "time": utcnow()}

if not scheduler.running:
    scheduler.add_job(scheduled_check, "interval", minutes=1, id="page-checker", max_instances=1, coalesce=True)
    scheduler.start()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
