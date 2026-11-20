# Docker Hub Submission Evaluator

A web application built with FastAPI and vanilla JavaScript for faculty to evaluate student Docker Hub submissions. Faculty upload a student roster as an Excel spreadsheet, search and preview live Docker Hub repositories and submission dates, and download dedicated or combined Excel reports.

---

## Key Features

- **Dynamic Roster Upload:** Faculty upload a roster in standard Excel (`.xlsx`) format. Parsed entirely in-memory on the server (`openpyxl`, `data_only=True`), never saved to disk.
- **Primary Faculty Need (Live Repositories & Submission Dates):**
  > *"For a student, show ALL repository names they submitted, each with a clickable link and its submission date."*
  - Queries Docker Hub live for public repositories, paginating through all repos and tags without limit.
  - Official **Submission Date** displays the most recent tag push timestamp in Indian Standard Time (`Asia/Kolkata` / UTC+5:30).
  - Repositories without pushed tags show blank dates with the note `"No image pushed"`.
  - Configurable date basis via `SUBMISSION_DATE_BASIS` (`last_push`, `first_push`, or `created`).
- **Interactive Web Preview Modal:** Live view of a student's repositories, submission timestamps, pull counts, and GitHub links before downloading Excel.
- **Asynchronous Background Export-All:** For rosters with many students, the combined export runs as a background job with a real-time progress bar, polling updates every 2s and auto-downloading when ready.
- **College Branding:** Amrita Vishwa Vidyapeetham and Institution's Innovation Council (IIC) logos, with a crimson-and-yellow theme matching amrita.edu. Logo files live in `app/static/img/`.
- **Formula Injection Protection:** Every cell written to generated Excel workbooks beginning with `=`, `+`, `-`, `@`, `\t`, or `\r` is automatically prefixed with a single quote (`'`) and set to string data type.

---

## Input Roster Excel Format

The uploaded file must be an `.xlsx` workbook:
- **Sheet:** Uses sheet named `"Students"` if present, otherwise the first sheet.
- **Header Row:** Automatically detected in the first 10 rows. Recognised header aliases:
  - **Docker ID (Mandatory):** `Docker ID`, `dockerid`, `Docker URL`, `Docker Username`, `Docker User`, `Docker Hub ID`, `docker`.
  - **Roll No (Optional):** `Roll No`, `roll number`, `roll`, `rollno`, `reg no`, `register number`.
  - **Name (Optional):** `Name`, `student name`, `student`.
- **Docker ID Values:** Accepts usernames (e.g., `alice`) or full profile URLs (e.g., `https://hub.docker.com/u/alice`).
- **Missing Roll/Name:** Displays cleanly as `"—"` in the UI and `"-"` in exported reports.
- **Limits:** Maximum 500 students per upload, maximum 2 MB file size.
- **Template:** Download a sample template anytime via the UI or `GET /api/template`.

---

## How to Run in 5 Steps

### 1. Prerequisites
Python 3.11+ installed.

### 2. Set Up a Virtual Environment
```bash
python -m venv .venv
# On Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment
```bash
cp .env.example .env
```
*(Optionally set `GITHUB_TOKEN`, `SUBMISSION_DATE_BASIS`, or `SUBMISSION_DEADLINE`).*

### 5. Start the Application Server
```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```
Open your browser and navigate to:
**[http://127.0.0.1:8000](http://127.0.0.1:8000)**

---

## Campus Server Deployment

To host the evaluator on a campus server accessible to faculty across the local network:

1. **Bind to all network interfaces:**
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
2. **Access from faculty workstations:**
   Faculty can open their browser and visit:
   ```
   http://<server-ip>:8000
   ```
   *(Find the server IP using `ipconfig` on Windows or `ip addr` on Linux).*
   Run a single worker only (no `--workers N`): uploaded rosters, export jobs and the cache live in process memory.

3. **Windows Firewall Rule (if running on Windows Server):**
   Open PowerShell as Administrator and run:
   ```powershell
   New-NetFirewallRule -DisplayName "Docker Submission Evaluator" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
   ```

4. **Security Notice:**
   The application contains no built-in authentication system. It is designed for internal use within a trusted campus LAN or VPN. If deploying beyond an isolated intranet, place the application behind a reverse proxy (such as Caddy or Nginx) configured with HTTPS and HTTP Basic Authentication.

---

## Configuration Reference

| Environment Variable | Default | Description |
|---|---|---|
| `DISPLAY_TZ` | `Asia/Kolkata` | Timezone for displayed timestamps (UTC+5:30) |
| `SUBMISSION_DATE_BASIS` | `last_push` | Date calculation rule: `last_push`, `first_push`, or `created` |
| `GITHUB_README_LOOKUP` | `false` | When `false`, skips fetching per-repo detail endpoint. When `true`, queries repo READMEs for GitHub URLs |
| `MAX_UPLOAD_MB` | `2` | Maximum file size for uploaded roster files in MB |
| `MAX_STUDENTS` | `500` | Maximum student rows allowed per uploaded roster |
| `ROSTER_TTL_HOURS` | `12` | In-memory roster retention TTL in hours |
| `MAX_ROSTERS` | `50` | Maximum concurrent in-memory rosters (LRU eviction) |
| `JOB_TTL_HOURS` | `2` | Background export job retention TTL in hours |
| `SUBMISSION_DEADLINE` | *(None)* | Optional ISO datetime to enable the "Status" column (On time / Late) |
| `GITHUB_TOKEN` | *(None)* | Reserved for GitHub API enrichment (not yet wired into reports; currently has no effect) |

---

## Running Automated Tests

Run the complete test suite completely offline with mocked network calls:
```bash
pytest
```
*All 58 test cases execute in under 10 seconds.*
