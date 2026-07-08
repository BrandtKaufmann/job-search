"""LinkedIn guest job search scraper.

Uses the public, unauthenticated endpoint that LinkedIn exposes for its guest
search UI. It returns HTML fragments of job cards (no login required):

    https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search

This is fragile -- LinkedIn rate limits by IP (HTTP 429 / 999) and occasionally
changes CSS classes. The scraper is written defensively: missing fields are
logged and skipped rather than raising.
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlencode, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

GUEST_SEARCH_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)

# A current Chrome on macOS UA. Python's default `python-requests/x.y` is
# filtered aggressively, so we present as a real browser.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# LinkedIn geoId codes for their guest search. Found by loading the guest
# search UI with a location picked from the autocomplete and copying geoId
# from the resulting URL.
LOCATION_CODES = {
    "United States": "103644278",
    "San Francisco Bay Area": "90000084",
    "San Francisco": "102277331",
    "Greater Seattle Area": "90000091",
    "Washington DC-Baltimore Area": "90000097",
    "Los Angeles Metropolitan Area": "90000049",
    "Remote": None,  # handled via f_WT=2 work-type filter, not geoId
}

# For "Remote" passes we still need *some* geo filter or LinkedIn returns a
# global mix. Remote-in-the-US is the right default for this project.
REMOTE_GEO_ID = LOCATION_CODES["United States"]
REMOTE_GEO_LABEL = "United States"

REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_REQUESTS = 2.0


def _build_url(keywords: str, location: str, start: int = 0) -> str:
    params: dict[str, str] = {
        "keywords": keywords,
        "f_TPR": "r86400",  # last 24 hours
        "start": str(start),
        "sortBy": "DD",  # date descending
    }
    if location == "Remote":
        params["f_WT"] = "2"  # remote work type
        params["location"] = REMOTE_GEO_LABEL
        params["geoId"] = REMOTE_GEO_ID
    else:
        params["location"] = location
        geo = LOCATION_CODES.get(location)
        if geo:
            params["geoId"] = geo
    return f"{GUEST_SEARCH_URL}?{urlencode(params)}"


def _extract_job_id(card) -> str | None:
    # LinkedIn encodes the job id a few different ways; try each.
    for attr in ("data-entity-urn", "data-id"):
        val = card.get(attr)
        if val:
            m = re.search(r"(\d{6,})", val)
            if m:
                return m.group(1)
    link = card.find("a", class_=re.compile(r"base-card__full-link"))
    if link and link.get("href"):
        m = re.search(r"/jobs/view/[^/]*-(\d{6,})", link["href"])
        if m:
            return m.group(1)
        m = re.search(r"currentJobId=(\d{6,})", link["href"])
        if m:
            return m.group(1)
    return None


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s).strip() if s else ""


def _parse_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results: list[dict] = []
    # Job cards show up either as <li> wrappers or as <div class="base-card"> directly.
    cards = soup.select("li div.base-card, div.base-card, li.jobs-search__results-list li")
    if not cards:
        cards = soup.select("li")
    for card in cards:
        job_id = _extract_job_id(card)
        if not job_id:
            continue
        title_el = card.find(class_=re.compile(r"base-search-card__title"))
        company_el = card.find(class_=re.compile(r"base-search-card__subtitle"))
        location_el = card.find(class_=re.compile(r"job-search-card__location"))
        link_el = card.find("a", class_=re.compile(r"base-card__full-link"))

        url = (link_el.get("href") if link_el else None) or (
            f"https://www.linkedin.com/jobs/view/{job_id}"
        )
        # Strip tracking query params for stability / prettier emails.
        parsed = urlparse(url)
        url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

        results.append(
            {
                "id": f"linkedin:{job_id}",
                "source": "linkedin",
                "title": _clean(title_el.get_text() if title_el else ""),
                "company": _clean(company_el.get_text() if company_el else ""),
                "location": _clean(location_el.get_text() if location_el else ""),
                "url": url,
            }
        )
    return results


def search(keywords: str, location: str, max_results: int = 25) -> list[dict]:
    """Return a list of job dicts for the given keyword + location.

    Returns an empty list (and logs a warning) on any HTTP or parse error, so a
    single flaky query doesn't bring the whole run down.
    """
    url = _build_url(keywords, location)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        log.warning("linkedin request failed for %r/%r: %s", keywords, location, e)
        return []

    if resp.status_code == 429 or resp.status_code == 999:
        log.warning(
            "linkedin rate-limited (%s) for %r/%r", resp.status_code, keywords, location
        )
        return []
    if resp.status_code >= 400:
        log.warning(
            "linkedin returned HTTP %s for %r/%r", resp.status_code, keywords, location
        )
        return []

    jobs = _parse_cards(resp.text)
    if not jobs:
        # Could be "no results" or a soft block. Log either way for visibility.
        log.info("linkedin: 0 cards parsed for %r/%r (HTTP %s, %d bytes)",
                 keywords, location, resp.status_code, len(resp.text))
    else:
        log.info("linkedin: %d jobs for %r/%r", len(jobs), keywords, location)

    time.sleep(SLEEP_BETWEEN_REQUESTS)
    return jobs[:max_results]
