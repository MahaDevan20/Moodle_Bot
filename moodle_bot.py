from dotenv import load_dotenv
load_dotenv()
import requests
from bs4 import BeautifulSoup
import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timezone

# --- CONFIGURATION (prefer environment variables for GitHub Actions) ---
MOODLE_LOGIN_URL = "https://lms.rajagiri.edu/login/index.php"
DASHBOARD_URL    = "https://lms.rajagiri.edu/my/"

USERNAME         = os.environ.get("MOODLE_USERNAME")
PASSWORD         = os.environ.get("MOODLE_PASSWORD")

SMTP_SERVER      = "smtp.gmail.com"
SMTP_PORT        = 587
SENDER_EMAIL     = os.environ.get("SENDER_EMAIL")
SENDER_PASSWORD  = os.environ.get("SENDER_PASSWORD")
RECEIVER_EMAIL   = os.environ.get("RECEIVER_EMAIL")

if not all([USERNAME, PASSWORD, SENDER_EMAIL, SENDER_PASSWORD, RECEIVER_EMAIL]):
    raise SystemExit("Missing required environment variables — check your .env file.")

# Tracker files (committed back to the repo by the GitHub Actions workflow)
TRACKER_FILE       = "last_assignments.txt"           # known assignments
DEADLINE_SENT_FILE = "sent_deadline_alerts.txt"       # deadlines already alerted

# Alert window: send deadline email when due within this many hours
DEADLINE_ALERT_HOURS = 24


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_set(filepath: str) -> set:
    if not os.path.exists(filepath):
        return set()
    with open(filepath, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}

def _append_entry(filepath: str, entry: str):
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(entry + "\n")

def _write_set(filepath: str, items):
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(items)))

def fetch_assignment_details(session, url):
    if not url:
        return None, None
    try:
        resp = session.get(url, timeout=10)
        if resp.status_code != 200:
            return None, None
        
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # 1. Parse Course / Subject Name
        course_name = None
        course_a = soup.find("a", href=lambda h: h and "course/view.php" in h)
        if course_a:
            title_attr = course_a.get("title")
            if title_attr:
                course_name = title_attr.strip()
            else:
                course_name = course_a.get_text(strip=True)
        
        # 2. Parse Due Date
        due_date = None
        dates_div = soup.find(class_="activity-dates")
        if dates_div:
            for div in dates_div.find_all("div"):
                text = div.get_text(strip=True)
                if text.startswith("Due:"):
                    due_date = text.replace("Due:", "").strip()
                    break
        
        if not due_date:
            for tr in soup.find_all("tr"):
                th = tr.find(["th", "td"], class_="c0")
                if th and "due date" in th.get_text().lower():
                    td = tr.find(class_="c1")
                    if td:
                        due_date = td.get_text(strip=True)
                        break
                        
        return course_name, due_date
    except Exception as e:
        print(f"  [ERROR] Fetch details failed for {url}: {e}")
        return None, None


def send_email(subject: str, body: str):
    msg = MIMEText(body, "html")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECEIVER_EMAIL
    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, [RECEIVER_EMAIL], msg.as_string())
        server.quit()
        print(f"  [OK] Email sent: {subject}")
    except Exception as e:
        print(f"  [FAIL] Could not send email: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_moodle_check():
    session = requests.Session()

    # ------------------------------------------------------------------
    # 1. Login
    # ------------------------------------------------------------------
    response = session.get(MOODLE_LOGIN_URL)
    soup = BeautifulSoup(response.text, "html.parser")

    token_el = soup.find("input", {"name": "logintoken"})
    if not token_el:
        print("[ERROR] Could not find login token — check credentials / URL.")
        return

    login_response = session.post(MOODLE_LOGIN_URL, data={
        "username":   USERNAME,
        "password":   PASSWORD,
        "logintoken": token_el["value"],
    })

    page_title = BeautifulSoup(login_response.text, "html.parser").title.text.strip()
    print(f"[LOGIN] {page_title}")
    if "login" in page_title.lower():
        print("[ERROR] Login failed — check MOODLE_USERNAME / MOODLE_PASSWORD.")
        return

    # ------------------------------------------------------------------
    # 2. Load dashboard
    # ------------------------------------------------------------------
    dash_resp = session.get(DASHBOARD_URL)
    dash_soup = BeautifulSoup(dash_resp.text, "html.parser")
    print(f"[DASHBOARD] {dash_soup.title.text.strip()}")

    with open("dashboard.html", "w", encoding="utf-8") as f:
        f.write(dash_resp.text)

    # ------------------------------------------------------------------
    # 3. Parse events
    #
    # Moodle calendar renders events as:
    #
    #   <td data-day-timestamp="1783276200" ...>   ← Unix timestamp of that day
    #     ...
    #     <span class="eventname">Assignment is due</span>
    #     ...
    #   </td>
    #
    # We walk from each <span class="eventname"> up to the parent <td>
    # to grab the timestamp. We deduplicate by name and skip past-due events.
    # ------------------------------------------------------------------
    now_utc   = datetime.now(timezone.utc)
    now_ts    = now_utc.timestamp()

    event_spans = dash_soup.find_all("span", class_="eventname")
    print(f"\n[PARSE] Found {len(event_spans)} raw event span(s)")

    seen_names    = set()
    events_parsed = []   # list of (name: str, due_ts: int | None, link: str | None)

    for span in event_spans:
        raw_name   = span.get_text(" ", strip=True)
        clean_name = raw_name.replace(" is due", "").strip()

        # Deduplicate
        if clean_name in seen_names:
            continue
        seen_names.add(clean_name)

        # Find due timestamp from nearest <td data-day-timestamp="..."> ancestor
        due_ts = None
        td = span.find_parent("td", attrs={"data-day-timestamp": True})
        if td:
            try:
                due_ts = int(td["data-day-timestamp"])
            except (ValueError, KeyError):
                pass

        # Skip events that are already past due
        if due_ts is not None and due_ts < now_ts:
            print(f"  [SKIP] '{clean_name}' — past due ({datetime.fromtimestamp(due_ts, tz=timezone.utc).strftime('%Y-%m-%d')})")
            continue

        # Get assignment link if available
        link = None
        a_tag = span.find_parent("a")
        if a_tag and "href" in a_tag.attrs:
            link = a_tag["href"]

        events_parsed.append((clean_name, due_ts, link))
        due_label = (
            datetime.fromtimestamp(due_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if due_ts else "no date"
        )
        print(f"  [EVENT] '{clean_name}' | Due: {due_label} | Link: {link}")

    if not events_parsed:
        print("\n[RESULT] No active (future) events found on the dashboard.")
        return

    current_names = [name for name, _, _ in events_parsed]

    # ------------------------------------------------------------------
    # 4. NEW ASSIGNMENT detection
    # ------------------------------------------------------------------
    old_known  = _load_set(TRACKER_FILE)
    new_events = [item for item in events_parsed if item[0] not in old_known]

    if new_events:
        print(f"\n[NEW] {[item[0] for item in new_events]}")
        body = (
            "<p>Hello,</p>"
            "<p>The following new assignment(s) have been added to Moodle:</p>"
            "<ol>"
        )
        for name, due_ts, link in new_events:
            course_name, due_date = fetch_assignment_details(session, link)
            if not course_name:
                course_name = "Not specified"
            if not due_date:
                if due_ts:
                    due_date = datetime.fromtimestamp(due_ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
                else:
                    due_date = "No deadline specified"
            
            body += (
                f"<li>"
                f"<strong>{name}</strong><br>"
                f"<b>Subject:</b> {course_name}<br>"
                f"<b>Last submission date:</b> {due_date}"
                f"</li>"
            )
        body += (
            "</ol>"
            f"<p><a href=\"{MOODLE_LOGIN_URL}\">Log in to see more details</a>.</p>"
            "<p>Regards,<br>Moodle Alert Bot</p>"
        )
        send_email("New Moodle Assignment Added", body)
    else:
        print("\n[NEW] No new assignments since last check.")

    # Always keep tracker in sync (union of old + current)
    _write_set(TRACKER_FILE, old_known | set(current_names))

    # ------------------------------------------------------------------
    # 5. DEADLINE APPROACHING — alert once per item
    # ------------------------------------------------------------------
    already_alerted = _load_set(DEADLINE_SENT_FILE)
    deadline_alerts = []

    for name, due_ts, link in events_parsed:
        if due_ts is None:
            continue
        hours_left = (due_ts - now_ts) / 3600
        if hours_left <= DEADLINE_ALERT_HOURS:
            if name not in already_alerted:
                deadline_alerts.append((name, due_ts, hours_left, link))
                _append_entry(DEADLINE_SENT_FILE, name)   # mark before sending
                print(f"  [DEADLINE] '{name}' due in {hours_left:.1f}h — alerting")
            else:
                print(f"  [DEADLINE] '{name}' due in {hours_left:.1f}h — already alerted, skipping")
        else:
            print(f"  [DEADLINE] '{name}' due in {hours_left:.1f}h — not yet in window")

    if deadline_alerts:
        body = (
            "<p>Hello,</p>"
            f"<p>The following assignment(s) are due within {DEADLINE_ALERT_HOURS} hours:</p>"
            "<ol>"
        )
        for name, due_ts, hours_left, link in deadline_alerts:
            course_name, due_date = fetch_assignment_details(session, link)
            if not course_name:
                course_name = "Not specified"
            if not due_date:
                due_date = datetime.fromtimestamp(due_ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
            
            body += (
                f"<li>"
                f"<strong>{name}</strong><br>"
                f"<b>Subject:</b> {course_name}<br>"
                f"<b>Last submission date:</b> {due_date} ({hours_left:.1f} hours remaining)"
                f"</li>"
            )
        body += (
            "</ol>"
            "<p>Please submit on time!</p>"
            f"<p><a href=\"{MOODLE_LOGIN_URL}\">Log in to see more details</a>.</p>"
            "<p>Regards,<br>Moodle Alert Bot</p>"
        )
        send_email(f"Deadline Alert: {len(deadline_alerts)} assignment(s) due soon", body)
    else:
        print("\n[DEADLINE] No upcoming deadlines within the alert window.")


if __name__ == "__main__":
    run_moodle_check()
