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
from urllib.parse import unquote, urlencode, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

GUEST_SEARCH_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)

GUEST_JOB_DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

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


GUEST_JOB_VIEW_URL = "https://www.linkedin.com/jobs-guest/jobs/view/{job_id}"

# LinkedIn historically exposed the offsite apply URL to guests in a
# <code id="applyUrl"><!--"https://www.linkedin.com/.../externalApply/...?url=<encoded>"--></code>
# element. As of mid-2026 it is usually omitted, but we still parse it when
# present since it is the only source of the actual company-site link.
_APPLY_URL_RE = re.compile(r'\?url=([^"&]+)')
_ENCODED_URL_RE = re.compile(
    r'https?%3A%2F%2F[^"&\s]+|https?://[^\s"\'<>]+',
    flags=re.IGNORECASE,
)

# Markers that reliably distinguish offsite-apply postings from Easy Apply
# ones on the guest job-detail fragment.
_OFFSITE_MARKERS = (
    "apply-button__offsite-apply-icon",
    "public_jobs_apply-link-offsite",
)

# Common ATS / careers-page hosts seen in offsite apply redirects.
_ATS_HOST_MARKERS = (
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "ashbyhq.com",
    "jobvite.com",
    "icims.com",
    "smartrecruiters.com",
    "taleo.net",
    "bamboohr.com",
    "careers.",
    "/careers/",
    "/jobs/",
)


def _is_external_apply_url(url: str) -> bool:
    """True when a URL looks like a company-site apply link, not LinkedIn."""
    if not url or "linkedin.com" in url.lower():
        return False
    lower = url.lower()
    return any(marker in lower for marker in _ATS_HOST_MARKERS)


def _extract_apply_url(html: str) -> str | None:
    """Best-effort extraction of a company-site apply URL from job HTML."""
    soup = BeautifulSoup(html, "lxml")
    code_el = soup.find("code", id="applyUrl")
    if code_el:
        m = _APPLY_URL_RE.search(code_el.decode_contents())
        if m:
            candidate = unquote(m.group(1))
            if _is_external_apply_url(candidate):
                return candidate

    # Fallback: scan for encoded redirect targets or bare external URLs.
    for m in _ENCODED_URL_RE.finditer(html):
        candidate = unquote(m.group(0))
        if _is_external_apply_url(candidate):
            return candidate
    return None


def _classify_apply(html: str, apply_url: str | None) -> str:
    if apply_url or any(marker in html for marker in _OFFSITE_MARKERS):
        return "direct"
    if "top-card-layout" in html:
        return "easy_apply"
    return "unknown"


def _fetch_html(url: str, job_id: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        log.warning("linkedin request failed for %s (%s): %s", job_id, url, e)
        return None
    finally:
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    if resp.status_code >= 400:
        log.warning("linkedin HTTP %s for %s (%s)", resp.status_code, job_id, url)
        return None
    return resp.text


def fetch_apply_info(job_id: str) -> dict:
    """Fetch the guest job-detail fragment and classify how one applies.

    Returns a dict with:
        apply_type: "direct"     -- offsite apply on the company's site
                    "easy_apply" -- LinkedIn-hosted quick apply only
                    "unknown"    -- fetch failed / page unrecognizable
        apply_url:  the direct company-site apply URL when LinkedIn exposes
                    it (rare for guests), else None.
    """
    html = _fetch_html(GUEST_JOB_DETAIL_URL.format(job_id=job_id), job_id)
    if html is None:
        return {"apply_type": "unknown", "apply_url": None}

    apply_url = _extract_apply_url(html)
    apply_type = _classify_apply(html, apply_url)

    # The full guest view page occasionally exposes an apply URL that the API
    # fragment omits. Only fetch it for likely-direct postings still missing a
    # URL, since it is a much larger response.
    if apply_type == "direct" and not apply_url:
        view_html = _fetch_html(GUEST_JOB_VIEW_URL.format(job_id=job_id), job_id)
        if view_html:
            apply_url = _extract_apply_url(view_html)
            apply_type = _classify_apply(view_html, apply_url)

    if apply_type == "unknown":
        log.info("linkedin job-detail page unrecognizable for %s (%d bytes)",
                 job_id, len(html))

    return {"apply_type": apply_type, "apply_url": apply_url}
