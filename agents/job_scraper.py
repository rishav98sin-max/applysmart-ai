# agents/job_scraper.py

import time
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ─────────────────────────────────────────────────────────────
# LINKEDIN — direct scraper (unchanged, working)
# ─────────────────────────────────────────────────────────────

def scrape_linkedin(job_title: str, location: str, num_jobs: int) -> list:
    print(f"   🔵 Scraping LinkedIn for '{job_title}' in '{location}'...")

    query = job_title.replace(" ", "%20")
    loc   = location.replace(" ", "%20").replace(",", "%2C")
    url   = (
        f"https://www.linkedin.com/jobs/search?"
        f"keywords={query}&location={loc}&f_TPR=r86400"
        f"&position=1&pageNum=0"
    )

    try:
        resp  = requests.get(url, headers=HEADERS, timeout=15)
        soup  = BeautifulSoup(resp.text, "html.parser")
        cards = soup.find_all("div", class_="base-card")[:num_jobs]

        jobs = []
        for card in cards:
            try:
                title_el    = card.find("h3", class_="base-search-card__title")
                company_el  = card.find("h4", class_="base-search-card__subtitle")
                location_el = card.find("span", class_="job-search-card__location")
                link_el     = card.find("a", class_="base-card__full-link")
                time_el     = card.find("time")

                title        = title_el.get_text(strip=True)    if title_el    else "N/A"
                company      = company_el.get_text(strip=True)   if company_el  else "N/A"
                loc_txt      = location_el.get_text(strip=True)  if location_el else "N/A"
                link         = link_el["href"]                   if link_el     else ""
                posted_label = time_el.get_text(strip=True)      if time_el     else "N/A"

                description = _fetch_linkedin_description(link)

                jobs.append({
                    "title":        title,
                    "company":      company,
                    "location":     loc_txt,
                    "url":          link,
                    "description":  description,
                    "posted":       posted_label,
                    "posted_label": posted_label,
                    "source":       "LinkedIn",
                })
            except Exception as e:
                print(f"   ⚠️  LinkedIn card parse error: {e}")
                continue

        print(f"   ✅ LinkedIn: {len(jobs)} jobs found")
        return jobs

    except Exception as e:
        print(f"   ❌ LinkedIn scrape failed: {e}")
        return []


def _fetch_linkedin_description(url: str) -> str:
    if not url:
        return ""
    
    # Try twice with a delay between attempts
    for attempt in range(2):
        try:
            if attempt > 0:
                print(f"   🔄 Retrying LinkedIn description fetch (attempt {attempt + 1})...")
                time.sleep(2)
            else:
                time.sleep(1)
            
            resp = requests.get(url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")

            desc = (
                soup.find("div", class_="description__text") or
                soup.find("div", class_="show-more-less-html__markup") or
                soup.find("section", class_="show-more-less-html")
            )
            result = desc.get_text(separator=" ", strip=True) if desc else ""
            
            # If we got a description, return it
            if result and len(result.strip()) >= 30:
                return result
            # If empty on first attempt, try again
            if attempt == 0:
                continue
            return result
        except Exception as e:
            print(f"   ⚠️  LinkedIn description fetch failed (attempt {attempt + 1}): {e}")
            if attempt == 0:
                continue
            return ""
    
    return ""


# ─────────────────────────────────────────────────────────────
# JOBSPY — Indeed, Glassdoor, Builtin
# ─────────────────────────────────────────────────────────────

def _scrape_via_jobspy(
    job_title:    str,
    location:     str,
    num_jobs:     int,
    site:         str,
    source_label: str,
) -> list:
    print(f"   🟠 Scraping {source_label} via jobspy for '{job_title}' in '{location}'...")
    try:
        from jobspy import scrape_jobs as jobspy_scrape

        fetch_count = num_jobs * 3   # fetch 3x — let matcher filter quality

        # Extract country from location for Indeed
        # Default to Ireland if location is empty or doesn't contain a country
        country_indeed = "Ireland"
        if location:
            loc_lower = location.lower()
            # Common country names to detect
            country_map = {
                "usa": "United States",
                "united states": "United States",
                "uk": "United Kingdom",
                "united kingdom": "United Kingdom",
                "ireland": "Ireland",
                "canada": "Canada",
                "australia": "Australia",
                "germany": "Germany",
                "france": "France",
                "netherlands": "Netherlands",
                "spain": "Spain",
                "italy": "Italy",
            }
            for country_key, country_value in country_map.items():
                if country_key in loc_lower:
                    country_indeed = country_value
                    break

        df = jobspy_scrape(
            site_name                  = [site],
            search_term                = job_title,
            location                   = location,
            results_wanted             = fetch_count,
            hours_old                  = 168,          # 7 days
            country_indeed             = country_indeed,
            linkedin_fetch_description = False,
        )

        if df is None or df.empty:
            print(f"   ⚠️  No jobs returned from {source_label}")
            return []

        jobs = []
        for _, row in df.iterrows():
            try:
                description = " ".join(filter(None, [
                    str(row.get("description",     "") or ""),
                    str(row.get("job_type",         "") or ""),
                    str(row.get("company_industry", "") or ""),
                ]))

                jobs.append({
                    "title":        str(row.get("title",       "N/A") or "N/A"),
                    "company":      str(row.get("company",     "N/A") or "N/A"),
                    "location":     str(row.get("location",    location) or location),
                    "url":          str(row.get("job_url",     "") or ""),
                    "description":  description,
                    "posted":       str(row.get("date_posted", "") or ""),
                    "posted_label": str(row.get("date_posted", "N/A") or "N/A"),
                    "source":       source_label,
                    "salary":       str(row.get("min_amount",  "") or ""),
                    "job_type":     str(row.get("job_type",    "") or ""),
                })
            except Exception as e:
                print(f"   ⚠️  {source_label} row parse error: {e}")
                continue

        print(f"   ✅ {source_label}: {len(jobs)} jobs found")
        return jobs

    except ImportError:
        print("   ❌ jobspy not installed — run: pip install python-jobspy --no-deps")
        return []
    except Exception as e:
        print(f"   ❌ {source_label} jobspy scrape failed: {type(e).__name__}: {e}")
        return []


def scrape_indeed(job_title: str, location: str, num_jobs: int) -> list:
    return _scrape_via_jobspy(job_title, location, num_jobs, "indeed", "Indeed")


def scrape_glassdoor(job_title: str, location: str, num_jobs: int) -> list:
    return _scrape_via_jobspy(job_title, location, num_jobs, "glassdoor", "Glassdoor")


def scrape_builtin(job_title: str, location: str, num_jobs: int) -> list:
    return _scrape_via_jobspy(job_title, location, num_jobs, "google", "Builtin")


# ─────────────────────────────────────────────────────────────
# JOBS.IE — custom BeautifulSoup scraper (Irish job board)
# ─────────────────────────────────────────────────────────────

def scrape_jobsie(job_title: str, location: str, num_jobs: int) -> list:
    print(f"   🟢 Scraping Jobs.ie for '{job_title}' in '{location}'...")

    query = job_title.replace(" ", "+")
    loc   = location.split(",")[0].strip().replace(" ", "+")
    url   = f"https://www.jobs.ie/jobs/?q={query}&l={loc}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        cards = (
            soup.find_all("article", class_="job") or
            soup.find_all("div", class_="job-result") or
            soup.find_all("li", class_="jobs-item")
        )
        cards = cards[:num_jobs]

        if not cards:
            # Fallback: grab any job links on the page
            anchors = soup.select("a[href*='/jobs/view']")[:num_jobs]
            jobs = []
            for a in anchors:
                href  = a["href"]
                link  = "https://www.jobs.ie" + href if href.startswith("/") else href
                title = a.get_text(strip=True)
                jobs.append({
                    "title":        title,
                    "company":      "See listing",
                    "location":     location,
                    "url":          link,
                    "description":  _fetch_jobsie_description(link),
                    "posted":       "N/A",
                    "posted_label": "N/A",
                    "source":       "Jobs.ie",
                })
            print(f"   ✅ Jobs.ie (fallback): {len(jobs)} jobs found")
            return jobs

        jobs = []
        for card in cards:
            try:
                title_el   = (
                    card.find("h2") or
                    card.find("h3") or
                    card.find(class_="job-title") or
                    card.find("a")
                )
                company_el = (
                    card.find(class_="company") or
                    card.find(class_="recruiter-name") or
                    card.find("span", class_="company-name")
                )
                link_el    = card.find("a", href=True)
                time_el    = card.find("time") or card.find(class_="date")

                title   = title_el.get_text(strip=True)   if title_el   else "N/A"
                company = company_el.get_text(strip=True)  if company_el else "N/A"
                href    = link_el["href"]                  if link_el    else ""
                link    = "https://www.jobs.ie" + href if href.startswith("/") else href
                posted  = time_el.get_text(strip=True)     if time_el    else "N/A"

                description = _fetch_jobsie_description(link)

                jobs.append({
                    "title":        title,
                    "company":      company,
                    "location":     location,
                    "url":          link,
                    "description":  description,
                    "posted":       posted,
                    "posted_label": posted,
                    "source":       "Jobs.ie",
                })
            except Exception as e:
                print(f"   ⚠️  Jobs.ie card parse error: {e}")
                continue

        print(f"   ✅ Jobs.ie: {len(jobs)} jobs found")
        return jobs

    except Exception as e:
        print(f"   ❌ Jobs.ie scrape failed: {type(e).__name__}: {e}")
        return []


def _fetch_jobsie_description(url: str) -> str:
    if not url or url == "https://www.jobs.ie":
        return ""
    try:
        time.sleep(0.8)
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        desc = (
    soup.find("div", class_="job-description") or
    soup.find("div", class_="description") or
    soup.find("section", class_="job-details") or
    soup.find("div", id="job-description")
           
        )
        return desc.get_text(separator=" ", strip=True) if desc else ""
    except Exception as e:
        print(f"   ⚠️  Jobs.ie description fetch failed: {e}")
        return ""


# ─────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────

SOURCE_MAP = {
    "LinkedIn":  scrape_linkedin,
    "Indeed":    scrape_indeed,
    "Glassdoor": scrape_glassdoor,
    "Jobs.ie":   scrape_jobsie,
    "Builtin":   scrape_builtin,
}

# Stable order for automatic fallback when the user's board returns no listings.
SOURCE_BOARD_ORDER = list(SOURCE_MAP.keys())


def boards_fallback_sequence(user_pick: str) -> list:
    """
    Boards to try in order: user's choice first, then every other board in
    SOURCE_MAP order. Used when the primary board returns zero jobs.

    If the user selected "All", returns only ``["All"]`` (combined search).
    Unknown names default to LinkedIn as primary.
    """
    s = (user_pick or "").strip()
    if s == "All":
        return ["All"]
    if s not in SOURCE_MAP:
        s = "LinkedIn"
    return [s] + [b for b in SOURCE_BOARD_ORDER if b != s]


def scrape_jobs(
    job_title: str,
    location:  str,
    num_jobs:  int = 5,
    source:    str = "LinkedIn",
) -> list:
    source = source.strip()

    if source == "All":
        per_board = max(1, num_jobs // len(SOURCE_MAP))
        jobs = []
        for label, fn in SOURCE_MAP.items():
            jobs += fn(job_title, location, per_board)
        print(f"   📊 All sources combined: {len(jobs)} jobs total")
        return jobs

    scraper_fn = SOURCE_MAP.get(source)
    if not scraper_fn:
        print(f"   ⚠️  Unknown source '{source}' — defaulting to LinkedIn")
        return scrape_linkedin(job_title, location, num_jobs)

    return scraper_fn(job_title, location, num_jobs)