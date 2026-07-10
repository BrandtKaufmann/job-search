"""Entrypoint: scrape boards, dedupe against seen-state, email new postings."""

from __future__ import annotations

import logging
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlparse

from . import indeed, linkedin, notify, state

# Each search pairs a LinkedIn keyword query with a title-filter policy.
# When title_filter is True, results are kept only if the title matches
# TITLE_ALLOWLIST -- LinkedIn keyword search also matches the description, so
# tight roles get noisy without this. Computer Vision is intentionally left
# broad so ambiguously-titled roles (Software/Perception/Research Engineer,
# etc.) that mention CV still surface.
SEARCHES = [
    {"keywords": "Computer Vision", "title_filter": False},
    {"keywords": "Machine Learning Engineer", "title_filter": True},
    {"keywords": "Deep Learning Engineer", "title_filter": True},
]

# Applied only to searches with title_filter=True.
TITLE_ALLOWLIST = re.compile(
    r"\b(machine learning|deep learning|ml engineer|ml)\b",
    flags=re.IGNORECASE,
)

LOCATIONS = [
    "San Francisco Bay Area",
    "Greater Seattle Area",
    "Washington DC-Baltimore Area",
    "Remote",
    # "Los Angeles Metropolitan Area",  # uncomment to also search LA
]

BOARDS = [
    ("linkedin", linkedin.search),
    ("indeed", indeed.search),
]

# Title substrings that indicate a role is too senior for a master's student.
# Matched case-insensitively with word boundaries so "Senior" doesn't clobber
# plain "Engineer" and "Lead" doesn't match "Leadership" etc.
EXCLUDED_SENIORITY_TERMS = [
    r"senior",
    r"sr\.?",
    r"staff",
    r"principal",
    r"lead",
    r"manager",
    r"director",
    r"head of",
    r"vp",
    r"vice president",
    r"chief",
    r"distinguished",
    r"fellow",
    # Roman-numeral levels III+ (II is often mid-level and sometimes fine).
    r"iii",
    r"iv",
]

_EXCLUSION_RE = re.compile(
    r"\b(?:" + "|".join(EXCLUDED_SENIORITY_TERMS) + r")\b",
    flags=re.IGNORECASE,
)


def is_too_senior(title: str) -> bool:
    return bool(_EXCLUSION_RE.search(title or ""))


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def collect() -> list[dict]:
    """Query every (board, search, location) combo and return a deduped list."""
    seen_ids: set[str] = set()
    all_jobs: list[dict] = []
    for board_name, search_fn in BOARDS:
        for search in SEARCHES:
            keywords = search["keywords"]
            title_filter = search["title_filter"]
            for location in LOCATIONS:
                try:
                    jobs = search_fn(keywords, location)
                except Exception as e:  # noqa: BLE001 -- never let one board kill the run
                    logging.exception("%s search failed for %r/%r: %s",
                                      board_name, keywords, location, e)
                    continue
                for job in jobs:
                    if job["id"] in seen_ids:
                        continue
                    if title_filter and not TITLE_ALLOWLIST.search(job.get("title", "")):
                        continue
                    seen_ids.add(job["id"])
                    all_jobs.append(job)
    return all_jobs


def filter_new(jobs: list[dict], seen: dict[str, str]) -> list[dict]:
    return [j for j in jobs if j["id"] not in seen]


# Query params that carry tracking noise rather than job identity; dropped
# when normalizing an apply URL into a role key.
_TRACKING_PARAMS = ("utm_", "trk", "ref", "src", "source", "gclid", "fbclid")

# Strip legal suffixes and punctuation so "Meta Platforms" and "Meta" match.
_COMPANY_SUFFIX_RE = re.compile(
    r"\b(?:incorporated|inc|llc|l\.l\.c\.|corp|corporation|ltd|limited|co|company|"
    r"technologies|technology|platforms|group|holdings)\b\.?",
    flags=re.IGNORECASE,
)

# Location / level noise at the end of titles ("- Burlingame, CA", "(Remote)").
_TITLE_SUFFIX_PATTERNS = (
    r"\s*[-–—]\s*[a-z][\w\s,.-]+,\s*[a-z]{2}\s*$",  # - Burlingame, CA
    r"\s*[-–—]\s*[a-z][\w\s.-]+\s+[a-z]{2}\s*$",      # - Mountain View CA
    r"\s*[(]\s*remote\s*[)]\s*$",
    r"\s*[(]\s*hybrid\s*[)]\s*$",
    r"\s*[-–—]\s*remote\s*$",
    r"\s*[-–—]\s*hybrid\s*$",
)


def _normalize_company(name: str) -> str:
    cleaned = re.sub(r"[^\w\s&]", " ", name or "")
    cleaned = _COMPANY_SUFFIX_RE.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def _normalize_title(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", (title or "")).strip().lower()
    for pattern in _TITLE_SUFFIX_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def _normalize_apply_url(url: str) -> str:
    parsed = urlparse(url)
    query = [
        (k, v) for k, v in parse_qsl(parsed.query)
        if not any(k.lower().startswith(p) for p in _TRACKING_PARAMS)
    ]
    normalized = parsed.netloc.lower() + parsed.path.rstrip("/")
    if query:
        normalized += "?" + urlencode(sorted(query))
    return normalized


def role_key(job: dict) -> str | None:
    """A cross-posting identity for a role, used to collapse duplicates.

    Prefer the direct (company-site) apply URL when LinkedIn exposes it --
    reposts and multi-location postings of one opening share it. Otherwise
    fall back to company + title, which catches e.g. Meta listing the same
    "Computer Vision Engineer" opening under four LinkedIn ids for four
    metros.
    """
    apply_url = job.get("apply_url")
    if apply_url:
        return f"role:url:{_normalize_apply_url(apply_url)}"
    company = _normalize_company(job.get("company", ""))
    title = _normalize_title(job.get("title", ""))
    if company and title:
        return f"role:{company}|{title}"
    return None


def enrich_linkedin_jobs(jobs: list[dict]) -> None:
    """Attach apply_type / apply_url to LinkedIn jobs (one fetch per job)."""
    for job in jobs:
        if job.get("source") != "linkedin":
            job.setdefault("apply_type", "unknown")
            continue
        job_id = job["id"].split(":", 1)[1]
        info = linkedin.fetch_apply_info(job_id)
        job["apply_type"] = info["apply_type"]
        if info["apply_url"]:
            job["apply_url"] = info["apply_url"]


def collapse_role_duplicates(jobs: list[dict], seen: dict[str, str]) -> list[dict]:
    """Drop jobs whose role was already reported; merge same-role jobs.

    Postings that share a role key within this run are merged into the first
    one, with their distinct locations combined so the email still shows all
    metros the role is offered in.
    """
    kept: list[dict] = []
    by_role: dict[str, dict] = {}
    for job in jobs:
        key = role_key(job)
        job["role_key"] = key
        if key is None:
            kept.append(job)
            continue
        if key in seen:
            logging.info("skipping %s (%s @ %s): role already reported",
                         job["id"], job.get("title"), job.get("company"))
            continue
        primary = by_role.get(key)
        if primary is None:
            by_role[key] = job
            kept.append(job)
        else:
            loc = job.get("location", "")
            locs = primary.setdefault("locations", [primary.get("location", "")])
            if loc and loc not in locs:
                locs.append(loc)
    return kept


def main() -> int:
    configure_logging()
    seen = state.prune(state.load_state())
    found = collect()
    logging.info("collected %d unique postings across boards", len(found))

    before_seniority = len(found)
    found = [j for j in found if not is_too_senior(j.get("title", ""))]
    logging.info(
        "filtered out %d postings by seniority; %d remain",
        before_seniority - len(found), len(found),
    )

    new_by_id = filter_new(found, seen)
    logging.info("%d of those are new since last run", len(new_by_id))

    enrich_linkedin_jobs(new_by_id)
    new_jobs = collapse_role_duplicates(new_by_id, seen)
    logging.info("%d remain after collapsing duplicate roles", len(new_jobs))

    if new_jobs:
        try:
            notify.send_digest(new_jobs)
        except Exception as e:  # noqa: BLE001
            logging.exception("email send failed; will not record ids as seen: %s", e)
            # Persist unchanged so we retry next run.
            state.save_state(seen)
            return 1

    now = state.now_iso()
    # Record every new posting id (including ones collapsed away as duplicates,
    # so we never re-fetch their detail pages) plus every role key we
    # processed, so reposts and multi-location copies stay suppressed.
    for job in new_by_id:
        seen[job["id"]] = now
        if job.get("role_key"):
            seen[job["role_key"]] = now
    state.save_state(seen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
