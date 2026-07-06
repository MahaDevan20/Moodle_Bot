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

def send_email(subject: str, body: str):
    msg = MIMEText(body)
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
    events_parsed = []   # list of (name: str, due_ts: int | None)

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

        events_parsed.append((clean_name, due_ts))
        due_label = (
            datetime.fromtimestamp(due_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if due_ts else "no date"
        )
        print(f"  [EVENT] '{clean_name}' | Due: {due_label}")

    if not events_parsed:
        print("\n[RESULT] No active (future) events found on the dashboard.")
        return

    current_names = [name for name, _ in events_parsed]

    # ------------------------------------------------------------------
    # 4. NEW ASSIGNMENT detection
    # ------------------------------------------------------------------
    old_known  = _load_set(TRACKER_FILE)
    new_items  = [n for n in current_names if n not in old_known]

    if new_items:
        print(f"\n[NEW] {new_items}")
        body = (
            "Hello,\n\n"
            "The following new assignment(s) have been added to Moodle:\n\n"
            + "".join(f"  * {item}\n" for item in new_items)
            + "\nLog in to Moodle for details.\n\nRegards,\nMoodle Alert Bot"
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

    for name, due_ts in events_parsed:
        if due_ts is None:
            continue
        hours_left = (due_ts - now_ts) / 3600
        if hours_left <= DEADLINE_ALERT_HOURS:
            if name not in already_alerted:
                deadline_alerts.append((name, due_ts, hours_left))
                _append_entry(DEADLINE_SENT_FILE, name)   # mark before sending
                print(f"  [DEADLINE] '{name}' due in {hours_left:.1f}h — alerting")
            else:
                print(f"  [DEADLINE] '{name}' due in {hours_left:.1f}h — already alerted, skipping")
        else:
            print(f"  [DEADLINE] '{name}' due in {hours_left:.1f}h — not yet in window")

    if deadline_alerts:
        body = (
            "Hello,\n\n"
            f"The following assignment(s) are due within {DEADLINE_ALERT_HOURS} hours:\n\n"
        )
        for name, due_ts, hours_left in deadline_alerts:
            due_str = datetime.fromtimestamp(due_ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
            body += f"  * {name}\n    Due: {due_str}  ({hours_left:.1f} hours remaining)\n\n"
        body += "Please submit on time!\n\nRegards,\nMoodle Alert Bot"
        send_email(f"Deadline Alert: {len(deadline_alerts)} assignment(s) due soon", body)
    else:
        print("\n[DEADLINE] No upcoming deadlines within the alert window.")


if __name__ == "__main__":
    run_moodle_check()
