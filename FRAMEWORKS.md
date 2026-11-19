# Architecture & Technology Stack: Frameworks and Libraries Explained

This document provides a comprehensive technical breakdown of the frameworks, libraries, and architectural choices powering the **Docker Hub Submission Evaluator**. It details why each technology was selected, how it is utilized within the codebase, alternatives that were considered, and the architectural trade-offs involved.

---

## 1. Executive Summary of Tech Stack

| Layer | Technology | Version | Purpose in Project |
|---|---|---|---|
| **Backend Framework** | [FastAPI](file:///d:/docker_automation/app/main.py) | `0.115.11` | Asynchronous REST API routing, request validation, error handling, static file mounting |
| **ASGI Web Server** | Uvicorn | `0.34.0` | High-performance asynchronous HTTP server hosting the FastAPI application |
| **Data Validation & Config** | [Pydantic & Pydantic-Settings](file:///d:/docker_automation/app/config.py) | `2.10.6` / `2.8.1` | Environment variable parsing, type-safe settings, input validation |
| **Multipart Uploads** | python-multipart | `0.0.32` | In-memory binary file upload streaming for `.xlsx` rosters |
| **Spreadsheet Engine** | [openpyxl](file:///d:/docker_automation/app/excel.py) | `3.1.5` | In-memory `.xlsx` parsing, formula sanitization, styled multi-sheet Excel generation |
| **Async HTTP Client** | [HTTPX](file:///d:/docker_automation/app/dockerhub.py) | `0.28.1` | Asynchronous Docker Hub and GitHub API calls with concurrency control and backoff |
| **Frontend Architecture** | [Vanilla JS / HTML5 / CSS3](file:///d:/docker_automation/app/static/index.html) | Modern ES6+ | Zero-build, dependency-free single-page dashboard with drag-and-drop & progress polling |
| **Testing Suite** | pytest & pytest-asyncio | `9.1.1` / `1.4.0` | 46 unit tests running 100% offline with mocked network requests |

All dependencies are strictly pinned in [requirements.txt](file:///d:/docker_automation/requirements.txt).

---

## 2. Backend Web Framework: FastAPI & Uvicorn

### Why FastAPI?
[FastAPI](file:///d:/docker_automation/app/main.py) is a modern, fast (high-performance) web framework for building APIs with Python 3.8+ based on standard Python type hints.

* **Native Asynchronous Concurrency:** Docker Hub evaluation involves querying dozens of student accounts, hundreds of repositories, and thousands of tags. FastAPI’s native `async`/`await` architecture allows the server to handle concurrent I/O operations without blocking the event loop or spinning up heavyweight OS threads.
* **Tight Pydantic Integration:** Request query parameters, path parameters, and configuration data are automatically validated, coerced, and documented.
* **Automatic Interactive API Documentation:** FastAPI automatically generates interactive Swagger/OpenAPI documentation at `/docs` and ReDoc documentation at `/redoc`.
* **Lightweight Footprint:** FastAPI imposes minimal runtime overhead compared to monolithic frameworks like Django.

### Why Uvicorn?
Uvicorn is a lightning-fast ASGI (Asynchronous Server Gateway Interface) server implementation built on `uvloop` and `httptools`.
* It provides the low-level HTTP transport layer that feeds requests into FastAPI.
* Supports live reloading (`--reload`) during local development and binding to all network interfaces (`--host 0.0.0.0`) for campus server deployment.

### Alternatives Considered
* **Flask:** Lacks native async request handling out-of-the-box. Asynchronous external API calls in Flask typically require complex background task workers (Celery/Redis) or thread pools, which would add unnecessary infrastructure complexity.
* **Django:** Overly heavyweight for this service. Includes an ORM, admin panel, session middleware, and database migrations that are unnecessary for a stateless, in-memory evaluator.
* **Tornado:** Capable of async I/O, but has a more dated API, lacks automatic OpenAPI documentation, and lacks Pydantic’s modern type validation system.

---

## 3. Spreadsheet Processing Engine: openpyxl

### Why openpyxl?
[openpyxl](file:///d:/docker_automation/app/excel.py) is a Python library for reading and writing Excel 2010 xlsx/xlsm/xltx/xltm files.

#### 1. In-Memory Roster Parsing ([app/roster.py](file:///d:/docker_automation/app/roster.py))
* **Memory Safety & Speed:** The parser initializes openpyxl with `read_only=True` and `data_only=True` directly from `io.BytesIO(upload_bytes)`:
  * `read_only=True`: Uses an iterative streaming XML reader instead of loading the entire worksheet DOM into RAM, drastically reducing memory usage.
  * `data_only=True`: Reads cached formula values rather than raw formula code, preventing formula evaluation overhead.
* **Zero Disk Persistence:** The uploaded workbook is never written to disk, eliminating filesystem storage leaks and neutralizing file-system-based attacks.
* **Security Against Malicious Macros:** Only standard `.xlsx` files are accepted; macro-enabled `.xlsm` workbooks are rejected immediately with HTTP 400.

#### 2. Styled Excel Report Generation ([app/excel.py](file:///d:/docker_automation/app/excel.py))
* **Full Styling Control:** openpyxl allows programmatic control over:
  * Font families, weights, colors, and sizes (`Font(name="Calibri", bold=True)`).
  * Cell backgrounds and alternating zebra striping (`PatternFill`).
  * Cell borders and header separators (`Border`, `Side`).
  * Column auto-fitting based on content length.
  * Native freeze panes (`ws.freeze_panes = "A5"`) and automatic filtering (`ws.auto_filter.ref`).
* **Native Date & Time Formatting:** openpyxl writes native Excel datetime objects with custom number formats (`yyyy-mm-dd hh:mm`), allowing faculty to sort and filter dates natively in Microsoft Excel.
* **Formula Injection Defense (`set_safe_cell`):** openpyxl allows setting cell values explicitly as strings (`cell.data_type = 's'`) and prefixing dangerous characters (`=`, `+`, `-`, `@`, `\t`, `\r`) with a single quote (`'`), neutralizing CSV/DDE injection attacks.

### Alternatives Considered
* **pandas (`pd.read_excel` / `pd.DataFrame.to_excel`):**
  * *Disadvantages:* Brings massive binary dependencies (`pandas`, `numpy`) totaling over 100 MB. Furthermore, pandas strips fine-grained cell formatting, hyperlinks, freeze panes, and custom multi-sheet title headers.
* **XlsxWriter:**
  * *Disadvantages:* XlsxWriter is strictly write-only; it cannot read or parse uploaded Excel rosters. Using XlsxWriter would require two separate libraries (one for reading, one for writing).
* **xlrd / xlwt:**
  * *Disadvantages:* `xlrd` deprecated support for `.xlsx` files in 2020 and only supports legacy `.xls` files. `xlwt` is also limited to legacy BIFF8 `.xls` files (max 65,536 rows).

---

## 4. Async Networking & Concurrency: HTTPX & asyncio

### Why HTTPX?
[HTTPX](file:///d:/docker_automation/app/dockerhub.py) is a next-generation HTTP client for Python, offering both synchronous and asynchronous APIs with standard HTTP/1.1 and HTTP/2 support.

* **Async/Await Interface:** Seamlessly integrates with FastAPI’s event loop using `httpx.AsyncClient`.
* **Connection Pooling:** Automatically reuses TCP connections across Docker Hub requests, reducing TLS handshake overhead.
* **Concurrency Control:** Paired with `asyncio.Semaphore(5)` to ensure the application never overwhelms the Docker Hub public API or exceeds client socket limits.
* **Robust Error Handling:** Provides distinct exception hierarchies (`httpx.HTTPStatusError`, `httpx.RequestError`) that power the client’s exponential backoff and retry loop for HTTP 429 (rate limited) and 5xx (server error) responses.

### Alternatives Considered
* **`requests`:** The industry standard for synchronous Python HTTP, but inherently blocking. Running synchronous `requests` calls inside an async FastAPI handler would freeze the server unless wrapped in thread pools (`run_in_executor`), adding thread synchronization overhead.
* **`aiohttp`:** Highly capable, but has a more verbose API, requires complex session management, and lacks the clean API parity with `requests` that HTTPX provides.
* **`urllib3` / `urllib.request`:** Low-level, synchronous standard library modules requiring substantial boilerplate for connection pooling, retries, and JSON decoding.

---

## 5. Configuration & Validation: Pydantic & Pydantic-Settings

### Why Pydantic?
[Pydantic](file:///d:/docker_automation/app/config.py) provides data validation and settings management using Python type annotations.

* **`BaseSettings` Architecture:** In [app/config.py](file:///d:/docker_automation/app/config.py), the `Settings` class reads environment variables from `.env` files automatically, casting strings into typed booleans, integers, and paths:
  ```python
  class Settings(BaseSettings):
      DISPLAY_TZ: str = "Asia/Kolkata"
      SUBMISSION_DATE_BASIS: str = "last_push"
      GITHUB_README_LOOKUP: bool = False
      MAX_UPLOAD_MB: int = 2
      MAX_STUDENTS: int = 500
      ROSTER_TTL_HOURS: int = 12
      MAX_ROSTERS: int = 50
      JOB_TTL_HOURS: int = 2
  ```
* **Fail-Fast Validation:** If an administrator enters an invalid value (e.g. `MAX_UPLOAD_MB="two"`), Pydantic raises an informative validation error at server startup rather than crashing silently during runtime.

---

## 6. Frontend Architecture: Vanilla JavaScript, HTML5 & CSS3

### Why Vanilla JS (Zero-Build Frontend)?
Rather than adopting a complex Single Page Application (SPA) framework (such as React, Vue, or Angular), the entire user interface in [app/static/index.html](file:///d:/docker_automation/app/static/index.html) is built with **pure, modern Vanilla JavaScript (ES6+), HTML5, and CSS3**.

```
Single index.html File ───> [No Node.js] ───> [No npm] ───> [No Webpack/Vite] ───> Direct Browser Execution
```

#### Key Advantages:
1. **Zero Build Pipeline:** No Node.js runtime, no npm/yarn package managers, and no bundlers (Webpack, Vite, Rollup) are required. The application runs immediately upon `pip install` and `uvicorn`.
2. **Instant Delivery & Low Latency:** The entire frontend is served in a single lightweight HTTP GET request. CSS and JS parse in milliseconds.
3. **Campus Intranet Longevity:** Standard HTML5 and ES6 APIs will continue to run unchanged in modern web browsers for decades without node_modules dependency decay, deprecation warnings, or security vulnerabilities in third-party npm packages.
4. **Direct Browser API Utilization:**
   * **HTML5 Drag-and-Drop API:** Provides the file upload drop zone.
   * **Fetch API:** Handles asynchronous `multipart/form-data` uploads and streaming JSON queries.
   * **Web Storage API (`localStorage`):** Remembers active `roster_id` and uploaded file names across page reloads.
   * **URLSearchParams:** Enables direct deep-linking to active rosters via `?roster_id=<id>`.
   * **Native Timers (`setInterval`):** Coordinates 2-second polling of background export jobs.

---

## 7. Automated Testing: pytest & pytest-asyncio

### Why pytest?
[pytest](file:///d:/docker_automation/tests) is the premier Python testing framework.

* **Concise Assertions:** Uses standard Python `assert` statements rather than cumbersome `self.assertEqual()` patterns.
* **Parametrized Testing:** Enables evaluating edge cases (e.g., various header aliases, malformed URLs, and formula injection payloads) across dozens of inputs with clean syntax.
* **`pytest-asyncio`:** Allows test functions to be written as native `async def test_...()` functions, enabling offline testing of async routes, jobs, and clients.
* **100% Offline Testing:** Network calls to Docker Hub and GitHub are monkeypatched in test suites, allowing the entire 46-test suite to execute in ~12 seconds with zero network dependency.

---

## 8. Summary Comparison Matrix

| Capability | Selected Framework / Library | Primary Alternative | Deciding Factor |
|---|---|---|---|
| **Web API Engine** | **FastAPI** | Flask | Native async concurrency, Pydantic type validation, automatic OpenAPI docs |
| **HTTP Transport** | **Uvicorn** | Gunicorn | High-performance ASGI event loop (`uvloop`), lightweight footprint |
| **Excel Manipulation** | **openpyxl** | pandas | Preserves formulas, cell styling, freeze panes, hyperlinks; no heavy C/scientific binaries |
| **Outbound HTTP** | **HTTPX** | requests | Native async/await support, connection pooling, easy retry handlers |
| **Config Management** | **Pydantic** | os.environ / python-dotenv | Strong type enforcement, automated coercion, fail-fast startup checks |
| **User Interface** | **Vanilla JS / CSS3** | React / Vue | Zero build pipeline, no npm ecosystem dependency, maximum long-term maintainability |
| **Test Automation** | **pytest** | unittest | Fixture composability, async test support, simple assertion syntax |

---

## 9. Codebase Cross-References

- [app/main.py](file:///d:/docker_automation/app/main.py): FastAPI routing, CORS, static file hosting, and endpoint handlers.
- [app/roster.py](file:///d:/docker_automation/app/roster.py): In-memory openpyxl Excel parsing, validation, and `RosterStore`.
- [app/dockerhub.py](file:///d:/docker_automation/app/dockerhub.py): HTTPX async client, pagination, and retry logic.
- [app/excel.py](file:///d:/docker_automation/app/excel.py): openpyxl workbook generator and formula injection protection (`set_safe_cell`).
- [app/jobs.py](file:///d:/docker_automation/app/jobs.py): Background job coordinator using asyncio.
- [app/config.py](file:///d:/docker_automation/app/config.py): Pydantic-Settings environment variables and defaults.
- [app/static/index.html](file:///d:/docker_automation/app/static/index.html): Vanilla JavaScript single-page application.
- [requirements.txt](file:///d:/docker_automation/requirements.txt): Pinned package versions.
