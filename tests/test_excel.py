import io
from datetime import datetime
import pytest
from openpyxl import load_workbook

from app.dockerhub import StudentProfileSummary, RepoSubmission, TagInfo
from app.excel import (
    build_student_workbook,
    build_combined_workbook,
    workbook_to_bytes,
    sanitize_filename,
    parse_and_convert_to_display_tz_naive,
)

def create_mock_submissions(has_github: bool = False):
    sub1 = RepoSubmission(
        name="repo-newest",
        namespace="testuser",
        repo_url="https://hub.docker.com/r/testuser/repo-newest",
        description="A sample web app",
        pull_count=120,
        star_count=5,
        date_registered="2026-06-20T12:00:00Z",
        last_updated="2026-06-25T10:05:00Z",
        first_tag_pushed="2026-06-22T10:00:00Z",
        latest_tag_pushed="2026-06-25T10:00:00Z",
        submission_date="2026-06-25T10:00:00Z",
        tags_count=2,
        github_url="https://github.com/testuser/repo-newest" if has_github else "Not found",
        other_github_urls=["https://github.com/testuser/docs"] if has_github else [],
        status="On time",
        notes="",
        tags=[
            TagInfo("v1", "2026-06-22T10:00:00Z", "2026-06-22T10:00:00Z", 1000, "amd64", "linux"),
            TagInfo("v2", "2026-06-25T10:00:00Z", "2026-06-25T10:00:00Z", 2000, "amd64", "linux"),
        ],
    )
    sub2 = RepoSubmission(
        name="repo-no-tags",
        namespace="testuser",
        repo_url="https://hub.docker.com/r/testuser/repo-no-tags",
        description="",
        pull_count=0,
        star_count=0,
        date_registered="2026-06-01T12:00:00Z",
        last_updated="2026-06-01T12:00:00Z",
        first_tag_pushed=None,
        latest_tag_pushed=None,
        submission_date=None,
        tags_count=0,
        github_url="Not found",
        other_github_urls=[],
        status=None,
        notes="No image pushed",
        tags=[],
    )
    return [sub1, sub2]

def create_mock_profile(has_github: bool = False) -> StudentProfileSummary:
    subs = create_mock_submissions(has_github=has_github)
    return StudentProfileSummary(
        roll="DEMO.U4CYS23999",
        name="Test Student",
        docker_id="TestUser",
        docker_id_lower="testuser",
        user_found=True,
        total_repos=2,
        total_pulls=120,
        repos_with_github=1 if has_github else 0,
        submissions=subs,
        error_note="",
    )

def test_sanitize_filename():
    assert sanitize_filename("DEMO.U4CYS23001_Demo Student 01") == "DEMO.U4CYS23001_Demo_Student_01"
    assert sanitize_filename("Invalid/Name:With*Chars") == "Invalid_Name_With_Chars"

def test_parse_and_convert_to_display_tz_naive():
    naive_dt = parse_and_convert_to_display_tz_naive("2026-06-25T10:00:00Z")
    assert naive_dt is not None
    assert naive_dt.tzinfo is None  # Must be naive for openpyxl
    assert naive_dt.hour == 15
    assert naive_dt.minute == 30

def test_single_student_excel_sheet_order_and_active():
    profile = create_mock_profile(has_github=False)
    wb = build_student_workbook(profile)

    # 1. Sheet order: "Repositories" FIRST
    assert wb.sheetnames == ["Repositories", "Summary", "Tags", "Notes"]
    # 2. Active sheet must be Repositories
    assert wb.active.title == "Repositories"

def test_repositories_sheet_layout_and_headers():
    profile = create_mock_profile(has_github=False)
    wb = build_student_workbook(profile)
    ws = wb["Repositories"]

    # Row 1: Title
    assert "Submitted Repositories: Test Student (DEMO.U4CYS23999)" in str(ws["A1"].value)
    # Row 2: Metadata / Subtitle
    assert "Total repositories: 2" in str(ws["A2"].value)
    assert "Date basis:" in str(ws["A2"].value)
    # Row 3: Blank
    assert ws["A3"].value is None

    # Row 4: Header
    headers = [cell.value for cell in ws[4]]
    expected_headers = [
        "S.No",
        "Repository Name",
        "Submission Date (IST)",
        "Repo Created (IST)",
        "First Push (IST)",
        "Last Push (IST)",
        "Downloads (Pulls)",
        "Stars",
        "Tags Count",
        "Notes",
    ]
    assert headers == expected_headers
    # When has_github is False, GitHub columns must be omitted!
    assert "GitHub URL" not in headers

    # Freeze panes below row 4 (A5)
    assert ws.freeze_panes == "A5"
    assert ws.auto_filter.ref is not None

    # Row 5: First repo (repo-newest)
    assert ws["A5"].value == 1
    assert ws["B5"].value == "repo-newest"
    assert ws["B5"].hyperlink is not None
    assert ws["B5"].hyperlink.target == "https://hub.docker.com/r/testuser/repo-newest"
    assert isinstance(ws["C5"].value, datetime)  # Submission Date is real datetime

    # Row 6: Second repo (repo-no-tags)
    assert ws["A6"].value == 2
    assert ws["B6"].value == "repo-no-tags"
    assert ws["C6"].value is None  # Submission date blank
    assert ws["J6"].value == "No image pushed"  # Note column

def test_repositories_sheet_with_github_columns():
    profile = create_mock_profile(has_github=True)
    wb = build_student_workbook(profile)
    ws = wb["Repositories"]
    headers = [cell.value for cell in ws[4]]
    # When has_github is True, GitHub columns must be shown!
    assert "GitHub URL" in headers
    assert "Other GitHub Links" in headers

def test_combined_workbook_sheets_and_student_summary():
    # Build 59 mock students to verify Student Summary has 59 rows
    profiles = []
    for i in range(1, 60):
        roll = f"DEMO.U4CYS230{i:02d}"
        subs = create_mock_submissions(has_github=False)
        p = StudentProfileSummary(
            roll=roll,
            name=f"Student {i}",
            docker_id=f"user{i}",
            docker_id_lower=f"user{i}",
            user_found=True,
            total_repos=2,
            total_pulls=120,
            repos_with_github=0,
            submissions=subs,
            error_note="",
        )
        profiles.append(p)

    wb = build_combined_workbook(profiles)
    assert wb.sheetnames == ["All Submissions", "Student Summary"]

    ws_sum = wb["Student Summary"]
    # Header is row 1, 59 students -> max_row == 60
    assert ws_sum.max_row == 60
    assert ws_sum["A1"].value == "Roll Number"
    assert ws_sum["D1"].value == "Total Repositories"
    assert ws_sum["H1"].value == "Repos With No Image Pushed"

    # First student in summary
    assert ws_sum["A2"].value == "DEMO.U4CYS23001"
    assert ws_sum["D2"].value == 2
    assert ws_sum["E2"].value == 120
    assert isinstance(ws_sum["F2"].value, datetime)  # Latest date
    assert ws_sum["H2"].value == 1  # 1 repo with no image pushed

    ws_all = wb["All Submissions"]
    # 59 students * 2 repos = 118 data rows + 1 header = 119 rows
    assert ws_all.max_row == 119
    assert ws_all.freeze_panes == "A2"
    assert ws_all.auto_filter.ref is not None

def test_workbook_to_bytes():
    profile = create_mock_profile()
    wb = build_student_workbook(profile)
    b = workbook_to_bytes(wb)
    assert isinstance(b, bytes)
    assert len(b) > 0

    reloaded = load_workbook(io.BytesIO(b))
    assert reloaded.sheetnames == ["Repositories", "Summary", "Tags", "Notes"]
    assert reloaded.active.title == "Repositories"
