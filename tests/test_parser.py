import logging
import pytest
from pathlib import Path
from app.parser import parse_students, Student

DOCKER_ID_MD = Path("docker_id.md")

def test_parse_students_count():
    """Acceptance criterion: parses exactly 59 students from docker_id.md."""
    students = parse_students(DOCKER_ID_MD)
    assert len(students) == 59, f"Expected exactly 59 students, but got {len(students)}"

def test_no_headers_or_separators():
    """Ensure header strings and separator rows are completely excluded."""
    students = parse_students(DOCKER_ID_MD)
    for s in students:
        assert "roll number" not in s.roll.lower()
        assert "---" not in s.roll
        assert "name" not in s.name.lower()
        assert "---" not in s.name
        assert "docker id" not in s.docker_id.lower()
        assert "---" not in s.docker_id

def test_first_and_last_student():
    """Verify first and last student details against docker_id.md."""
    students = parse_students(DOCKER_ID_MD)
    first = students[0]
    assert first.roll == "DEMO.U4CYS23001"
    assert first.name == "Demo Student 01"
    assert first.docker_id == "demo-student-01"
    assert first.docker_id_lower == "demo-student-01"

    last = students[-1]
    assert last.roll == "DEMO.U4CYS23063"
    assert last.name == "Demo Student 59"
    assert last.docker_id == "demo-student-59"
    assert last.docker_id_lower == "demo-student-59"

def test_mixed_case_docker_ids():
    """Ensure mixed-case Docker IDs preserve original case for display but lowercase for API."""
    students = parse_students(DOCKER_ID_MD)
    student_map = {s.roll: s for s in students}

    # Test specified mixed-case examples from IMPLEMENTATION_PLAN.md
    mixed_a = student_map["DEMO.U4CYS23041"]
    assert mixed_a.docker_id == "Demo-Student-37"
    assert mixed_a.docker_id_lower == "demo-student-37"

    mixed_b = student_map["DEMO.U4CYS23049"]
    assert mixed_b.docker_id == "Demo-Student-45"
    assert mixed_b.docker_id_lower == "demo-student-45"

    mixed_c = student_map["DEMO.U4CYS23061"]
    assert mixed_c.docker_id == "Demo-Student-57"
    assert mixed_c.docker_id_lower == "demo-student-57"

    mixed_d = student_map["DEMO.U4CYS23058"]
    assert mixed_d.docker_id == "Demo-Student-54"
    assert mixed_d.docker_id_lower == "demo-student-54"

    mixed_e = student_map["DEMO.U4CYS23055"]
    assert mixed_e.docker_id == "Demo-Student-51"
    assert mixed_e.docker_id_lower == "demo-student-51"

def test_long_name_not_truncated():
    """Names must not be truncated."""
    students = parse_students(DOCKER_ID_MD)
    student_map = {s.roll: s for s in students}
    mixed_d = student_map["DEMO.U4CYS23058"]
    assert mixed_d.name == "Demo Student 54 With Four Extra Words Here"

def test_non_continuous_roll_numbers():
    """Roll numbers in batch 23CYS have gaps (e.g., 23004 to 23007)."""
    students = parse_students(DOCKER_ID_MD)
    rolls = [s.roll for s in students]
    assert "DEMO.U4CYS23004" in rolls
    assert "DEMO.U4CYS23005" not in rolls
    assert "DEMO.U4CYS23006" not in rolls
    assert "DEMO.U4CYS23007" in rolls

def test_warning_logged_if_count_not_59(tmp_path, caplog):
    """Parser must log a warning if count != 59, and NOT raise an error."""
    sample_file = tmp_path / "sample.md"
    sample_file.write_text(
        "| Roll Number | Name | Docker ID |\n"
        "|:---:|:---:|:---:|\n"
        "| DEMO.U4CYS23001 | Test Student | [testuser](https://hub.docker.com/r/testuser) |\n",
        encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING):
        result = parse_students(sample_file, expected_count=59)
    assert len(result) == 1
    assert any("Expected 59 students" in record.message for record in caplog.records)

def test_file_not_found():
    with pytest.raises(FileNotFoundError):
        parse_students("nonexistent_file_xyz.md")
