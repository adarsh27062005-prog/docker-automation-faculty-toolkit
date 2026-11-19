import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

logger = logging.getLogger(__name__)

MARKDOWN_LINK_PATTERN = re.compile(r"\[(.*?)\](?:\(.*\))?")

@dataclass
class Student:
    roll: str
    name: str
    docker_id: str

    @property
    def docker_id_lower(self) -> str:
        """Docker Hub namespaces are strictly lowercase."""
        return self.docker_id.lower()

    def to_dict(self) -> dict:
        return {
            "roll": self.roll,
            "name": self.name,
            "docker_id": self.docker_id,
        }

def parse_students(file_path: Union[str, Path], expected_count: Optional[int] = None) -> List[Student]:
    """
    Parses students from a markdown table file.
    Skips headers, separator rows, and non-student content.
    Trims all whitespace, extracts Docker ID from markdown link or plain text,
    and preserves original casing for display.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Student data file not found: {path}")

    students: List[Student] = []

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            # Only process table rows
            if not line_str.startswith("|"):
                continue

            # Split cells by pipe
            cells = [c.strip() for c in line_str.split("|")]
            # With leading and trailing pipes, cells has empty first and last elements
            if len(cells) < 4:
                continue

            # Content cells: index 1, 2, 3
            roll = cells[1]
            name = cells[2]
            docker_cell = cells[3]

            # Skip header or separator rows
            if not roll or roll.lower() == "roll number" or set(roll).issubset({"-", ":"}):
                continue
            if not name or name.lower() == "name" or set(name).issubset({"-", ":"}):
                continue

            # Extract Docker ID from markdown link [id](url)
            m = MARKDOWN_LINK_PATTERN.search(docker_cell)
            if m:
                docker_id = m.group(1).strip()
            else:
                docker_id = docker_cell.strip()

            students.append(Student(roll=roll, name=name, docker_id=docker_id))

    if expected_count is not None and len(students) != expected_count:
        logger.warning(
            "Expected %d students in %s, but found %d students.",
            expected_count,
            path,
            len(students),
        )

    return students
