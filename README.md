# SitePulse 🌐

**SitePulse** is a Python-based desktop utility designed to audit website health by crawling links and identifying broken connections in real-time.

---

## 🚀 Overview
Maintaining a website means ensuring every link leads where it should. **SitePulse** automates the tedious task of manual link checking. By providing a starting URL, the application recursively crawls the site to find 404 errors, redirects, and broken paths, presenting them in a clean, user-friendly GUI.

## ✨ Key Features
* **Deep Crawling:** Automatically traverses a provided URL to discover nested pages and resources.
* **Live Status Reporting:** See the HTTP status of every link as the crawler finds them.
* **Desktop GUI:** No command-line knowledge required—simply paste a link and pulse the site.
* **Error Identification:** Quickly spot broken links that could hurt your SEO or user experience.

## 🛠️ Technical Stack
* **Language:** Python 3.x
* **Core Engine:** `link_checker_gui.py` (Custom Crawler Logic)
* **Communication:** Utilizes `requests` and `BeautifulSoup4` for high-frequency site crawling.
* **GUI Framework:** Python Desktop GUI.

## 📥 Installation & Setup
To run SitePulse from your source code, follow these steps:

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/huntertigert/Sitepulse.git](https://github.com/huntertigert/Sitepulse.git)
   cd Sitepulse

2. Install necessary libraries:
Bash
pip install requests beautifulsoup4

3. Launch the App:
Bash
python link_checker_gui.py

📝 Roadmap
[ ] Multi-threaded crawling for faster performance on large sites.

[ ] Export results to CSV or Excel for client reporting.

[ ] Visual sitemap generation.
