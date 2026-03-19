import os
import threading
import time
import csv
import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
import urllib.robotparser as robotparser

import tkinter as tk
from tkinter import ttk, messagebox

# ==============================
# ENGINE CONFIG DEFAULTS
# ==============================
DEFAULT_START_URL = "https://www.alkami.com/"
DEFAULT_DOMAIN = "alkami.com"

DEFAULT_MAX_PAGES = 5000
DEFAULT_MAX_WORKERS = 80
DEFAULT_REQUEST_TIMEOUT = 10
DEFAULT_DELAY_BETWEEN_PAGES = 0
DEFAULT_CHECK_EXTERNAL_LINKS = False

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    )
}

# Reports stored in user's home folder
REPORTS_DIR = os.path.join(os.path.expanduser("~"), "SitePulseReports")

# Config file for persisting settings
CONFIG_PATH = os.path.join(os.path.expanduser("~"), "SitePulseConfig.json")

# Placeholder text for ignore patterns (hint only, not active unless changed)
IGNORE_PLACEHOLDER = "/blog/, /resource-library/, /press-room/"

# ==============================
# CORE ENGINE
# ==============================

def run_crawl(
    start_url,
    domain,
    output_internal,
    output_external,
    max_pages,
    max_workers,
    request_timeout,
    delay_between_pages,
    check_external_links,
    headers,
    progress_callback,
    cancel_event,
    ignore_patterns,
    max_depth=None,              # limit link depth (0 = start page)
    include_subdomains=True,     # control subdomains
    respect_robots=False,        # obey robots.txt if True
    retry_failed=True,           # retry once on failures
):
    visited_pages = set()
    # store (url, depth) so we can enforce max_depth
    queue = deque([(start_url, 0)])

    internal_checked = {}
    external_checked = {}

    internal_broken = []
    external_broken = []

    internal_lock = threading.Lock()
    external_lock = threading.Lock()

    domain = domain.lower()

    def is_internal(url):
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if include_subdomains:
            return host.endswith(domain)
        else:
            return host == domain

    def should_ignore(url):
        return any(pat in url for pat in ignore_patterns)

    def normalize_url(base, link):
        if not link:
            return None
        link = link.strip()
        if (
            link.startswith("#")
            or link.startswith("mailto:")
            or link.startswith("tel:")
            or link.startswith("javascript:")
        ):
            return None
        return urljoin(base, link)

    # robots.txt parser (optional)
    rp = None
    if respect_robots:
        try:
            parsed_start = urlparse(start_url)
            robots_url = f"{parsed_start.scheme}://{domain}/robots.txt"
            rp = robotparser.RobotFileParser()
            rp.set_url(robots_url)
            rp.read()
        except Exception:
            rp = None  # fail open if robots.txt can’t be read

    def allowed_by_robots(url):
        if not respect_robots or rp is None:
            return True
        ua = headers.get("User-Agent", "*")
        try:
            return rp.can_fetch(ua, url)
        except Exception:
            return True

    def fetch_page(url):
        # Respect robots.txt if enabled
        if not allowed_by_robots(url):
            with internal_lock:
                internal_broken.append(
                    {"target": url, "source": None, "status": "blocked_by_robots"}
                )
            return None

        attempts = 2 if retry_failed else 1
        last_exc = None
        for _ in range(attempts):
            try:
                resp = requests.get(url, timeout=request_timeout, headers=headers)
                # retry on 5xx if allowed
                if retry_failed and resp.status_code >= 500:
                    last_exc = None
                    continue
                return resp
            except Exception as e:
                last_exc = e
        with internal_lock:
            internal_broken.append(
                {"target": url, "source": None, "status": f"request_error: {last_exc}"}
            )
        return None

    def check_link(url, source, is_int):
        # Internal-only ignore logic
        if is_int and should_ignore(url):
            return

        # robots.txt also applies to link checks for internal URLs
        if is_int and not allowed_by_robots(url):
            with internal_lock:
                internal_broken.append(
                    {"target": url, "source": source, "status": "blocked_by_robots"}
                )
            return

        cache = internal_checked if is_int else external_checked
        lock = internal_lock if is_int else external_lock
        broken_array = internal_broken if is_int else external_broken

        with lock:
            status = cache.get(url)

        if status is None:
            attempts = 2 if retry_failed else 1
            last_exc = None
            for _ in range(attempts):
                try:
                    resp = requests.head(
                        url,
                        allow_redirects=True,
                        timeout=request_timeout,
                        headers=headers,
                    )
                    if resp.status_code >= 400 or resp.status_code == 405:
                        resp = requests.get(
                            url,
                            allow_redirects=True,
                            timeout=request_timeout,
                            headers=headers,
                        )
                    # retry on 5xx if configured
                    if retry_failed and resp.status_code >= 500:
                        last_exc = None
                        continue
                    status = resp.status_code
                    break
                except Exception as e:
                    last_exc = e

            if status is None:
                status = f"request_error: {last_exc}"

            with lock:
                cache[url] = status

        if isinstance(status, int) and status >= 400 or (
            isinstance(status, str) and status.startswith("request_error")
        ):
            with lock:
                broken_array.append({"target": url, "source": source, "status": status})

    # Crawl
    pages_crawled = 0
    total_pages_estimate = max_pages if max_pages else 1000
    status = "completed"
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []

        while queue:
            if cancel_event.is_set():
                status = "cancelled"
                break

            if max_pages and pages_crawled >= max_pages:
                break

            page_url, depth = queue.popleft()
            if page_url in visited_pages:
                continue

            visited_pages.add(page_url)
            pages_crawled += 1

            # INTERNAL ONLY ignore for pages
            if is_internal(page_url) and should_ignore(page_url):
                continue

            if not is_internal(page_url):
                continue

            resp = fetch_page(page_url)
            if not resp:
                continue

            if resp.status_code >= 400:
                internal_broken.append(
                    {"target": page_url, "source": None, "status": resp.status_code}
                )
                continue

            if "text/html" not in resp.headers.get("Content-Type", ""):
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            next_depth = depth + 1
            depth_limited = max_depth is not None and next_depth > max_depth

            for tag in soup.find_all("a", href=True):
                href = normalize_url(page_url, tag.get("href"))
                if not href:
                    continue

                # INTERNAL ONLY ignore for discovered internal links
                if is_internal(href) and should_ignore(href):
                    continue

                if is_internal(href):
                    if not depth_limited and href not in visited_pages:
                        queue.append((href, next_depth))
                    futures.append(executor.submit(check_link, href, page_url, True))
                else:
                    if check_external_links:
                        futures.append(executor.submit(check_link, href, page_url, False))

            if delay_between_pages:
                time.sleep(delay_between_pages)

            if progress_callback:
                progress_callback({
                    "pages_crawled": pages_crawled,
                    "internal_links_checked": len(internal_checked),
                    "external_links_checked": len(external_checked),
                    "estimated_total_pages": total_pages_estimate,
                })

        for f in as_completed(futures):
            if cancel_event.is_set():
                status = "cancelled"
                break

    # Save CSVs
    os.makedirs(os.path.dirname(output_internal), exist_ok=True)
    os.makedirs(os.path.dirname(output_external), exist_ok=True)

    with open(output_internal, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["target", "source", "status"])
        writer.writeheader()
        writer.writerows(internal_broken)

    with open(output_external, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["target", "source", "status"])
        writer.writeheader()
        writer.writerows(external_broken)

    return {
        "start_url": start_url,
        "domain": domain,
        "pages_crawled": pages_crawled,
        "internal_links_checked": len(internal_checked),
        "external_links_checked": len(external_checked),
        "internal_broken": internal_broken,
        "external_broken": external_broken,
        "output_internal": output_internal,
        "output_external": output_external,
        "duration_seconds": time.time() - start_time,
        "status": status,
    }


# ==============================
# TKINTER GUI + SPLASH
# ==============================

class LinkCheckerGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.splash = None

        self.title("SitePulse — Website Health Monitor")
        # default size; may be overridden by saved config
        self.geometry("900x880")
        self.minsize(900, 760)

        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        # theme state
        self.dark_mode_var = tk.BooleanVar(value=False)

        # these will be set in _apply_theme
        self.colors = {}

        self.crawl_thread = None
        self.cancel_event = threading.Event()
        self.current_stats = None

        # scheduler state
        self.scheduler_thread = None
        self.scheduler_stop_event = threading.Event()
        self.autoscan_enabled_var = tk.BooleanVar(value=False)

        # weekly schedule state: booleans for each weekday
        self.autoscan_days = {
            "Mon": tk.BooleanVar(value=False),
            "Tue": tk.BooleanVar(value=False),
            "Wed": tk.BooleanVar(value=False),
            "Thu": tk.BooleanVar(value=False),
            "Fri": tk.BooleanVar(value=False),
            "Sat": tk.BooleanVar(value=False),
            "Sun": tk.BooleanVar(value=False),
        }
        # time of day for scheduled scan
        self.autoscan_time_var = tk.StringVar(value="02:00")  # default 2am

        # state for ignore placeholder / advanced accordion
        self.ignore_is_placeholder = True
        self.advanced_open = False

        self._show_splash()
        self._build_ui()
        self._load_settings()
        self._apply_theme()
        self.after(1400, self._end_splash)

        # hook close to save settings
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- Splash Screen ----------
    def _show_splash(self):
        splash = tk.Toplevel(self)
        self.splash = splash
        splash.overrideredirect(True)

        width, height = 360, 240
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = (sw // 2) - (width // 2)
        y = (sh // 2) - (height // 2)
        splash.geometry(f"{width}x{height}+{x}+{y}")

        splash.configure(bg="#ffffff")

        tk.Label(
            splash,
            text="SitePulse",
            font=("Helvetica", 20, "bold"),
            fg="#111827",
            bg="#ffffff",
        ).pack(pady=(40, 4))
        tk.Label(
            splash,
            text="Website Health Monitor",
            font=("Helvetica", 12),
            fg="#6b7280",
            bg="#ffffff",
        ).pack(pady=(0, 16))
        tk.Label(
            splash,
            text="Loading…",
            font=("Helvetica", 11),
            fg="#22c55e",
            bg="#ffffff",
        ).pack()

    def _end_splash(self):
        if self.splash:
            try:
                self.splash.destroy()
            except Exception:
                pass
        self.deiconify()

    # ---------- Main UI ----------
    def _build_ui(self):
        header = ttk.Frame(self, padding=(16, 12))
        header.pack(fill=tk.X)

        ttk.Label(header, text="SitePulse", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Label(
            header,
            text="Website link health & integrity monitor",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT, padx=(12, 0))

        tk.Frame(self, height=1, bg="#e5e7eb").pack(fill=tk.X, pady=(0, 8))

        main = ttk.Frame(self, padding=(16, 8))
        main.pack(fill=tk.BOTH, expand=True)

        # ===== Top form =====
        form = ttk.Frame(main)
        form.pack(fill=tk.X, pady=(0, 8))

        # Website URL
        row = 0
        ttk.Label(form, text="Website URL:").grid(
            row=row, column=0, sticky="w", padx=(0, 8)
        )
        self.url_var = tk.StringVar(value=DEFAULT_START_URL)
        ttk.Entry(form, textvariable=self.url_var).grid(
            row=row, column=1, sticky="we"
        )

        # Ignore Patterns (under URL) — with tooltip + placeholder
        row += 1
        ignore_label_frame = ttk.Frame(form)
        ignore_label_frame.grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=(4, 0)
        )

        ttk.Label(
            ignore_label_frame,
            text="Ignore patterns (comma-separated):",
        ).pack(side=tk.LEFT)

        ignore_info_icon = ttk.Label(
            ignore_label_frame,
            text="ⓘ",
            foreground="#6b7280",
        )
        ignore_info_icon.pack(side=tk.LEFT, padx=(4, 0))

        self.ignore_var = tk.StringVar(value="")
        self.ignore_entry = ttk.Entry(form, textvariable=self.ignore_var)
        self.ignore_entry.grid(row=row, column=1, sticky="we", pady=(4, 0))

        # Insert placeholder text initially
        self.ignore_entry.insert(0, IGNORE_PLACEHOLDER)
        self.ignore_is_placeholder = True
        self.ignore_entry.configure(foreground="#9ca3af")

        # Bind focus events to simulate placeholder
        self.ignore_entry.bind("<FocusIn>", self._on_ignore_focus_in)
        self.ignore_entry.bind("<FocusOut>", self._on_ignore_focus_out)

        # Ignore tooltip popup
        self.ignore_tooltip = tk.Toplevel(self, bg="white")
        self.ignore_tooltip.withdraw()
        self.ignore_tooltip.overrideredirect(True)
        self.ignore_tooltip.attributes("-topmost", True)

        ignore_msg = tk.Label(
            self.ignore_tooltip,
            text=(
                "Any URL containing one of these patterns will be skipped for crawling and link checks.\n"
                "Example: '/blog/' will skip all URLs that contain '/blog/'.\n"
                "The gray placeholder is just a suggestion and is ignored unless you edit this field."
            ),
            background="white",
            foreground="#111827",
            font=("Helvetica", 10),
            justify="left",
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=6,
        )
        ignore_msg.pack()

        def show_ignore_tooltip(event):
            x = self.winfo_pointerx() + 12
            y = self.winfo_pointery() + 12
            self.ignore_tooltip.geometry(f"+{x}+{y}")
            self.ignore_tooltip.deiconify()

        def hide_ignore_tooltip(event):
            self.ignore_tooltip.withdraw()

        ignore_info_icon.bind("<Enter>", show_ignore_tooltip)
        ignore_info_icon.bind("<Leave>", hide_ignore_tooltip)

        # Max pages
        row += 1
        ttk.Label(form, text="Max pages:").grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=(8, 4)
        )
        self.max_pages_var = tk.StringVar(value=str(DEFAULT_MAX_PAGES))
        ttk.Entry(form, textvariable=self.max_pages_var, width=12).grid(
            row=row, column=1, sticky="w"
        )

        # Threads (with tooltip)
        row += 1
        threads_label_frame = ttk.Frame(form)
        threads_label_frame.grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=(0, 4)
        )

        ttk.Label(threads_label_frame, text="Threads:").pack(side=tk.LEFT)

        threads_info_icon = ttk.Label(
            threads_label_frame,
            text="ⓘ",
            foreground="#6b7280",
        )
        threads_info_icon.pack(side=tk.LEFT, padx=(4, 0))

        self.max_workers_var = tk.StringVar(value=str(DEFAULT_MAX_WORKERS))
        ttk.Entry(form, textvariable=self.max_workers_var, width=12).grid(
            row=row, column=1, sticky="w"
        )

        # Threads tooltip popup
        self.threads_tooltip = tk.Toplevel(self, bg="white")
        self.threads_tooltip.withdraw()
        self.threads_tooltip.overrideredirect(True)
        self.threads_tooltip.attributes("-topmost", True)

        threads_msg = tk.Label(
            self.threads_tooltip,
            text=(
                "Number of worker threads used to check links in parallel.\n"
                "Higher = faster scans, but more load on your machine and the target site.\n"
                "Typical range: 10–40. Reduce if a site seems slow or rate-limited."
            ),
            background="white",
            foreground="#111827",
            font=("Helvetica", 10),
            justify="left",
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=6,
        )
        threads_msg.pack()

        def show_threads_tooltip(event):
            x = self.winfo_pointerx() + 12
            y = self.winfo_pointery() + 12
            self.threads_tooltip.geometry(f"+{x}+{y}")
            self.threads_tooltip.deiconify()

        def hide_threads_tooltip(event):
            self.threads_tooltip.withdraw()

        threads_info_icon.bind("<Enter>", show_threads_tooltip)
        threads_info_icon.bind("<Leave>", hide_threads_tooltip)

        # External toggle
        row += 1
        self.check_external_var = tk.BooleanVar(value=DEFAULT_CHECK_EXTERNAL_LINKS)
        ttk.Checkbutton(
            form,
            text="Check external links",
            variable=self.check_external_var,
        ).grid(
            row=row, column=1, sticky="w", pady=(4, 4)
        )

        form.columnconfigure(1, weight=1)

        # ===== Buttons =====
        btn_row = ttk.Frame(main)
        btn_row.pack(fill=tk.X, pady=(10, 4))

        self.start_btn = ttk.Button(
            btn_row,
            text="Start Scan",
            style="Accent.TButton",
            command=self.on_start,
        )
        self.start_btn.pack(side=tk.LEFT)

        self.cancel_btn = ttk.Button(
            btn_row,
            text="Cancel",
            style="Plain.TButton",
            command=self.on_cancel,
            state=tk.DISABLED,
        )
        self.cancel_btn.pack(side=tk.LEFT, padx=(8, 0))

        # ===== Progress + Status =====
        prog = ttk.Frame(main)
        prog.pack(fill=tk.X, pady=(4, 4))

        self.progress_var = tk.DoubleVar(value=0.0)
        ttk.Progressbar(
            prog,
            variable=self.progress_var,
            maximum=100,
        ).pack(fill=tk.X)

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(
            prog,
            textvariable=self.status_var,
            style="Muted.TLabel",
        ).pack(anchor="w")

        tk.Frame(main, height=1, bg="#e5e7eb").pack(fill=tk.X, pady=(4, 8))

        # ===== Stats =====
        stats = ttk.Frame(main)
        stats.pack(fill=tk.X, pady=(0, 4))

        self.pages_label_var = tk.StringVar(value="Pages crawled: 0")
        self.internal_label_var = tk.StringVar(value="Internal links checked: 0")
        self.external_label_var = tk.StringVar(value="External links checked: 0")

        ttk.Label(stats, textvariable=self.pages_label_var).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(stats, textvariable=self.internal_label_var).grid(
            row=0, column=1, sticky="w", padx=(16, 0)
        )
        ttk.Label(stats, textvariable=self.external_label_var).grid(
            row=0, column=2, sticky="w", padx=(16, 0)
        )

        self.open_internal_btn = ttk.Button(
            stats,
            text="Open Internal CSV",
            style="Plain.TButton",
            command=self.open_internal_csv,
            state=tk.DISABLED,
        )
        self.open_internal_btn.grid(
            row=1,
            column=0,
            sticky="w",
            pady=(8, 0),
        )

        self.open_external_btn = ttk.Button(
            stats,
            text="Open External CSV",
            style="Plain.TButton",
            command=self.open_external_csv,
            state=tk.DISABLED,
        )
        self.open_external_btn.grid(
            row=1,
            column=1,
            sticky="w",
            padx=(8, 0),
            pady=(8, 0),
        )

        # Open Reports Folder button
        self.open_reports_btn = ttk.Button(
            stats,
            text="Open Reports Folder",
            style="Plain.TButton",
            command=self._open_reports_folder,
        )
        self.open_reports_btn.grid(
            row=1,
            column=2,
            sticky="w",
            padx=(8, 0),
            pady=(8, 0),
        )

        stats.columnconfigure(3, weight=1)

        # ===== Scan Summary =====
        summary = ttk.Frame(main)
        summary.pack(fill=tk.X, pady=(4, 4))

        self.summary_title_var = tk.StringVar(
            value="Scan summary: no scans run yet"
        )
        self.summary_internal_var = tk.StringVar(
            value="Broken internal links: 0"
        )
        self.summary_external_var = tk.StringVar(
            value="Broken external links: 0"
        )
        self.summary_duration_var = tk.StringVar(
            value="Duration: 0.00 sec"
        )

        ttk.Label(
            summary,
            textvariable=self.summary_title_var,
            style="Muted.TLabel",
        ).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 2)
        )
        ttk.Label(
            summary,
            textvariable=self.summary_internal_var,
        ).grid(
            row=1, column=0, sticky="w", padx=(0, 16)
        )
        ttk.Label(
            summary,
            textvariable=self.summary_external_var,
        ).grid(
            row=1, column=1, sticky="w", padx=(0, 16)
        )
        ttk.Label(
            summary,
            textvariable=self.summary_duration_var,
        ).grid(
            row=1, column=2, sticky="w"
        )

        summary.columnconfigure(3, weight=1)

        # ===== Activity Log =====
        ttk.Label(
            main,
            text="Activity log",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(12, 4))

        log_frame = ttk.Frame(main)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            height=8,  # constrain so advanced section is visible
            background="#f9fafb",
            foreground="#111827",
            insertbackground="#111827",
            borderwidth=1,
            relief="solid",
            font=("Menlo", 10),
        )
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scroll = ttk.Scrollbar(
            log_frame,
            orient="vertical",
            command=self.log_text.yview,
        )
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=scroll.set)

        # ===== Advanced Settings (under Activity Log) =====
        adv_container = ttk.Frame(main)
        adv_container.pack(fill=tk.X, pady=(8, 20))

        # header row (white, minimal, smaller text)
        self.adv_header_frame = ttk.Frame(adv_container)
        self.adv_header_frame.pack(fill=tk.X)

        self.adv_arrow_var = tk.StringVar(value="▶")
        self.adv_arrow_label = ttk.Label(
            self.adv_header_frame,
            textvariable=self.adv_arrow_var,
        )
        self.adv_arrow_label.grid(row=0, column=0, sticky="w")

        # smaller font for the title
        self.adv_title_label = ttk.Label(
            self.adv_header_frame,
            text="Advanced settings",
            style="Muted.TLabel",
            font=("Helvetica", 9),
        )
        self.adv_title_label.grid(row=0, column=1, sticky="w", padx=(4, 0))

        self.adv_header_frame.columnconfigure(0, weight=0)
        self.adv_header_frame.columnconfigure(1, weight=1)

        # make whole header clickable
        self.adv_header_frame.bind("<Button-1>", self._toggle_advanced)
        self.adv_arrow_label.bind("<Button-1>", self._toggle_advanced)
        self.adv_title_label.bind("<Button-1>", self._toggle_advanced)

        # Advanced content (Slack + crawl controls) frame below header
        self.adv_frame = ttk.Frame(adv_container)
        self.adv_frame.pack(fill=tk.X, pady=(2, 0))

        ttk.Label(self.adv_frame, text="Slack webhook (optional):").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=(4, 0)
        )
        self.slack_var = tk.StringVar(
            value=os.getenv("SLACK_WEBHOOK_URL", "")
        )
        ttk.Entry(self.adv_frame, textvariable=self.slack_var).grid(
            row=0, column=1, sticky="we", pady=(4, 0)
        )
        self.adv_frame.columnconfigure(1, weight=1)

        ttk.Label(
            self.adv_frame,
            text=(
                "If provided, SitePulse will post a summary message to this Slack webhook "
                "when a scan finishes (pages crawled, broken links, and report paths)."
            ),
            style="Muted.TLabel",
            wraplength=520,
            justify="left",
        ).grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="w",
            padx=(0, 8),
            pady=(2, 8),
        )

        # --- Crawl control options ---

        # Max depth with tooltip
        depth_label_frame = ttk.Frame(self.adv_frame)
        depth_label_frame.grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=(0, 4)
        )

        ttk.Label(
            depth_label_frame,
            text="Max depth (0 = start page, blank = unlimited):",
        ).pack(side=tk.LEFT)

        # Tooltip icon
        tooltip_icon = ttk.Label(
            depth_label_frame,
            text="ⓘ",
            foreground="#6b7280",
        )
        tooltip_icon.pack(side=tk.LEFT, padx=(4, 0))

        # Tooltip popup
        self.depth_tooltip = tk.Toplevel(self, bg="white")
        self.depth_tooltip.withdraw()
        self.depth_tooltip.overrideredirect(True)
        self.depth_tooltip.attributes("-topmost", True)

        tooltip_msg = tk.Label(
            self.depth_tooltip,
            text=(
                "Controls how many link levels SitePulse will crawl.\n"
                "0 = only the start page\n"
                "1 = start page + pages linked from it\n"
                "2 = one level deeper, etc.\n"
                "Blank = unlimited depth."
            ),
            background="white",
            foreground="#111827",
            font=("Helvetica", 10),
            justify="left",
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=6,
        )
        tooltip_msg.pack()

        def show_depth_tooltip(event):
            x = self.winfo_pointerx() + 12
            y = self.winfo_pointery() + 12
            self.depth_tooltip.geometry(f"+{x}+{y}")
            self.depth_tooltip.deiconify()

        def hide_depth_tooltip(event):
            self.depth_tooltip.withdraw()

        tooltip_icon.bind("<Enter>", show_depth_tooltip)
        tooltip_icon.bind("<Leave>", hide_depth_tooltip)

        # Max depth entry
        self.max_depth_var = tk.StringVar(value="")
        ttk.Entry(
            self.adv_frame,
            textvariable=self.max_depth_var,
            width=10,
        ).grid(
            row=2, column=1, sticky="w", pady=(0, 4)
        )

        # Include subdomains
        self.include_subdomains_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            self.adv_frame,
            text="Include subdomains",
            variable=self.include_subdomains_var,
        ).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(0, 2)
        )

        # Respect robots.txt
        self.respect_robots_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self.adv_frame,
            text="Respect robots.txt",
            variable=self.respect_robots_var,
        ).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )

        # Dark mode toggle
        self.dark_mode_checkbox = ttk.Checkbutton(
            self.adv_frame,
            text="Enable dark mode",
            variable=self.dark_mode_var,
            command=self._on_theme_toggle,
        )
        self.dark_mode_checkbox.grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )

        # --- Scheduled scans: weekly days + time ---
        # Days row
        ttk.Label(
            self.adv_frame,
            text="Scheduled scan days:",
        ).grid(
            row=6, column=0, sticky="nw", padx=(0, 8), pady=(4, 2)
        )

        days_frame = ttk.Frame(self.adv_frame)
        days_frame.grid(row=6, column=1, sticky="w", pady=(4, 2))

        # Order: Mon..Sun
        for idx, day in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
            ttk.Checkbutton(
                days_frame,
                text=day,
                variable=self.autoscan_days[day],
            ).grid(row=0, column=idx, sticky="w", padx=(0, 4))

        # Time row
        ttk.Label(
            self.adv_frame,
            text="Scheduled scan time (HH:MM, 24h):",
        ).grid(
            row=7, column=0, sticky="w", padx=(0, 8), pady=(0, 4)
        )
        ttk.Entry(
            self.adv_frame,
            textvariable=self.autoscan_time_var,
            width=10,
        ).grid(
            row=7, column=1, sticky="w", pady=(0, 4)
        )

        # Enable scheduled scans
        self.autoscan_checkbox = ttk.Checkbutton(
            self.adv_frame,
            text="Enable scheduled scans",
            variable=self.autoscan_enabled_var,
            command=self._on_schedule_toggle,
        )
        self.autoscan_checkbox.grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )

        # start collapsed
        self.adv_frame.pack_forget()
        self.advanced_open = False

    # ----- Theme handling -----

    def _apply_theme(self):
        """Apply light or dark theme based on self.dark_mode_var."""
        dark = bool(self.dark_mode_var.get())

        if dark:
            self.colors = {
                "bg": "#020617",           # near-black
                "frame_bg": "#020617",
                "text_main": "#e5e7eb",
                "text_muted": "#9ca3af",
                "accent": "#22c55e",
                "divider": "#111827",
                "log_bg": "#020617",
                "log_fg": "#e5e7eb",
                "tooltip_bg": "#111827",
                "tooltip_fg": "#e5e7eb",
                "entry_bg": "#020617",
                "entry_fg": "#e5e7eb",
            }
        else:
            self.colors = {
                "bg": "#ffffff",
                "frame_bg": "#ffffff",
                "text_main": "#111827",
                "text_muted": "#6b7280",
                "accent": "#22c55e",
                "divider": "#e5e7eb",
                "log_bg": "#f9fafb",
                "log_fg": "#111827",
                "tooltip_bg": "#ffffff",
                "tooltip_fg": "#111827",
                "entry_bg": "#ffffff",
                "entry_fg": "#111827",
            }

        c = self.colors

        self.configure(bg=c["bg"])
        # update style colors
        self.style.configure(
            "TFrame",
            background=c["frame_bg"],
        )
        self.style.configure(
            "TLabel",
            background=c["frame_bg"],
            foreground=c["text_main"],
            font=("Helvetica", 11),
        )
        self.style.configure(
            "Muted.TLabel",
            background=c["frame_bg"],
            foreground=c["text_muted"],
            font=("Helvetica", 11),
        )
        self.style.configure(
            "Header.TLabel",
            background=c["frame_bg"],
            foreground=c["text_main"],
            font=("Helvetica", 16, "bold"),
        )
        self.style.configure(
            "Accent.TButton",
            padding=6,
            background=c["accent"],
            foreground="#ffffff",
            borderwidth=0,
        )
        self.style.map(
            "Accent.TButton",
            background=[("active", c["accent"])],
        )
        self.style.configure(
            "Plain.TButton",
            padding=6,
            background="#e5e7eb" if not dark else "#1f2933",
            foreground=c["text_main"],
            borderwidth=0,
        )
        self.style.map(
            "Plain.TButton",
            background=[("active", "#d4d4d8" if not dark else "#374151")],
        )

        # log text colors
        if hasattr(self, "log_text"):
            self.log_text.configure(
                background=c["log_bg"],
                foreground=c["log_fg"],
                insertbackground=c["log_fg"],
            )

        # tooltips
        for tooltip_attr in ("ignore_tooltip", "threads_tooltip", "depth_tooltip"):
            tooltip = getattr(self, tooltip_attr, None)
            if isinstance(tooltip, tk.Toplevel):
                tooltip.configure(bg=c["tooltip_bg"])
                # each tooltip has exactly one label child
                for child in tooltip.winfo_children():
                    if isinstance(child, tk.Label):
                        child.configure(
                            background=c["tooltip_bg"],
                            foreground=c["tooltip_fg"],
                        )

    def _on_theme_toggle(self):
        self._apply_theme()
        # save settings immediately so theme persists
        self._save_settings()

    # ----- Settings persistence -----

    def _load_settings(self):
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        # Window size
        width = data.get("window_width")
        height = data.get("window_height")
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            self.geometry(f"{width}x{height}")

        # URL
        url = data.get("url")
        if isinstance(url, str) and url.strip():
            self.url_var.set(url.strip())

        # Ignore patterns
        ignore_patterns_str = data.get("ignore_patterns")
        if isinstance(ignore_patterns_str, str) and ignore_patterns_str.strip():
            # user actually set something; override placeholder
            self.ignore_is_placeholder = False
            self.ignore_entry.configure(foreground="#111827")
            self.ignore_entry.delete(0, tk.END)
            self.ignore_entry.insert(0, ignore_patterns_str)
            self.ignore_var.set(ignore_patterns_str)

        # Max pages
        max_pages = data.get("max_pages")
        if isinstance(max_pages, int) and max_pages > 0:
            self.max_pages_var.set(str(max_pages))

        # Threads
        threads = data.get("threads")
        if isinstance(threads, int) and threads > 0:
            self.max_workers_var.set(str(threads))

        # Check external
        check_ext = data.get("check_external_links")
        if isinstance(check_ext, bool):
            self.check_external_var.set(check_ext)

        # Max depth
        max_depth = data.get("max_depth")
        if isinstance(max_depth, int) and max_depth >= 0:
            self.max_depth_var.set(str(max_depth))

        # Include subdomains
        include_sub = data.get("include_subdomains")
        if isinstance(include_sub, bool):
            self.include_subdomains_var.set(include_sub)

        # Respect robots
        respect_robots = data.get("respect_robots")
        if isinstance(respect_robots, bool):
            self.respect_robots_var.set(respect_robots)

        # Slack webhook
        slack = data.get("slack_webhook")
        if isinstance(slack, str):
            self.slack_var.set(slack)

        # Dark mode
        dark_mode = data.get("dark_mode")
        if isinstance(dark_mode, bool):
            self.dark_mode_var.set(dark_mode)

        # Scheduled scans: days + time + enabled
        autoscan_enabled = data.get("autoscan_enabled")
        if isinstance(autoscan_enabled, bool):
            self.autoscan_enabled_var.set(autoscan_enabled)

        autoscan_time = data.get("autoscan_time")
        if isinstance(autoscan_time, str) and autoscan_time.strip():
            self.autoscan_time_var.set(autoscan_time.strip())

        autoscan_days = data.get("autoscan_days")
        if isinstance(autoscan_days, dict):
            for day, var in self.autoscan_days.items():
                val = autoscan_days.get(day)
                if isinstance(val, bool):
                    var.set(val)

        # If autoscan was enabled last time, start scheduler now
        if isinstance(autoscan_enabled, bool) and autoscan_enabled:
            self._on_schedule_toggle()

    def _save_settings(self):
        try:
            # current window size
            try:
                width = self.winfo_width()
                height = self.winfo_height()
            except Exception:
                width = 900
                height = 880

            # ignore patterns string (if placeholder, store empty)
            if self.ignore_is_placeholder:
                ignore_str = ""
            else:
                ignore_str = self.ignore_var.get().strip()

            autoscan_days_data = {
                day: bool(var.get())
                for day, var in self.autoscan_days.items()
            }

            payload = {
                "window_width": width,
                "window_height": height,
                "url": self.url_var.get().strip(),
                "ignore_patterns": ignore_str,
                "max_pages": int(self.max_pages_var.get().strip()) if self.max_pages_var.get().strip().isdigit() else None,
                "threads": int(self.max_workers_var.get().strip()) if self.max_workers_var.get().strip().isdigit() else None,
                "check_external_links": bool(self.check_external_var.get()),
                "max_depth": int(self.max_depth_var.get().strip()) if self.max_depth_var.get().strip().isdigit() else None,
                "include_subdomains": bool(self.include_subdomains_var.get()),
                "respect_robots": bool(self.respect_robots_var.get()),
                "slack_webhook": self.slack_var.get().strip(),
                "dark_mode": bool(self.dark_mode_var.get()),
                "autoscan_enabled": bool(self.autoscan_enabled_var.get()),
                "autoscan_time": self.autoscan_time_var.get().strip(),
                "autoscan_days": autoscan_days_data,
            }

            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            # don't crash on save errors
            pass

    # ----- Placeholder handlers for ignore patterns -----

    def _on_ignore_focus_in(self, event):
        if self.ignore_is_placeholder:
            self.ignore_entry.delete(0, tk.END)
            fg = self.colors.get("entry_fg", "#111827")
            self.ignore_entry.configure(foreground=fg)
            self.ignore_is_placeholder = False

    def _on_ignore_focus_out(self, event):
        text = self.ignore_entry.get().strip()
        if not text:
            self.ignore_entry.delete(0, tk.END)
            self.ignore_entry.insert(0, IGNORE_PLACEHOLDER)
            self.ignore_entry.configure(foreground="#9ca3af")  # placeholder gray
            self.ignore_is_placeholder = True

    # ----- Advanced accordion -----

    def _toggle_advanced(self, event=None):
        self.advanced_open = not self.advanced_open
        if self.advanced_open:
            self.adv_arrow_var.set("▼")
            self.adv_frame.pack(fill=tk.X, pady=(2, 0))
        else:
            self.adv_arrow_var.set("▶")
            self.adv_frame.pack_forget()

    # ----- Scheduler -----

    def _on_schedule_toggle(self):
        """Called when 'Enable scheduled scans' is toggled."""
        enabled = bool(self.autoscan_enabled_var.get())

        if self.scheduler_stop_event is None:
            self.scheduler_stop_event = threading.Event()
        else:
            self.scheduler_stop_event.clear()

        if enabled:
            # start scheduler thread if not running
            if not self.scheduler_thread or not self.scheduler_thread.is_alive():
                self.scheduler_thread = threading.Thread(
                    target=self._scheduler_loop,
                    daemon=True,
                )
                self.scheduler_thread.start()
                self.log("Scheduled scans enabled.")
        else:
            # stop scheduler loop
            if self.scheduler_stop_event is not None:
                self.scheduler_stop_event.set()
            self.log("Scheduled scans disabled.")

        # persist change
        self._save_settings()

    def _compute_next_run(self):
        """
        Compute the next datetime when a scheduled scan should run,
        based on selected days + autoscan_time.
        Returns a datetime in the future (or None if no days selected or invalid time).
        """
        # Which days are selected?
        selected_days = [d for d, var in self.autoscan_days.items() if var.get()]
        if not selected_days:
            return None

        # Map day label to weekday index: Mon=0 ... Sun=6
        day_to_idx = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
        selected_indices = [day_to_idx[d] for d in selected_days if d in day_to_idx]

        # Parse time
        time_str = (self.autoscan_time_var.get() or "").strip()
        try:
            hour, minute = map(int, time_str.split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError()
        except Exception:
            # invalid time
            return None

        now = datetime.now()
        now_weekday = now.weekday()  # 0=Mon

        # Find the earliest upcoming scheduled datetime in the next 7 days
        candidates = []
        for idx in selected_indices:
            # how many days ahead is this day from today?
            delta_days = (idx - now_weekday) % 7
            candidate_date = now.date() + timedelta(days=delta_days)
            candidate_dt = datetime(
                candidate_date.year,
                candidate_date.month,
                candidate_date.day,
                hour,
                minute,
            )
            # if candidate time is in the past for "today", push to next week
            if candidate_dt <= now:
                candidate_dt += timedelta(days=7)
            candidates.append(candidate_dt)

        if not candidates:
            return None

        return min(candidates)

    def _scheduler_loop(self):
        """Background loop that triggers scans on a weekly day+time schedule."""
        while not self.scheduler_stop_event.is_set():
            next_run = self._compute_next_run()

            if next_run is None:
                # nothing to schedule (no days or invalid time); wait a bit and retry
                self.log("Scheduled scan: no valid days/time configured; waiting...")
                for _ in range(60):
                    if self.scheduler_stop_event.is_set():
                        return
                    time.sleep(1)
                continue

            now = datetime.now()
            wait_seconds = max(5.0, (next_run - now).total_seconds())

            self.log(f"Next scheduled scan at {next_run.strftime('%Y-%m-%d %H:%M')}")

            waited = 0.0
            chunk = 1.0
            while waited < wait_seconds and not self.scheduler_stop_event.is_set():
                time.sleep(chunk)
                waited += chunk

            if self.scheduler_stop_event.is_set():
                break

            # schedule a scan on the main thread
            self.after(0, self._trigger_scheduled_scan)

            # loop back to compute the *next* run (in the next week window)

    def _trigger_scheduled_scan(self):
        """Called by scheduler on the main thread; starts a scan if idle."""
        if self.crawl_thread and self.crawl_thread.is_alive():
            self.log("Scheduled scan skipped (another scan is already running).")
            return

        self.log("Starting scheduled scan…")
        self.on_start()

    # ----- Helpers -----

    def log(self, msg):
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)

    def set_status(self, msg):
        self.status_var.set(msg)

    def update_progress_from_stats(self, s):
        pages = s.get("pages_crawled", 0)
        internal = s.get("internal_links_checked", 0)
        external = s.get("external_links_checked", 0)
        est = s.get("estimated_total_pages", 100)

        self.pages_label_var.set(f"Pages crawled: {pages}")
        self.internal_label_var.set(f"Internal links checked: {internal}")
        self.external_label_var.set(f"External links checked: {external}")

        pct = min(100.0, (pages / est) * 100.0 if est else 0.0)
        self.progress_var.set(pct)

    def _open_reports_folder(self):
        try:
            os.makedirs(REPORTS_DIR, exist_ok=True)
            if os.name == "nt":
                os.startfile(REPORTS_DIR)  # type: ignore[attr-defined]
            else:
                os.system(f"open '{REPORTS_DIR}'")
        except Exception:
            messagebox.showerror("Error", "Could not open reports folder.")

    # ----- Button Handlers -----

    def on_start(self):
        if self.crawl_thread and self.crawl_thread.is_alive():
            messagebox.showinfo("Scan in progress", "A scan is already running.")
            return

        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Error", "Please enter a website URL.")
            return

        try:
            max_pages = int(self.max_pages_var.get().strip())
        except Exception:
            messagebox.showerror("Error", "Max pages must be an integer.")
            return

        try:
            max_workers = int(self.max_workers_var.get().strip())
        except Exception:
            messagebox.showerror("Error", "Threads must be an integer.")
            return

        # Optional max depth
        raw_depth = self.max_depth_var.get().strip() if hasattr(self, "max_depth_var") else ""
        if raw_depth == "":
            max_depth = None
        else:
            try:
                max_depth = int(raw_depth)
                if max_depth < 0:
                    raise ValueError()
            except Exception:
                messagebox.showerror("Error", "Max depth must be a non-negative integer or blank.")
                return

        include_subdomains = (
            self.include_subdomains_var.get()
            if hasattr(self, "include_subdomains_var")
            else True
        )
        respect_robots = (
            self.respect_robots_var.get()
            if hasattr(self, "respect_robots_var")
            else False
        )

        # Ignore patterns: only apply if user actually changed/typed them
        if self.ignore_is_placeholder:
            ignore_patterns = []
        else:
            raw = self.ignore_var.get().strip()
            ignore_patterns = [p.strip() for p in raw.split(",") if p.strip()]

        slack_webhook = self.slack_var.get().strip() or None

        self.cancel_event.clear()
        self.progress_var.set(0)
        self.set_status("Starting scan…")

        self.log(f"URL: {url}")
        self.log(f"Ignore patterns: {ignore_patterns}")
        self.log(f"Threads: {max_workers}, Max pages: {max_pages}, Max depth: {max_depth}")
        self.log(f"Include subdomains: {include_subdomains}, Respect robots.txt: {respect_robots}")
        self.log("Starting scan…")

        self.start_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)

        def worker():
            try:
                os.makedirs(REPORTS_DIR, exist_ok=True)

                ts = time.strftime("%Y%m%d-%H%M%S")
                out_int = os.path.join(REPORTS_DIR, f"sitepulse_internal_{ts}.csv")
                out_ext = os.path.join(REPORTS_DIR, f"sitepulse_external_{ts}.csv")

                stats = run_crawl(
                    start_url=url,
                    domain=urlparse(url).netloc.lower(),
                    output_internal=out_int,
                    output_external=out_ext,
                    max_pages=max_pages,
                    max_workers=max_workers,
                    request_timeout=DEFAULT_REQUEST_TIMEOUT,
                    delay_between_pages=DEFAULT_DELAY_BETWEEN_PAGES,
                    check_external_links=self.check_external_var.get(),
                    headers=DEFAULT_HEADERS,
                    progress_callback=lambda s: self.after(0, self.update_progress_from_stats, s),
                    cancel_event=self.cancel_event,
                    ignore_patterns=ignore_patterns,
                    max_depth=max_depth,
                    include_subdomains=include_subdomains,
                    respect_robots=respect_robots,
                    retry_failed=True,
                )

                self.current_stats = stats

                def finish():
                    self.start_btn.config(state=tk.NORMAL)
                    self.cancel_btn.config(state=tk.DISABLED)

                    if stats["status"] == "cancelled":
                        self.set_status("Scan cancelled.")
                        self.log("Scan cancelled.")
                    else:
                        self.set_status("Scan complete.")
                        self.log("Scan complete.")

                    self.open_internal_btn.config(state=tk.NORMAL)
                    self.open_external_btn.config(
                        state=tk.NORMAL if self.check_external_var.get() else tk.DISABLED
                    )

                    self.log(f"Internal CSV: {stats['output_internal']}")
                    self.log(f"External CSV: {stats['output_external']}")

                    # Update scan summary panel
                    self.summary_title_var.set(f"Scan summary for {stats['start_url']}")
                    self.summary_internal_var.set(
                        f"Broken internal links: {len(stats['internal_broken'])}"
                    )
                    self.summary_external_var.set(
                        f"Broken external links: {len(stats['external_broken'])}"
                    )
                    self.summary_duration_var.set(
                        f"Duration: {stats['duration_seconds']:.2f} sec"
                    )

                    # Save settings after a successful scan
                    self._save_settings()

                    if slack_webhook:
                        try:
                            message = (
                                f"Scan complete for {stats['start_url']}\n"
                                f"Pages: {stats['pages_crawled']}\n"
                                f"Internal broken: {len(stats['internal_broken'])}\n"
                                f"External broken: {len(stats['external_broken'])}\n"
                                f"Duration: {stats['duration_seconds']:.2f} sec"
                            )
                            requests.post(slack_webhook, json={"text": message}, timeout=5)
                            self.log("Slack notification sent.")
                        except Exception as e:
                            self.log(f"Slack error: {e}")

                self.after(0, finish)

            except Exception as e:
                def err():
                    self.start_btn.config(state=tk.NORMAL)
                    self.cancel_btn.config(state=tk.DISABLED)
                    self.set_status("Error during scan.")
                    self.log(f"Error: {e}")
                    messagebox.showerror("Error", f"Scan error:\n{e}")
                self.after(0, err)

        self.crawl_thread = threading.Thread(target=worker, daemon=True)
        self.crawl_thread.start()

    def on_cancel(self):
        if self.crawl_thread and self.crawl_thread.is_alive():
            self.cancel_event.set()
            self.set_status("Cancelling scan…")
            self.log("Cancel requested…")

    def on_close(self):
        # stop scheduler if running
        try:
            if self.scheduler_stop_event is not None:
                self.scheduler_stop_event.set()
        except Exception:
            pass

        # persist settings on close
        self._save_settings()
        self.destroy()

    def open_internal_csv(self):
        if not self.current_stats:
            return
        path = self.current_stats["output_internal"]
        if os.path.exists(path):
            self._open_file(path)
        else:
            messagebox.showerror("Error", "Internal CSV file not found.")

    def open_external_csv(self):
        if not self.current_stats:
            return
        path = self.current_stats["output_external"]
        if os.path.exists(path):
            self._open_file(path)
        else:
            messagebox.showerror("Error", "External CSV file not found.")

    def _open_file(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                os.system(f"open '{path}'")
        except Exception:
            messagebox.showerror("Error", "Could not open file.")


if __name__ == "__main__":
    app = LinkCheckerGUI()
    app.mainloop()
