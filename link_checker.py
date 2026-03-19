import os
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import time
import threading
from tqdm import tqdm

# ==============================
# CONFIG (defaults for CLI use)
# ==============================
DEFAULT_START_URL = "https://www.alkami.com/"
DEFAULT_DOMAIN = "alkami.com"

DEFAULT_OUTPUT_INTERNAL = "broken_internal_links.csv"
DEFAULT_OUTPUT_EXTERNAL = "broken_external_links.csv"

# Crawl limits
DEFAULT_MAX_PAGES = 5000          # Max pages to crawl
DEFAULT_RESPECT_MAX_PAGES = True  # If False, ignore MAX_PAGES and crawl all discovered pages

# Concurrency
DEFAULT_MAX_WORKERS = 80          # Number of threads for link checking/page fetching

# Request settings
DEFAULT_REQUEST_TIMEOUT = 10      # Seconds
DEFAULT_DELAY_BETWEEN_PAGES = 0   # Seconds delay between page fetches

# External link toggle
DEFAULT_CHECK_EXTERNAL_LINKS = False  # False = only on-site links, True = also check external links

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    )
}


# ==============================
# CORE ENGINE
# ==============================
def run_crawl(
    start_url: str = DEFAULT_START_URL,
    domain: str = DEFAULT_DOMAIN,
    output_internal: str = DEFAULT_OUTPUT_INTERNAL,
    output_external: str = DEFAULT_OUTPUT_EXTERNAL,
    max_pages: int | None = DEFAULT_MAX_PAGES,
    respect_max_pages: bool = DEFAULT_RESPECT_MAX_PAGES,
    max_workers: int = DEFAULT_MAX_WORKERS,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    delay_between_pages: float = DEFAULT_DELAY_BETWEEN_PAGES,
    check_external_links: bool = DEFAULT_CHECK_EXTERNAL_LINKS,
    headers: dict | None = None,
) -> dict:
    """
    Core crawl engine.
    Returns a stats dict you can use from CLI, GUI, schedulers, Slack, etc.

    stats = {
        "start_url": str,
        "domain": str,
        "pages_crawled": int,
        "internal_links_checked": int,
        "external_links_checked": int,
        "internal_broken": list[dict],
        "external_broken": list[dict],
        "output_internal": str,
        "output_external": str,
        "duration_seconds": float,
    }
    """
    if headers is None:
        headers = DEFAULT_HEADERS

    visited_pages = set()
    queue = deque([start_url])

    internal_checked: dict[str, int | str] = {}
    external_checked: dict[str, int | str] = {}

    internal_broken: list[dict] = []
    external_broken: list[dict] = []

    internal_lock = threading.Lock()
    external_lock = threading.Lock()

    def is_internal(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.netloc.lower().endswith(domain)

    def normalize_url(base: str, link: str | None) -> str | None:
        if not link:
            return None
        link = link.strip()

        if link.startswith("#") or link.startswith("mailto:") or link.startswith("tel:") or link.startswith("javascript:"):
            return None

        return urljoin(base, link)

    def fetch_page(url: str):
        try:
            return requests.get(url, timeout=request_timeout, headers=headers)
        except Exception:
            with internal_lock:
                internal_broken.append({
                    "target": url,
                    "source": None,
                    "status": "request_error"
                })
            return None

    def check_link(url: str, source: str | None, is_internal_link: bool):
        """
        Checks a single link (internal or external) and records if broken.
        """
        cache = internal_checked if is_internal_link else external_checked
        lock = internal_lock if is_internal_link else external_lock
        broken_array = internal_broken if is_internal_link else external_broken

        with lock:
            if url in cache:
                status = cache[url]
            else:
                status = None

        if status is None:
            try:
                resp = requests.head(url, allow_redirects=True, timeout=request_timeout, headers=headers)
                if resp.status_code >= 400 or resp.status_code == 405:
                    resp = requests.get(url, allow_redirects=True, timeout=request_timeout, headers=headers)
                status = resp.status_code
            except Exception:
                status = "request_error"

            with lock:
                cache[url] = status

        if (isinstance(status, int) and status >= 400) or status == "request_error":
            with lock:
                broken_array.append({
                    "target": url,
                    "source": source,
                    "status": status
                })

    # --------- MAIN CRAWL ----------
    start_time = time.time()
    pages_crawled = 0

    total_pages_estimate = max_pages if (respect_max_pages and max_pages) else 1000
    page_pbar = tqdm(total=total_pages_estimate, desc="Pages crawled", unit="page")
    link_pbar = tqdm(total=0, desc="Links scheduled", unit="link")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []

        while queue:
            if respect_max_pages and max_pages is not None and pages_crawled >= max_pages:
                print("[INFO] Reached MAX_PAGES limit")
                break

            page_url = queue.popleft()

            if page_url in visited_pages:
                continue

            visited_pages.add(page_url)
            pages_crawled += 1
            page_pbar.update(1)

            if not is_internal(page_url):
                continue

            resp = fetch_page(page_url)
            if resp is None:
                continue

            if resp.status_code >= 400:
                with internal_lock:
                    internal_broken.append({
                        "target": page_url,
                        "source": None,
                        "status": resp.status_code
                    })
                continue

            if "text/html" not in resp.headers.get("Content-Type", ""):
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            for tag in soup.find_all("a", href=True):
                href = normalize_url(page_url, tag.get("href"))
                if not href:
                    continue

                if is_internal(href):
                    # enqueue internal page for crawling
                    if href not in visited_pages:
                        queue.append(href)

                    # check internal link
                    futures.append(executor.submit(check_link, href, page_url, True))
                    link_pbar.total += 1
                    link_pbar.refresh()

                else:
                    # external link behavior controlled by toggle
                    if check_external_links:
                        futures.append(executor.submit(check_link, href, page_url, False))
                        link_pbar.total += 1
                        link_pbar.refresh()
                    else:
                        continue

            if delay_between_pages > 0:
                time.sleep(delay_between_pages)

        # wait for all link checks
        for future in as_completed(futures):
            _ = future.result()
            link_pbar.update(1)

    page_pbar.close()
    link_pbar.close()

    duration = time.time() - start_time

    # --------- SAVE CSVs ----------
    with open(output_internal, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["target", "source", "status"])
        writer.writeheader()
        for row in internal_broken:
            writer.writerow(row)

    with open(output_external, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["target", "source", "status"])
        writer.writeheader()
        for row in external_broken:
            writer.writerow(row)

    stats = {
        "start_url": start_url,
        "domain": domain,
        "pages_crawled": pages_crawled,
        "internal_links_checked": len(internal_checked),
        "external_links_checked": len(external_checked),
        "internal_broken": internal_broken,
        "external_broken": external_broken,
        "output_internal": output_internal,
        "output_external": output_external,
        "duration_seconds": duration,
    }

    return stats


# ==============================
# OPTIONAL: SLACK NOTIFICATION HOOK
# ==============================
def notify_slack(summary_text: str, webhook_url: str | None = None):
    """
    Simple Slack notification helper.

    Set SLACK_WEBHOOK_URL in your environment, or pass webhook_url directly.
    """
    if webhook_url is None:
        webhook_url = os.getenv("SLACK_WEBHOOK_URL")

    if not webhook_url:
        print("[WARN] No Slack webhook URL provided; skipping Slack notification.")
        return

    payload = {"text": summary_text}

    try:
        resp = requests.post(webhook_url, json=payload, timeout=5)
        if resp.status_code != 200:
            print(f"[WARN] Slack notification failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[WARN] Error sending Slack notification: {e}")


# ==============================
# CLI ENTRYPOINT
# ==============================
if __name__ == "__main__":
    # You can later parse CLI args here if you want.
    print("[INFO] Starting crawl...")
    stats = run_crawl()

    print("\n[SUMMARY]")
    print(f"Start URL: {stats['start_url']}")
    print(f"Domain: {stats['domain']}")
    print(f"Pages crawled: {stats['pages_crawled']}")
    print(f"Internal links checked: {stats['internal_links_checked']}")
    print(f"External links checked: {stats['external_links_checked']}")
    print(f"Internal broken links: {len(stats['internal_broken'])}")
    print(f"External broken links: {len(stats['external_broken'])}")
    print(f"Internal CSV: {stats['output_internal']}")
    print(f"External CSV: {stats['output_external']}")
    print(f"Duration: {stats['duration_seconds']:.2f} seconds")

    # OPTIONAL: auto-notify Slack if env var is set
    slack_webhook = os.getenv("SLACK_WEBHOOK_URL")
    if slack_webhook:
        summary = (
            f"✅ Crawl complete for {stats['start_url']}\n"
            f"Pages crawled: {stats['pages_crawled']}\n"
            f"Internal broken links: {len(stats['internal_broken'])}\n"
            f"External broken links: {len(stats['external_broken'])}\n"
            f"Internal CSV: {stats['output_internal']}\n"
            f"External CSV: {stats['output_external']}\n"
            f"Duration: {stats['duration_seconds']:.2f} seconds"
        )
        notify_slack(summary, slack_webhook)
