"""Regression tests for the post-handoff reliability fixes:
- failed tag / repo-list fetches must not look like real results or be cached
- non-ASCII download filenames must not break the response
- background export tasks must be strongly referenced
"""
import asyncio
import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.cache import cache
from app.config import settings
from app.dockerhub import (
    DockerHubClient,
    REPOS_PARTIAL_NOTE,
    TAGS_PARTIAL_NOTE,
    TAGS_UNAVAILABLE_NOTE,
)
from app.excel import build_combined_workbook, content_disposition
from app.jobs import ExportJob, job_manager
from app.main import app

BASE = "https://hub.docker.com/v2"


class FakeResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json


def repo(name):
    return {
        "name": name,
        "pull_count": 1,
        "star_count": 0,
        "date_registered": "2026-06-01T10:00:00Z",
        "last_updated": "2026-06-02T10:00:00Z",
        "description": "",
    }


def make_fetch(routes):
    """routes: url -> FakeResponse or None (None simulates exhausted retries)."""
    async def fake_fetch(self_client, client_obj, url):
        if url in routes:
            return routes[url]
        if "/users/" in url:
            return FakeResponse(200, {"username": "x"})
        return FakeResponse(404)
    return fake_fetch


@pytest.fixture(autouse=True)
def _reset():
    cache.clear()
    settings.SUBMISSION_DATE_BASIS = "last_push"
    yield
    cache.clear()


@pytest.mark.asyncio
async def test_failed_tag_fetch_is_not_reported_as_no_image_pushed(monkeypatch):
    routes = {
        f"{BASE}/repositories/stu/?page_size=100": FakeResponse(200, {"next": None, "results": [repo("good"), repo("ratelimited"), repo("empty")]}),
        f"{BASE}/repositories/stu/good/tags?page_size=100": FakeResponse(200, {"next": None, "results": [{"name": "v1", "tag_last_pushed": "2026-06-05T10:00:00Z"}]}),
        f"{BASE}/repositories/stu/ratelimited/tags?page_size=100": None,  # 429s exhausted
        f"{BASE}/repositories/stu/empty/tags?page_size=100": FakeResponse(200, {"next": None, "results": []}),
    }
    monkeypatch.setattr(DockerHubClient, "_fetch", make_fetch(routes))

    profile = await DockerHubClient().get_student_profile("1", "S", "stu", use_cache=True)
    by_name = {s.name: s for s in profile.submissions}

    assert by_name["good"].data_incomplete is False
    assert by_name["good"].notes == ""

    assert by_name["ratelimited"].data_incomplete is True
    assert by_name["ratelimited"].notes == TAGS_UNAVAILABLE_NOTE
    assert by_name["ratelimited"].no_image_pushed is False

    assert by_name["empty"].notes == "No image pushed"
    assert by_name["empty"].no_image_pushed is True

    assert "Tag data incomplete for 1 repo(s)" in profile.error_note
    # Incomplete result must not be cached
    assert cache.get("student_profile:stu:last_push") is None


@pytest.mark.asyncio
async def test_partial_tag_pagination_is_flagged(monkeypatch):
    routes = {
        f"{BASE}/repositories/stu/?page_size=100": FakeResponse(200, {"next": None, "results": [repo("big")]}),
        f"{BASE}/repositories/stu/big/tags?page_size=100": FakeResponse(200, {"next": f"{BASE}/repositories/stu/big/tags?page=2", "results": [{"name": "v1", "tag_last_pushed": "2026-06-05T10:00:00Z"}]}),
        f"{BASE}/repositories/stu/big/tags?page=2": FakeResponse(503),
    }
    monkeypatch.setattr(DockerHubClient, "_fetch", make_fetch(routes))

    profile = await DockerHubClient().get_student_profile("1", "S", "stu", use_cache=True)
    sub = profile.submissions[0]
    assert sub.data_incomplete is True
    assert sub.notes == TAGS_PARTIAL_NOTE
    assert sub.submission_date == "2026-06-05T10:00:00Z"  # best available, but flagged
    assert cache.get("student_profile:stu:last_push") is None


@pytest.mark.asyncio
async def test_partial_repo_pagination_is_flagged_and_not_cached(monkeypatch):
    routes = {
        f"{BASE}/repositories/stu/?page_size=100": FakeResponse(200, {"next": f"{BASE}/repositories/stu/?page=2", "results": [repo("a")]}),
        f"{BASE}/repositories/stu/?page=2": None,
        f"{BASE}/repositories/stu/a/tags?page_size=100": FakeResponse(200, {"next": None, "results": []}),
    }
    monkeypatch.setattr(DockerHubClient, "_fetch", make_fetch(routes))

    profile = await DockerHubClient().get_student_profile("1", "S", "stu", use_cache=True)
    assert profile.total_repos == 1
    assert REPOS_PARTIAL_NOTE in profile.error_note
    assert cache.get("student_profile:stu:last_push") is None


@pytest.mark.asyncio
async def test_complete_result_is_still_cached(monkeypatch):
    routes = {
        f"{BASE}/repositories/stu/?page_size=100": FakeResponse(200, {"next": None, "results": [repo("a")]}),
        f"{BASE}/repositories/stu/a/tags?page_size=100": FakeResponse(200, {"next": None, "results": [{"name": "v1", "tag_last_pushed": "2026-06-05T10:00:00Z"}]}),
    }
    monkeypatch.setattr(DockerHubClient, "_fetch", make_fetch(routes))

    profile = await DockerHubClient().get_student_profile("1", "S", "stu", use_cache=True)
    assert profile.error_note == ""
    assert cache.get("student_profile:stu:last_push") is profile


@pytest.mark.asyncio
async def test_repo_list_server_error_reports_failure(monkeypatch):
    routes = {f"{BASE}/repositories/stu/?page_size=100": FakeResponse(500)}
    monkeypatch.setattr(DockerHubClient, "_fetch", make_fetch(routes))

    profile = await DockerHubClient().get_student_profile("1", "S", "stu", use_cache=True)
    assert profile.error_note.startswith("Failed to fetch repositories")
    assert cache.get("student_profile:stu:last_push") is None


@pytest.mark.asyncio
async def test_combined_workbook_counts_only_real_no_image_repos(monkeypatch):
    routes = {
        f"{BASE}/repositories/stu/?page_size=100": FakeResponse(200, {"next": None, "results": [repo("ratelimited"), repo("empty")]}),
        f"{BASE}/repositories/stu/ratelimited/tags?page_size=100": None,
        f"{BASE}/repositories/stu/empty/tags?page_size=100": FakeResponse(200, {"next": None, "results": []}),
    }
    monkeypatch.setattr(DockerHubClient, "_fetch", make_fetch(routes))
    profile = await DockerHubClient().get_student_profile("1", "S", "stu", use_cache=False)

    wb = build_combined_workbook([profile])
    ws = wb["Student Summary"]
    headers = [c.value for c in ws[1]]
    col = headers.index("Repos With No Image Pushed") + 1
    assert ws.cell(row=2, column=col).value == 1


def test_content_disposition_ascii_and_unicode():
    assert content_disposition("report.xlsx") == 'attachment; filename="report.xlsx"'

    header = content_disposition("All_Submissions_மாணவர்_2026-11-19.xlsx")
    header.encode("latin-1")  # must not raise
    assert 'filename="All_Submissions__2026-11-19.xlsx"' in header
    assert "filename*=UTF-8''All_Submissions_%E0%AE%AE" in header

    assert 'filename="download.xlsx"' in content_disposition("மாணவர்.xlsx")


def test_job_download_with_unicode_filename():
    client = TestClient(app)
    job = ExportJob(
        job_id="unicode-job",
        roster_id="r",
        uploaded_filename="மாணவர்.xlsx",
        status="done",
        done=1,
        total=1,
        excel_bytes=b"PK fake",
        download_filename="All_Submissions_மாணவர்_2026-11-19.xlsx",
    )
    job_manager._jobs[job.job_id] = job
    try:
        res = client.get("/api/jobs/unicode-job/download")
        assert res.status_code == 200
        assert "filename*=UTF-8''" in res.headers["content-disposition"]
    finally:
        job_manager._jobs.pop(job.job_id, None)


@pytest.mark.asyncio
async def test_job_task_is_strongly_referenced(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run(self, job, roster):
        started.set()
        await release.wait()
        job.status = "done"

    monkeypatch.setattr(type(job_manager), "_run_export_job", fake_run)

    class R:
        roster_id = "strong-ref-roster"
        filename = "r.xlsx"
        students = []

    job_id = await job_manager.start_job(R())
    await started.wait()
    assert len(job_manager._tasks) >= 1
    release.set()
    await asyncio.sleep(0.01)
    assert not any(not t.done() for t in job_manager._tasks)
    job_manager._jobs.pop(job_id, None)
