# job-search

Scrapes LinkedIn and Indeed twice a day via GitHub Actions for new
postings matching target roles, and emails a digest via Gmail SMTP.

## What it looks for

- **Searches** (each queried separately against each board):
  - **Computer Vision** — broad; LinkedIn matches the keyword in the title
    *or description*, so ambiguously-titled roles (Software / Perception /
    Research Engineer, etc.) that mention CV surface for manual triage.
  - **Machine Learning Engineer** and **Deep Learning Engineer** — tight;
    results are kept only if the *title* matches `TITLE_ALLOWLIST`
    (`machine learning`, `deep learning`, `ml engineer`, `ml`), since keyword
    search alone is noisy.
- **Locations**: San Francisco Bay Area, San Diego Metropolitan Area,
  and US-Remote (three passes)
- **Recency**: postings from the last 24 hours
- **Seniority filter**: excludes titles containing `senior`, `sr`, `staff`,
  `principal`, `lead`, `manager`, `director`, `head of`, `vp`, `chief`,
  `distinguished`, `fellow`, or roman-numeral levels `III`/`IV` — roles
  that typically require years of experience.

Edit `SEARCHES`, `TITLE_ALLOWLIST`, `LOCATIONS`, and
`EXCLUDED_SENIORITY_TERMS` at the top of [`scraper/main.py`](scraper/main.py)
to change what's searched and what's filtered.

## How it works

```
GitHub Actions cron (2x/day)
  -> scraper.main
       -> scraper.linkedin.search(...)   # per title, per location
       -> scraper.indeed.search(...)
       -> dedupe against seen_jobs.json
       -> if any new: notify.send_digest(...)  (Gmail SMTP)
  -> commit updated seen_jobs.json back to the repo
```

State is persisted by committing `seen_jobs.json` back to the default branch,
so the next run's fresh VM knows what it has already reported.

## One-time setup

### 1. Create the repo on GitHub and push this code

```bash
git init -b main
git add .
git commit -m "Initial commit: job scraper"
# Create an empty private repo on github.com first, then:
git remote add origin https://github.com/<you>/job-search.git
git push -u origin main
```

Or with [`gh`](https://cli.github.com/):

```bash
gh repo create job-search --private --source=. --push
```

### 2. Generate a Gmail App Password

The GitHub Secret cannot be your regular Gmail password -- Google blocks
SMTP logins with account passwords. You need an **App Password**, which
requires 2-Step Verification on the account.

1. Enable 2-Step Verification at <https://myaccount.google.com/security>.
2. Go to <https://myaccount.google.com/apppasswords>.
3. Create a new password named e.g. `job-search-actions`.
4. Copy the 16-character password shown (you can paste it with or without
   spaces; Gmail accepts both).

### 3. Add the secrets to the repo

In your repo on GitHub: **Settings -> Secrets and variables -> Actions
-> New repository secret**. Add:

| Secret | Value |
| --- | --- |
| `GMAIL_USER` | your Gmail address |
| `GMAIL_APP_PASSWORD` | the 16-character App Password from step 2 |
| `NOTIFY_TO` | (optional) where to send alerts; defaults to `GMAIL_USER` |

### 4. Allow the workflow to push commits

The workflow commits `seen_jobs.json` back to the repo, so it needs write
access to contents.

**Settings -> Actions -> General -> Workflow permissions ->
Read and write permissions -> Save.**

The `permissions: contents: write` at the top of the workflow is necessary
but not sufficient; the repo-level setting is a ceiling.

### 5. Trigger a first run

**Actions tab -> job-scraper -> Run workflow -> Run workflow.**

Expect the first run's email to contain many jobs (everything visible is
"new"). Subsequent runs should only email deltas.

## Schedule

Runs twice daily at **9:15 AM** and **4:30 PM Pacific**, using the native
cron `timezone` field (`America/Los_Angeles`), so the times stay correct
across daylight saving automatically. GitHub cron is best-effort and can be
delayed 5-15 minutes under load.

To change the cadence, edit the `cron`/`timezone` entries in
[`.github/workflows/job-scraper.yml`](.github/workflows/job-scraper.yml).

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GMAIL_USER=you@gmail.com
export GMAIL_APP_PASSWORD='xxxx xxxx xxxx xxxx'
export NOTIFY_TO=you@gmail.com
python -m scraper.main
```

The scraper will read and write `seen_jobs.json` in the repo root; delete
it to force a "first run" where everything is treated as new.

## Known limitations

- **LinkedIn** rate-limits by IP. Expect occasional empty-result runs or
  HTTP 429/999 from GitHub Actions runner IPs. The scraper logs and
  tolerates these.
- **Indeed** aggressively Cloudflare-challenges datacenter IPs. Many runs
  from GitHub Actions will return zero results. Treat it as a bonus; the
  primary signal is LinkedIn.
- **CSS selectors** on both sites change occasionally. If runs start
  consistently returning zero jobs, check the action logs for
  `"0 cards parsed"` messages and update the selectors in the respective
  module.
- **First run** emails a large digest. Pre-seed `seen_jobs.json` with a
  previous snapshot if you want to avoid this.

## Secrets and permissions model

- Secrets are encrypted at rest by GitHub and scrubbed from log output;
  workflows triggered by forks of public repos do not receive them.
- The workflow's `GITHUB_TOKEN` is ephemeral per-run and scoped by the
  `permissions:` block. We request only `contents: write`.
- Commits authored by the bot use `job-scraper-bot@users.noreply.github.com`
  so they do not count as contributions for any real user and do not link
  to a profile.

## Pinned versions

- Runner: `ubuntu-24.04`
- Actions: `actions/checkout@v4.2.2`, `actions/setup-python@v5.3.0`
- Python: 3.11
- pip: 24.3.1
- Packages: see [`requirements.txt`](requirements.txt)
