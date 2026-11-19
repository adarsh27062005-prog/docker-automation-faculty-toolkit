import io
import pytest
from openpyxl import Workbook
from app.roster import parse_roster_excel, normalize_docker_id, RosterParseError, roster_store, Roster, Student
from app.config import settings

def make_excel_bytes(sheet_name="Students", rows=None):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    if rows:
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

def test_normalize_docker_id():
    assert normalize_docker_id("demo-student-01") == "demo-student-01"
    assert normalize_docker_id("demo-student-01") == "demo-student-01"
    assert normalize_docker_id("https://hub.docker.com/u/demo-student-02/") == "demo-student-02"
    assert normalize_docker_id("https://hub.docker.com/r/demo-student-02?param=1#sec") == "demo-student-02"
    assert normalize_docker_id("  demo-student-59  ") == "demo-student-59"
    assert normalize_docker_id("invalid id!") is None
    assert normalize_docker_id("") is None
    assert normalize_docker_id(None) is None

def test_header_aliases():
    aliases = ["Docker ID", "docker_id", "Docker Username", "Docker URL", "Docker Hub ID", "docker"]
    for alias in aliases:
        data = make_excel_bytes(rows=[
            ["Roll No", "Student Name", alias],
            ["101", "Alice", "aliceuser"]
        ])
        roster = parse_roster_excel(data, filename="test.xlsx")
        assert len(roster.students) == 1
        assert roster.students[0].docker_id == "aliceuser"
        assert roster.students[0].roll == "101"
        assert roster.students[0].name == "Alice"

def test_docker_id_only_file():
    data = make_excel_bytes(rows=[
        ["Docker ID"],
        ["demo-student-06"],
        ["aliceuser"]
    ])
    roster = parse_roster_excel(data, filename="only_id.xlsx")
    assert len(roster.students) == 2
    assert roster.students[0].docker_id == "demo-student-06"
    assert roster.students[0].roll == ""
    assert roster.students[0].name == ""
    assert roster.students[1].docker_id == "aliceuser"

def test_numeric_roll_number():
    data = make_excel_bytes(rows=[
        ["Roll No", "Name", "Docker ID"],
        [3, "Bob", "bobuser"],
        [4.0, "Charlie", "charlieuser"]
    ])
    roster = parse_roster_excel(data, filename="numeric.xlsx")
    assert len(roster.students) == 2
    assert roster.students[0].roll == "3"
    assert roster.students[1].roll == "4"

def test_missing_docker_column_raises_error():
    data = make_excel_bytes(rows=[
        ["Roll No", "Name"],
        ["101", "Alice"]
    ])
    with pytest.raises(RosterParseError) as exc_info:
        parse_roster_excel(data, filename="bad.xlsx")
    assert "No recognized Docker ID header found" in str(exc_info.value)

def test_duplicates_and_invalids():
    data = make_excel_bytes(rows=[
        ["Roll No", "Name", "Docker Hub ID"],
        [1, "Alice", "demo-student-01"],
        [2, "Bob", "https://hub.docker.com/u/demo-student-02/"],
        [3, "Charlie", "demo-student-01"],  # duplicate
        [4, "Dave", ""],  # missing
        [5, "Eve", "bad id!"],  # invalid
        [None, None, None],  # blank
        ["", "", "demo-student-06"],  # id only
        [7, "Grace", "demo-student-03"]
    ])
    roster = parse_roster_excel(data, filename="sample_messy.xlsx")
    # Expected valid students: demo-student-01, demo-student-02 (preserves casing), demo-student-06, demo-student-03 -> 4 students
    assert len(roster.students) == 4
    student_ids = [s.docker_id_lower for s in roster.students]
    assert student_ids == ["demo-student-01", "demo-student-02", "demo-student-06", "demo-student-03"]

    # Skipped list checks
    reasons = [s["reason"] for s in roster.skipped]
    assert any("duplicate of row 2" in r for r in reasons)
    assert any("missing Docker ID" in r for r in reasons)
    assert any("invalid characters" in r for r in reasons)

def test_roster_store_and_isolation():
    roster_store.clear()
    r1 = Roster(
        roster_id="roster_1",
        filename="r1.xlsx",
        students=[Student(roll="1", name="Alice", docker_id="alice")]
    )
    r2 = Roster(
        roster_id="roster_2",
        filename="r2.xlsx",
        students=[Student(roll="2", name="Bob", docker_id="bob")]
    )
    roster_store.add(r1)
    roster_store.add(r2)

    assert roster_store.get("roster_1") is not None
    assert roster_store.get("roster_2") is not None
    assert roster_store.get("non_existent") is None

    # Isolation check
    assert r1.find_student("alice") is not None
    assert r1.find_student("bob") is None
    assert r2.find_student("bob") is not None
    assert r2.find_student("alice") is None

def test_sample_roster_59_students():
    from pathlib import Path
    roster_path = Path("sample_roster.xlsx")
    if roster_path.is_file():
        content = roster_path.read_bytes()
        roster = parse_roster_excel(content, filename="sample_roster.xlsx")
        assert len(roster.students) == 59
