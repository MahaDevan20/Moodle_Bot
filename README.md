# Moodle Assignment Tracker Bot

Scrapes your Moodle dashboard, detects new assignments and 
upcoming deadlines, and emails you alerts. Runs automatically 
via GitHub Actions every 30 minutes.

## Setup

1. Clone this repo
2. Create a `.env` file in the root with:
   MOODLE_USERNAME=your_moodle_username
   MOODLE_PASSWORD=your_moodle_password
   SENDER_EMAIL=your_gmail_address
   SENDER_PASSWORD=your_gmail_app_password
   RECEIVER_EMAIL=where_you_want_alerts_sent
3. Install dependencies:
   pip install -r requirements.txt
4. Run locally:
   python moodle_bot.py

## Deploying via GitHub Actions

Add the same variables above as repository Secrets 
(Settings → Secrets and variables → Actions), then the 
workflow in `.github/workflows/moodle_check.yml` will run 
automatically every 30 minutes.
