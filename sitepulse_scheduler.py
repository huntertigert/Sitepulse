import os
import time
import json
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests

# Import run_crawl from your main GUI file
from link_checker_gui import run_crawl, REPORTS_DIR, CONFIG_PATH, DEFAULT_HEADERS


def load_config():
    """Load SitePulseConfig.json to get schedule + crawler settings."""
    if not os.path.exists(CONFIG_PATH):
        print("No config file found — scheduler cannot run.")
        return None

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("Failed to read config:", e)
        return None


def compute_next_run(schedule_days, schedule_time):
    """
    schedule_days: list of weekday strings ["Mon", "Thu"]
    schedule_time: "HH:MM" (24-hour format)
    Returns next datetime in the future.
    """

    if not schedule_days:
        return None

    # Map weekday -> index
    day_to_idx = {
        "Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3,
        "Fri": 4, "Sat": 5, "Sun": 6
    }

    selected_indices = [day_to_idx[d] for d in schedule_days if d in day_to_idx]

    # Parse time
    try:
        hour, minute = map(int, schedule_time.split(":"))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError()
    except Exception:
        print("Invalid schedule time — expected HH:MM, got:", schedule_time)
        return None

    now = datetime.now()
    now_weekday = now.weekday()

    candidates = []
    for idx in selected_indices:
        # Days ahead (0 = today)
        delta_days = (idx - now_weekday) % 7
        date = now.date() + timedelta(days=delta_days)

        dt = datetime(date.year, date.month, date.day, hour, minute)

        # If today's time already passed, push to next week
        if dt <= now:
            dt += timedelta(days=7)

        candidates.append(dt)

    return min(candidates) if candidates else None


def post_to_slack(webhook, stats):
    """Send scan summary to Slack (if webhook exists)."""
    if not webhook:
        print("Slack webhook not set — skipping Slack notification.")
        return

    message = (
        f"SitePulse scheduled scan complete\n"
        f"URL: {stats['start_url']}\n"
        f"Pages crawled: {stats['pages_crawled']}\n"
        f"Internal broken: {len(stats['internal_broken'])}\n"
        f"External broken: {len(stats['external_broken'])}\n"
        f"Duration: {stats['duration_seconds']:.2f} sec\n"
        f"Internal CSV: {stats['output_internal']}\n"
        f"External CSV: {stats['output_external']}\n"
    )

    try:
        requests.post(webhook, json={"text": message}, timeout=5)
        print("Slack notification sent.")
    except Exception as e:
        print("Slack error:", e)


def perform_scan(config):
    """Run a single scan using the same engine as the GUI."""
    url = config.get("url")
    if not url:
        print("URL missing in config — cannot run scan.")
        return

    # Settings
    max_pages = config.get("max_pages") or 5000
    threads = config.get("threads") or 40
    max_depth = config.get("max_depth")
    include_subdomains = config.get("include_subdomains", True)
    respect_robots = config.get("respect_robots", False)

    ignore_patterns_str = config.get("ignore_patterns", "")
    ignore_patterns = [s.strip() for s in ignore_patterns_str.split(",") if s.strip()]

    slack_webhook = config.get("slack_webhook", "")

    os.makedirs(REPORTS_DIR, exist_ok=True)

    # Unique CSV file names
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_int = os.path.join(REPORTS_DIR, f"sitepulse_internal_{ts}.csv")
    out_ext = os.path.join(REPORTS_DIR, f"sitepulse_external_{ts}.csv")

    print("Starting scheduled scan…")

    stats = run_crawl(
        start_url=url,
        domain=urlparse(url).netloc.lower(),
        output_internal=out_int,
        output_external=out_ext,
        max_pages=max_pages,
        max_workers=threads,
        request_timeout=10,
        delay_between_pages=0,
        check_external_links=config.get("check_external_links", False),
        headers=DEFAULT_HEADERS,
        progress_callback=None,
        cancel_event=DummyCancel(),
        ignore_patterns=ignore_patterns,
        max_depth=max_depth,
        include_subdomains=include_subdomains,
        respect_robots=respect_robots,
        retry_failed=True,
    )

    print("Scan complete.")
    print("Internal CSV:", stats["output_internal"])
    print("External CSV:", stats["output_external"])

    post_to_slack(slack_webhook, stats)


class DummyCancel:
    """Minimal cancel_event replacement for headless mode."""
    def is_set(self):
        return False


def main():
    print("=== SitePulse Scheduler Started ===")

    while True:
        config = load_config()
        if config is None:
            print("Config not found. Sleeping 5 minutes.")
            time.sleep(300)
            continue

        # Read schedule settings
        schedule_enabled = config.get("autoscan_enabled", False)
        if not schedule_enabled:
            print("Scheduled scans disabled — sleeping 10 minutes.")
            time.sleep(600)
            continue

        schedule_days = config.get("autoscan_days", {})
        selected_days = [d for d, v in schedule_days.items() if v]

        schedule_time = config.get("autoscan_time", "02:00")

        next_run = compute_next_run(selected_days, schedule_time)

        if not next_run:
            print("No valid schedule found — sleeping 10 minutes.")
            time.sleep(600)
            continue

        now = datetime.now()
        wait_seconds = max(5, (next_run - now).total_seconds())

        print(f"Next run: {next_run} (in {int(wait_seconds)} sec)")

        # Sleep in chunks so we can reload config
        slept = 0
        chunk = 5
        while slept < wait_seconds:
            time.sleep(chunk)
            slept += chunk

        # Run scan
        perform_scan(config)


if __name__ == "__main__":
    main()
