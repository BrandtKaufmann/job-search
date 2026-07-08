"""Entrypoint: scrape boards, dedupe against seen-state, email new postings."""

from __future__ import annotations

import logging
import re
import sys

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

LOCATIONS = ["San Francisco Bay Area", "San Diego Metropolitan Area", "Remote"]

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

    new_jobs = filter_new(found, seen)
    logging.info("%d of those are new since last run", len(new_jobs))

    if new_jobs:
        try:
            notify.send_digest(new_jobs)
        except Exception as e:  # noqa: BLE001
            logging.exception("email send failed; will not record ids as seen: %s", e)
            # Persist unchanged so we retry next run.
            state.save_state(seen)
            return 1

    now = state.now_iso()
    for job in new_jobs:
        seen[job["id"]] = now
    state.save_state(seen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
