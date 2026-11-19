# Implementation Plan: Docker/GitHub Submission Viewer (Faculty Web App)

Course context: 20CYS402 – Distributed Systems and Cloud Computing (Batch 23CYS).
Input file in this folder: `docker_id.md`.

---

## 1. Goal

Build a small web app that lets a faculty member:

1. See a searchable list of all students (from `docker_id.md`).
2. Click a student and **download a dedicated Excel (.xlsx) file** for that student containing:
   - Their Docker Hub repositories (each as a clickable hyperlink)
   - The **GitHub repository URL** linked to each submission (clickable hyperlink)
   - **Submission date(s)**
   - **Download count** (Docker Hub `pull_count`)
3. (Bonus) Download one combined Excel file for all students.

## 2. Non-goals (do NOT build these)

- No login/auth system, no database, no user accounts.
- No browser extension.
- No scraping of HTML pages. Use only the public JSON APIs listed below.
- No paid services, no external hosting setup, no Docker Compose, no CI/CD.
- Do NOT modify `docker_id.md`.

## 3. Input data facts (verified from `docker_id.md`)

- It is a Markdown table with columns: `Roll Number | Name | Docker ID`.
- Data rows start with `| DEMO.U4CYS23` and there are 59 of them.
- The Docker ID cell is a Markdown link: `[docker_id](https://hub.docker.com/r/docker_id)`. The link text is the Docker ID.
- Roll numbers are NOT continuous (some numbers are skipped). Do not assume continuity.
- Some Docker IDs contain uppercase letters (e.g. `Demo-Student-37`, `Demo-Student-45`, `Demo-Student-57`, `Demo-Student-54`, `Demo-Student-51`). Docker Hub namespaces are lowercase, so **lowercase the ID before calling the API**, but keep the original text for display.
- Some rows have trailing spaces or extra whitespace. Trim all cells.
- The header row and the `|:---:|` separator row must be skipped.
- A name can be long (e.g. "Demo Student 54 With Four Extra Words Here"); do not truncate.

## 4. Tech stack (keep it simple)

- **Backend:** Python 3.11+, FastAPI, Uvicorn
- **HTTP client:** `httpx` (async) with timeouts and retries
- **Excel:** `openpyxl`
- **Frontend:** a single static `index.html` with vanilla JS and plain CSS (no React, no build step), served by FastAPI
- **Config:** `.env` file read via `python-dotenv` or plain `os.environ`
- **Tests:** `pytest`

If a library above is unavailable, choose the closest standard alternative and say so in the final report.

## 5. Folder structure

```
project-root/
├── docker_id.md                 # provided, read-only
├── IMPLEMENTATION_PLAN.md       # this file
├── README.md                    # how to run (you write this)
├── requirements.txt
├── .env.example
├── overrides.csv                # optional manual GitHub URL overrides (docker_id,github_url)
├── app/
│   ├── main.py                  # FastAPI app and routes
│   ├── config.py                # settings
│   ├── parser.py                # docker_id.md -> list of students
│   ├── dockerhub.py             # Docker Hub API client
│   ├── github_links.py          # GitHub URL extraction (+ optional GitHub API)
│   ├── excel.py                 # workbook builders
│   ├── cache.py                 # simple in-memory TTL cache
│   └── static/
│       └── index.html
└── tests/
    ├── test_parser.py
    ├── test_github_links.py
    └── test_excel.py
```

## 6. Data sources

### 6.1 Docker Hub public API (no authentication required)

Base: `https://hub.docker.com/v2`

| Purpose | Endpoint |
|---|---|
| List a user's repos | `GET /repositories/{namespace}/?page_size=100` (follow the `next` field for pagination) |
| Single repo details (includes full README text) | `GET /repositories/{namespace}/{repo}/` |
| List tags of a repo | `GET /repositories/{namespace}/{repo}/tags?page_size=100` (follow `next`) |

Fields expected (**verify against a real response before relying on them**):

- Repo list item: `name`, `namespace`, `description`, `pull_count`, `star_count`, `last_updated`, `date_registered`, `is_private`
- Single repo: the above plus `full_description`
- Tag item: `name`, `tag_last_pushed`, `last_updated`, `full_size`, `images` (with `architecture`, `os`)

**Definitions used in this app:**

- **Downloads** = `pull_count` of the repo. It is a cumulative count for the whole repo and includes pulls by anyone, not just the student. State this in the Excel notes sheet.
- **Submission date** is ambiguous, so output three separate columns and let faculty decide:
  - `Repo Created` = `date_registered`
  - `First Tag Pushed` = earliest `tag_last_pushed` among tags
  - `Last Pushed` = `last_updated` of the repo
- Optional: env var `SUBMISSION_DEADLINE` (ISO datetime). If set, add a status column: `On time` if `First Tag Pushed` is on or before the deadline, otherwise `Late`. If not set, omit the column.

### 6.2 GitHub URL discovery

The `.md` file does NOT contain GitHub URLs. Discover them in this order and stop at the first hit per repo:

1. `overrides.csv` (manual mapping `docker_id,github_url`), if the file exists.
2. Regex search in the repo `full_description` (README), then in `description`.
3. If nothing is found, output `Not found` (do not guess or construct a URL).

Regex target: `https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+`. Strip trailing punctuation, `.git`, and anything after the repo name (e.g. `/tree/main`). If multiple distinct URLs are found, put the first in the main column and the rest in an "Other GitHub links" column.

### 6.3 GitHub API (OPTIONAL, only if `GITHUB_TOKEN` is set)

For each discovered GitHub URL, call `GET https://api.github.com/repos/{owner}/{repo}` and add columns: `GitHub Created`, `GitHub Last Push` (`pushed_at`), `Stars`, `Forks`, `Is Fork`. On failure (404, rate limit), leave blank and continue. If no token is set, skip this step entirely and keep the app fully working.

## 7. Backend API

| Method | Route | Behavior |
|---|---|---|
| GET | `/` | Serves `index.html` |
| GET | `/api/students` | JSON list: `[{roll, name, docker_id}]` parsed from `docker_id.md` |
| GET | `/api/students/{docker_id}/preview` | JSON summary (repo count, total pulls, GitHub links found) for the UI to show before download |
| GET | `/api/export/{docker_id}` | Returns the student's `.xlsx` as a file download |
| GET | `/api/export-all` | Returns one combined `.xlsx` with one row per repo per student |
| GET | `/api/health` | `{"status":"ok"}` |

Rules:

- `{docker_id}` must be validated against the parsed student list (case-insensitive). Unknown IDs return 404. Never forward arbitrary user input to Docker Hub.
- Use a TTL cache (default 15 minutes, env `CACHE_TTL_SECONDS`) around Docker Hub responses.
- Limit concurrency to avoid rate limits (e.g. `asyncio.Semaphore(5)`) and retry on HTTP 429 or 5xx with backoff (max 3 attempts).
- For `export-all`, process students concurrently with the same semaphore, never fail the whole export because one student failed; record the error in that student's row.
- Filename format: `{roll}_{Name_With_Underscores}.xlsx`, sanitized for filesystem safety.

## 8. Excel specification (per-student workbook)

**Sheet 1: `Summary`**

- Name, Roll Number, Docker ID (hyperlink to `https://hub.docker.com/u/{docker_id}`), Total repos, Total pulls, Repos with GitHub link, Generated at (timestamp)

**Sheet 2: `Repositories`** (one row per repo)

| Column | Notes |
|---|---|
| Repo Name | Hyperlink to `https://hub.docker.com/r/{namespace}/{repo}` |
| GitHub URL | Hyperlink to the GitHub repo, or `Not found` |
| Other GitHub Links | Optional |
| Repo Created | Date and time |
| First Tag Pushed | Date and time |
| Last Pushed | Date and time |
| Downloads (Pulls) | Integer |
| Stars | Integer |
| Tags Count | Integer |
| Status | Only if `SUBMISSION_DEADLINE` set |
| Notes | e.g. "No public repos", "Private repo skipped" |

**Sheet 3: `Tags`** (optional but implement): Repo, Tag, Pushed At, Size (MB), Architectures

**Sheet 4: `Notes`**: Short definitions of "Downloads" and each date column, and the data source/time.

Formatting requirements: bold header row, frozen header, auto-sized columns (with a sane max width), real Excel date cells (not strings) with format `yyyy-mm-dd hh:mm`, dates in UTC and labelled as UTC, real hyperlinks (`cell.hyperlink`) with link styling.

**Combined workbook (`export-all`)**: one sheet `All Submissions` with columns: Roll, Name, Docker ID, Repo, GitHub URL, Repo Created, First Tag Pushed, Last Pushed, Pulls, Stars, Status/Error. Include a row even for students with no repos (with a clear note).

## 9. Frontend specification (`index.html`)

- Title and short description at the top.
- Search box filtering by name, roll number, or Docker ID (client-side, instant).
- Table of students: Roll, Name, Docker ID (link to Docker Hub), and a **Download Excel** button per row.
- Clicking a row's button triggers `/api/export/{docker_id}` and shows a loading state (spinner or disabled button), then an error toast on failure.
- Optional "Preview" action showing repo count, total pulls and GitHub link from `/api/students/{docker_id}/preview`.
- A top-right **Download all (Excel)** button.
- Responsive, clean, readable; no external CDN dependencies required.

## 10. Error handling and edge cases

- Docker Hub user not found (404): workbook still generated, with a note "Docker Hub user not found".
- User exists but has zero public repos: note "No public repositories".
- Private repos do not appear in the public API; do not attempt workarounds.
- Network timeout (10 s per request) or repeated failures: return a clear error message to the UI; never crash the server.
- Unicode and long names must not break filenames or sheet content.
- Excel sheet names must be at most 31 characters and avoid `[]:*?/\`.

## 11. Testing and acceptance criteria

Tests (pytest):

- `test_parser.py`: parses exactly 59 students from `docker_id.md`, correct Docker ID extraction (including mixed-case IDs), no header or separator rows included.
- `test_github_links.py`: regex extracts URLs from sample text, strips `.git` and `/tree/...`, handles none and multiple.
- `test_excel.py`: generates a workbook from a mocked Docker Hub payload and checks sheet names, headers, hyperlinks and date cell types.
- Mock all network calls in tests; tests must pass offline.

Acceptance criteria:

1. `uvicorn app.main:app --reload` starts without errors; `/` shows 59 students.
2. Search filters correctly.
3. Clicking Download for a student with real public repos returns an `.xlsx` that opens in Excel/LibreOffice with working hyperlinks.
4. Pull counts and dates match what is visible on the student's Docker Hub page (spot check at least 3 students manually and report the comparison).
5. Students with no repos or non-existent users do not break the app.
6. `export-all` completes for all 59 students and does not fail if some students error.
7. README explains setup in under 10 steps.

## 12. Build phases (stop and report after each)

**Phase 0: Verification (no app code yet).** Read this file and `docker_id.md`. Make real calls to the Docker Hub endpoints in section 6.1 for 2 or 3 students from the list using curl or a small script. Print the actual JSON field names you observe and compare against section 6.1. Report any differences. Do not continue until the field names are confirmed.

**Phase 1: Parser + tests.**

**Phase 2: Docker Hub client + cache + tests with mocks.**

**Phase 3: GitHub link extraction (+ optional GitHub API enrichment).**

**Phase 4: Excel builders (single and combined) + tests.**

**Phase 5: FastAPI routes + frontend.**

**Phase 6: README, `.env.example`, `requirements.txt`, manual end-to-end check, final report.**

## 13. Rules to prevent hallucination (follow strictly)

1. Do not invent data. If a value is unavailable, write `Not found` or leave the cell blank with a note. Never fabricate GitHub URLs, dates or counts.
2. Do not invent API fields or endpoints. Only use what you have confirmed in Phase 0. If a field is missing, say so.
3. Do not add features, services or dependencies outside this document without asking first.
4. Do not edit `docker_id.md`.
5. Treat the student count as exactly what the parser finds (expected 59). If the count differs, stop and report.
6. If anything in this document is ambiguous or conflicts with what you observe, stop and ask instead of choosing silently.
7. In your final report, list: what was built, what was tested, what was NOT verified, and any assumptions made.

## 14. Open questions (defaults if not answered)

| Question | Default |
|---|---|
| Where do students put their GitHub URL? | Docker Hub repo README or description (section 6.2) |
| What counts as "submission date"? | Show all three date columns |
| Is there a deadline to flag late submissions? | None; the Status column is hidden |
| Should GitHub API enrichment be used? | Only if `GITHUB_TOKEN` is set |
