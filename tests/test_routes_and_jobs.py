import io
import pytest
from unittest.mock import AsyncMock, patch
from openpyxl import load_workbook, Workbook
from fastapi.testclient import TestClient

from app.main import app
from app.roster import roster_store, parse_roster_excel
from app.jobs import job_manager
from app.dockerhub import StudentProfileSummary, RepoSubmission

client = TestClient(app)

def make_test_excel(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Students"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

def test_template_download():
    res = client.get("/api/template")
    assert res.status_code == 200
    assert "roster_template.xlsx" in res.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(res.content))
    assert "Students" in wb.sheetnames
    assert "Instructions" in wb.sheetnames
    ws = wb["Students"]
    assert ws.cell(row=1, column=1).value == "Roll No"
    assert ws.cell(row=1, column=2).value == "Name"
    assert ws.cell(row=1, column=3).value == "Docker ID"

def test_wrong_extension_rejected():
    files = {"file": ("students.csv", b"Roll No,Name,Docker ID\n1,Alice,alice", "text/csv")}
    res = client.post("/api/roster", files=files)
    assert res.status_code == 400
    assert "Only Excel (.xlsx) files are supported" in res.json()["detail"]

def test_oversize_file_rejected():
    # 2.1 MB
    big_data = b"x" * (2100 * 1024)
    files = {"file": ("big.xlsx", big_data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    res = client.post("/api/roster", files=files)
    assert res.status_code == 413
    assert "exceeds the maximum size limit" in res.json()["detail"]

def test_upload_roster_and_students():
    data = make_test_excel([
        ["Roll No", "Student Name", "Docker Username"],
        ["101", "Alice", "aliceuser"],
        ["102", "Bob", "bobuser"],
    ])
    files = {"file": ("test_roster.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    res = client.post("/api/roster", files=files)
    assert res.status_code == 200
    body = res.json()
    assert "roster_id" in body
    assert body["students_loaded"] == 2
    roster_id = body["roster_id"]

    # List students
    res_list = client.get(f"/api/roster/{roster_id}/students")
    assert res_list.status_code == 200
    students = res_list.json()
    assert len(students) == 2
    assert students[0]["docker_id"] == "aliceuser"
    assert students[1]["docker_id"] == "bobuser"

def test_expired_or_invalid_roster_404():
    res = client.get("/api/roster/non_existent_roster/students")
    assert res.status_code == 404
    assert "Roster expired or not found" in res.json()["detail"]

@patch("app.main.dockerhub_client.get_student_profile")
def test_preview_and_export_single_student(mock_get_profile):
    mock_get_profile.return_value = StudentProfileSummary(
        roll="101",
        name="Alice",
        docker_id="aliceuser",
        docker_id_lower="aliceuser",
        user_found=True,
        total_repos=1,
        total_pulls=42,
        repos_with_github=0,
        submissions=[
            RepoSubmission(
                name="my-app",
                namespace="aliceuser",
                repo_url="https://hub.docker.com/r/aliceuser/my-app",
                description="",
                submission_date="2026-11-15T10:00:00Z",
                date_registered="2026-11-01T10:00:00Z",
                last_updated="2026-11-15T10:00:00Z",
                first_tag_pushed="2026-11-10T10:00:00Z",
                latest_tag_pushed="2026-11-15T10:00:00Z",
                pull_count=42,
                star_count=2,
                tags_count=1,
                github_url="Not found",
                other_github_urls=[],
                notes="",
                status=None,
                tags=[]
            )
        ]
    )

    data = make_test_excel([
        ["Roll No", "Name", "Docker ID"],
        ["101", "Alice", "aliceuser"]
    ])
    files = {"file": ("roster.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    res_upload = client.post("/api/roster", files=files)
    roster_id = res_upload.json()["roster_id"]

    # Preview
    res_prev = client.get(f"/api/roster/{roster_id}/students/aliceuser/preview")
    assert res_prev.status_code == 200
    prev_data = res_prev.json()
    assert prev_data["total_repos"] == 1
    assert prev_data["total_pulls"] == 42
    assert len(prev_data["repos"]) == 1

    # Single Export
    res_exp = client.get(f"/api/roster/{roster_id}/students/aliceuser/export")
    assert res_exp.status_code == 200
    assert "101_aliceuser_" in res_exp.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(res_exp.content))
    assert wb.active.title == "Repositories"

@patch("app.jobs.dockerhub_client.get_student_profile")
def test_export_all_job_progression_and_download(mock_get_profile):
    mock_get_profile.return_value = StudentProfileSummary(
        roll="101",
        name="Alice",
        docker_id="aliceuser",
        docker_id_lower="aliceuser",
        user_found=True,
        total_repos=1,
        total_pulls=10,
        repos_with_github=0,
        submissions=[]
    )

    data = make_test_excel([
        ["Roll No", "Name", "Docker ID"],
        ["101", "Alice", "aliceuser"]
    ])
    files = {"file": ("batch.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    res_upload = client.post("/api/roster", files=files)
    roster_id = res_upload.json()["roster_id"]

    # Start job
    res_start = client.post(f"/api/roster/{roster_id}/export-all/start")
    assert res_start.status_code == 200
    job_id = res_start.json()["job_id"]

    # In test client or async environment, wait a tiny bit for background task
    import time
    for _ in range(20):
        res_job = client.get(f"/api/jobs/{job_id}")
        assert res_job.status_code == 200
        job_data = res_job.json()
        if job_data["status"] == "done":
            break
        time.sleep(0.1)

    assert job_data["status"] == "done"
    assert job_data["done"] == 1
    assert job_data["total"] == 1

    # Download
    res_dl = client.get(f"/api/jobs/{job_id}/download")
    assert res_dl.status_code == 200
    assert "All_Submissions_batch_" in res_dl.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(res_dl.content))
    assert "All Submissions" in wb.sheetnames
    assert "Student Summary" in wb.sheetnames

def test_formula_injection_protection():
    from app.excel import set_safe_cell, build_student_workbook
    wb = Workbook()
    ws = wb.active
    c = ws.cell(row=1, column=1)

    # Test dangerous formula strings
    for prefix in ['=', '+', '-', '@', '\t', '\r']:
        val = f"{prefix}HYPERLINK('http://evil.com')"
        set_safe_cell(c, val)
        assert c.data_type == 's'
        assert c.value.startswith("'")

    # Test normal strings
    set_safe_cell(c, "Alice")
    assert c.value == "Alice"
