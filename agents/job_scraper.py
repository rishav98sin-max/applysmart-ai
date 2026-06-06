# agents/job_scraper.py

import re
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

        _scrape_kwargs = dict(
            site_name                  = [site],
            search_term                = job_title,
            location                   = location,
            results_wanted             = fetch_count,
            country_indeed             = country_indeed,
            linkedin_fetch_description = False,
        )
        # python-jobspy drifts its kwargs between releases (Run 23: hours_old
        # vanished; Run 31: linkedin_fetch_description vanished from Indeed).
        # Each drift killed an entire board. Generalised resilience: keep
        # peeling off the offending kwarg until the call accepts what's left
        # OR we run out of kwargs to strip. Two strips max to avoid loops.
        _extra = {"hours_old": 168}
        for _strip_attempt in range(3):
            try:
                df = jobspy_scrape(**_extra, **_scrape_kwargs)
                break
            except TypeError as e:
                msg = str(e)
                # Try the always-tried kwarg first (back-compat with the
                # original "drop hours_old, retry" pattern).
                if "hours_old" in msg and "hours_old" in _extra:
                    print("   ℹ️  jobspy: 'hours_old' unsupported in this version — scraping without the recency filter")
                    _extra.pop("hours_old", None)
                    continue
                # Generic: parse "got an unexpected keyword argument 'NAME'"
                # and strip NAME from _scrape_kwargs. This catches whichever
                # board kwarg jobspy decides to rename next.
                import re as _re
                m = _re.search(r"unexpected keyword argument '([^']+)'", msg)
                if m and m.group(1) in _scrape_kwargs:
                    bad = m.group(1)
                    print(f"   ℹ️  jobspy: '{bad}' unsupported in this version — retrying without it")
                    _scrape_kwargs.pop(bad, None)
                    continue
                raise
        else:
            print(f"   ⚠️  jobspy: still TypeError after kwarg-strip retries — bailing")
            return []

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
    """Real builtin.com scraper (replaces the old jobspy 'google' proxy,
    which returned 0). builtin.com is a tech-jobs board with global +
    remote roles, including Ireland (cards tagged e.g. 'Remote or Hybrid
    Dublin, IRL'). Card structure (2026):
        data-id="job-card"          → each listing
          data-id="job-card-title"  → <a> title, href /job/...
          data-id="company-title"   → company name
    Location + description are taken from the card text snippet, which is
    rich enough for match scoring (no per-job detail fetch needed)."""
    print(f"   🟠 Scraping Builtin for '{job_title}' in '{location}'...")
    query = "+".join(w for w in job_title.strip().split() if w)
    url = f"https://builtin.com/jobs?search={query}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.find_all(attrs={"data-id": "job-card"})[:num_jobs]
        if not cards:
            print("   ⚠️  Builtin: 0 job-card elements")
            return []
        jobs = []
        for card in cards:
            try:
                title_a = card.find(attrs={"data-id": "job-card-title"})
                if not title_a:
                    continue
                title = title_a.get_text(strip=True) or "N/A"
                href  = title_a.get("href", "") or ""
                link  = ("https://builtin.com" + href) if href.startswith("/") else href

                comp_el = card.find(attrs={"data-id": "company-title"})
                company = comp_el.get_text(strip=True) if comp_el else "N/A"

                card_text = card.get_text(" ", strip=True)
                # Location: builtin prints the work-mode then the place then
                # the seniority, e.g. "Remote or Hybrid Dublin, IRL Senior
                # level". Pull the "<City>, <CC>" that sits before "... level",
                # rejecting card furniture ("Saved", "Ago", "Remote"...).
                job_loc = location
                for m in re.finditer(r"([A-Z][A-Za-z.\-]+(?:\s[A-Z][A-Za-z.\-]+)?,\s*[A-Z]{2,3})", card_text):
                    cand = m.group(1)
                    if not re.search(r"\b(Saved|Ago|Remote|Hybrid|Office|Manager|Reposted|Days?)\b", cand):
                        job_loc = cand
                        break

                # Snippet = card text minus the title/company furniture.
                snippet = card_text.replace(title, "", 1).replace(company, "", 1).strip()

                jobs.append({
                    "title":        title,
                    "company":      company,
                    "location":     job_loc,
                    "url":          link,
                    "description":  snippet,
                    "posted":       "N/A",
                    "posted_label": "N/A",
                    "source":       "Builtin",
                })
            except Exception as e:
                print(f"   ⚠️  Builtin card parse error: {e}")
                continue
        print(f"   ✅ Builtin: {len(jobs)} jobs found")
        return jobs
    except Exception as e:
        print(f"   ❌ Builtin scrape failed: {type(e).__name__}: {e}")
        return []


# ─────────────────────────────────────────────────────────────
# JOBS.IE — custom BeautifulSoup scraper (Irish job board)
# ─────────────────────────────────────────────────────────────

def scrape_jobsie(job_title: str, location: str, num_jobs: int) -> list:
    print(f"   🟢 Scraping Jobs.ie for '{job_title}' in '{location}'...")

    # 2026 site rewrite: the old ?q=&l= search endpoint returns an empty
    # shell (no listings). The live results live at an SEO path
    # /{Title-With-Dashes}-jobs which 302s to /jobs/{title-slug}. Cards are
    # now `data-testid="job-item"` with the title in an inner
    # `data-testid="job-item-title"` <a> linking to /job/...
    slug = "-".join(w for w in job_title.strip().split() if w)
    url  = f"https://www.jobs.ie/{slug}-jobs"

    try:
        # Run 31 (Jun 2026): bumped Jobs.ie listing-page timeout 15→30s.
        # The site was timing out on 3/3 attempts at 15s during the Cormac
        # IS Auditor run; cold-cache responses for Dublin-region queries
        # routinely take 18–25s on the cards endpoint. 30s gives headroom
        # without hanging the run; the board-cooldown logic in job_agent
        # will park Jobs.ie after 2 consecutive failures anyway.
        resp = requests.get(url, headers=HEADERS, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")

        cards = soup.find_all(attrs={"data-testid": "job-item"})[:num_jobs]
        if not cards:
            print("   ⚠️  Jobs.ie: 0 job-item cards (selectors may have changed again)")
            return []

        jobs = []
        for card in cards:
            try:
                title_a = card.find(attrs={"data-testid": "job-item-title"})
                if not title_a:
                    continue
                title = title_a.get_text(strip=True) or "N/A"
                href  = title_a.get("href", "") or ""
                link  = ("https://www.jobs.ie" + href) if href.startswith("/") else href

                # The card text carries: Title · Company · Location · Salary ·
                # description snippet — enough for the matcher. We use the
                # snippet directly (detail pages rate-limit / time out), then
                # best-effort enrich with the full JD on a short timeout.
                card_text = card.get_text(" ", strip=True)
                snippet   = card_text.replace(title, "", 1).strip()

                # Company from the URL slug is the most reliable signal:
                # /job/<title-slug>/<company-slug>-jobNNNN
                company = "See listing"
                parts = [p for p in href.split("/") if p]
                if len(parts) >= 3:
                    comp_slug = re.sub(r"-job\d+$", "", parts[2])
                    company = comp_slug.replace("-", " ").title() or company

                full_desc = _fetch_jobsie_description(link)
                description = full_desc if len(full_desc) > len(snippet) else snippet

                jobs.append({
                    "title":        title,
                    "company":      company,
                    "location":     location,
                    "url":          link,
                    "description":  description or snippet,
                    "posted":       "N/A",
                    "posted_label": "N/A",
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
    """Best-effort full-JD fetch from a Jobs.ie detail page. Detail pages
    rate-limit / time out, so this uses a SHORT timeout and returns "" on
    any failure — the caller falls back to the listing-card snippet, which
    is enough for match scoring. Never blocks the scrape."""
    if not url or url == "https://www.jobs.ie":
        return ""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        soup = BeautifulSoup(resp.text, "html.parser")
        desc = (
            soup.find(attrs={"data-testid": "job-description"}) or
            soup.find(attrs={"data-testid": "job-details"}) or
            soup.find("div", class_="job-description") or
            soup.find("div", id="job-description") or
            soup.find("section", class_="job-details")
        )
        return desc.get_text(separator=" ", strip=True) if desc else ""
    except Exception:
        # Silent — detail enrichment is optional; snippet covers matching.
        return ""


# ─────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────

SOURCE_MAP = {
    "LinkedIn":  scrape_linkedin,
    "Indeed":    scrape_indeed,
    "Glassdoor": scrape_glassdoor,   # retained but DEAD (see _DEAD_BOARDS)
    "Jobs.ie":   scrape_jobsie,
    "Builtin":   scrape_builtin,
}

# Boards excluded from the live fallback/escalation order and the UI.
# Glassdoor serves a Cloudflare/captcha challenge page to requests-based
# scrapers (verified Jun 2026: HTTP 200 but a challenge body) and jobspy
# 1.1.82 (latest) errors on its API. Re-enabling needs a headless browser.
# The scraper fn stays in SOURCE_MAP so an explicit call still degrades
# gracefully (returns []), but it never enters automatic rotation.
_DEAD_BOARDS = {"Glassdoor"}

# Live boards = everything in SOURCE_MAP except the known-dead ones.
LIVE_BOARDS = [b for b in SOURCE_MAP if b not in _DEAD_BOARDS]

# Stable order for automatic fallback when the user's board returns no listings.
SOURCE_BOARD_ORDER = list(LIVE_BOARDS)


def live_boards_for(user_pick: str) -> list:
    """Live boards with the user's pick first (for board ESCALATION). Dead
    boards excluded. Unknown / 'All' → just the live order."""
    s = (user_pick or "").strip()
    if s not in LIVE_BOARDS:
        return list(LIVE_BOARDS)
    return [s] + [b for b in LIVE_BOARDS if b != s]


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