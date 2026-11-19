from __future__ import annotations
import io
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from openpyxl import load_workbook
from app.config import settings

DOCKER_ID_REGEX = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*$")

HEADER_ALIASES = {
    "docker": {"docker id", "dockerid", "docker url", "docker username", "docker user", "docker hub id", "docker"},
    "roll": {"roll no", "roll number", "roll", "rollno", "reg no", "register number"},
    "name": {"name", "student name", "student"},
}

class RosterParseError(Exception):
    """Raised when an uploaded roster file cannot be parsed or has invalid headers."""
    pass

@dataclass
class Student:
    roll: str
    name: str
    docker_id: str
    docker_id_lower: str = ""

    def __post_init__(self):
        if not self.docker_id_lower and self.docker_id:
            self.docker_id_lower = self.docker_id.strip().lower()

    def to_dict(self) -> dict:
        return {
            "roll": self.roll,
            "name": self.name,
            "docker_id": self.docker_id,
        }

@dataclass
class Roster:
    roster_id: str
    filename: str
    students: List[Student]
    students_by_id: Dict[str, Student] = field(default_factory=dict)
    rows_read: int = 0
    students_loaded: int = 0
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.students_by_id and self.students:
            self.students_by_id = {s.docker_id_lower: s for s in self.students}
        if not self.students_loaded:
            self.students_loaded = len(self.students)

    def touch(self) -> None:
        self.last_accessed = time.time()

    def find_student(self, docker_id: str) -> Optional[Student]:
        return self.students_by_id.get(docker_id.strip().lower())

def normalize_header(h: Any) -> str:
    if h is None:
        return ""
    s = str(h).strip().lower()
    s = re.sub(r"[\s_.]+", " ", s)
    return s.strip()

def format_cell_text(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val).strip()

def normalize_docker_id(val: Any) -> Optional[str]:
    """
    Extracts and normalizes the lowercase Docker ID.
    Returns None if empty or invalid.
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None

    if "hub.docker.com" in s.lower():
        s_clean = s.split("?")[0].split("#")[0].rstrip("/")
        parts = [p for p in s_clean.split("/") if p]
        if not parts:
            return None
        s = parts[-1].strip()
        if not s:
            return None

    s_lower = s.lower()
    if not DOCKER_ID_REGEX.match(s_lower):
        return None

    return s_lower

def parse_docker_id_cell(val: Any) -> Tuple[str, str]:
    """
    Extracts display ID and lowercase ID from cell value.
    Raises ValueError with skip reason on failure.
    """
    if val is None:
        raise ValueError("missing Docker ID")
    s = str(val).strip()
    if not s:
        raise ValueError("missing Docker ID")

    if "hub.docker.com" in s.lower():
        s_clean = s.split("?")[0].split("#")[0].rstrip("/")
        parts = [p for p in s_clean.split("/") if p]
        if not parts:
            raise ValueError("invalid characters")
        s = parts[-1].strip()
        if not s:
            raise ValueError("invalid characters")

    s_lower = s.lower()
    if not DOCKER_ID_REGEX.match(s_lower):
        raise ValueError("invalid characters")

    return s, s_lower

def parse_roster_excel(file_bytes: bytes, filename: str) -> Roster:
    """
    Parses an uploaded .xlsx file in memory.
    Returns a validated Roster instance.
    Raises RosterParseError on header issues or invalid content.
    """
    if not filename.lower().endswith(".xlsx"):
        raise RosterParseError(f"Invalid file format for '{filename}'. Only .xlsx Excel files are supported.")

    try:
        wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as exc:
        raise RosterParseError(f"Could not parse Excel workbook: {str(exc)}")

    # Sheet choice: "Students" if exists, otherwise first sheet
    sheet_name = "Students" if "Students" in wb.sheetnames else wb.sheetnames[0]
    ws = wb[sheet_name]

    # Also load formula view to read formula-looking strings (e.g. formula injection test cases) if data_only has None
    try:
        wb_f = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=False)
        ws_f = wb_f[sheet_name]
        rows_iter_f = ws_f.iter_rows(values_only=True)
    except Exception:
        rows_iter_f = None

    # Header detection in first 10 rows
    docker_col: Optional[int] = None
    roll_col: Optional[int] = None
    name_col: Optional[int] = None
    header_row_idx: Optional[int] = None

    all_scanned_headers: List[str] = []

    # Read rows
    rows_iter = ws.iter_rows(values_only=True)
    row_list: List[Tuple[Any, ...]] = []
    total_count = 0
    for r in rows_iter:
        total_count += 1
        row_list.append(r)
        if total_count >= 5000:
            break

    row_list_f: List[Tuple[Any, ...]] = []
    if rows_iter_f:
        count_f = 0
        for rf in rows_iter_f:
            count_f += 1
            row_list_f.append(rf)
            if count_f >= 5000:
                break

    # Scan first 10 rows for header containing Docker ID column
    for r_idx in range(min(10, len(row_list))):
        current_row = row_list[r_idx]
        if not current_row:
            continue

        temp_docker_col = None
        temp_roll_col = None
        temp_name_col = None

        row_headers_found: List[str] = []

        for c_idx, cell_val in enumerate(current_row):
            norm = normalize_header(cell_val)
            if norm:
                row_headers_found.append(str(cell_val).strip())
            if norm in HEADER_ALIASES["docker"]:
                temp_docker_col = c_idx
            elif norm in HEADER_ALIASES["roll"]:
                temp_roll_col = c_idx
            elif norm in HEADER_ALIASES["name"]:
                temp_name_col = c_idx

        if temp_docker_col is not None:
            docker_col = temp_docker_col
            roll_col = temp_roll_col
            name_col = temp_name_col
            header_row_idx = r_idx + 1  # 1-based row index
            break
        else:
            all_scanned_headers.extend(row_headers_found)

    if docker_col is None or header_row_idx is None:
        headers_str = ", ".join(f"'{h}'" for h in all_scanned_headers[:15]) if all_scanned_headers else "None"
        raise RosterParseError(
            f"No recognized Docker ID header found in sheet '{sheet_name}'. "
            f"Found headers: [{headers_str}]. "
            f"Please ensure your file includes a 'Docker ID' column or use the downloadable template."
        )

    students: List[Student] = []
    skipped: List[Dict[str, Any]] = []
    warnings: List[str] = []
    seen_ids: Dict[str, int] = {}  # docker_id_lower -> 1-based row index

    data_start_idx = header_row_idx  # 0-based index in row_list = header_row_idx
    rows_read = 0

    for idx in range(data_start_idx, len(row_list)):
        rows_read += 1
        row = row_list[idx]
        excel_row_num = idx + 1  # 1-based Excel row number

        if not row:
            continue

        # Extract values
        row_f = row_list_f[idx] if idx < len(row_list_f) else None
        raw_docker = row[docker_col] if docker_col < len(row) else None
        if raw_docker is None and row_f and docker_col < len(row_f):
            raw_docker = row_f[docker_col]

        raw_roll = row[roll_col] if (roll_col is not None and roll_col < len(row)) else None
        if raw_roll is None and row_f and roll_col is not None and roll_col < len(row_f):
            raw_roll = row_f[roll_col]

        raw_name = row[name_col] if (name_col is not None and name_col < len(row)) else None
        if raw_name is None and row_f and name_col is not None and name_col < len(row_f):
            raw_name = row_f[name_col]

        roll_text = format_cell_text(raw_roll)
        name_text = format_cell_text(raw_name)

        # Check fully blank row
        if raw_docker is None and not roll_text and not name_text:
            if all(c is None or str(c).strip() == "" for c in row):
                continue

        # Check Docker ID cell
        raw_docker_str = str(raw_docker).strip() if raw_docker is not None else ""
        if not raw_docker_str:
            skipped.append({
                "row_index": excel_row_num,
                "raw_docker_cell": "",
                "reason": "missing Docker ID"
            })
            continue

        try:
            display_id, lower_id = parse_docker_id_cell(raw_docker)
        except ValueError as e:
            skipped.append({
                "row_index": excel_row_num,
                "raw_docker_cell": raw_docker_str,
                "reason": str(e)
            })
            continue

        # Check duplicate
        if lower_id in seen_ids:
            first_row = seen_ids[lower_id]
            skipped.append({
                "row_index": excel_row_num,
                "raw_docker_cell": display_id,
                "reason": f"duplicate of row {first_row}"
            })
            continue

        seen_ids[lower_id] = excel_row_num
        students.append(Student(
            roll=roll_text,
            name=name_text,
            docker_id=display_id,
            docker_id_lower=lower_id,
        ))

    roster_id = secrets.token_urlsafe(16)
    return Roster(
        roster_id=roster_id,
        filename=filename,
        students=students,
        rows_read=rows_read,
        students_loaded=len(students),
        skipped=skipped,
        warnings=warnings,
    )

class RosterStore:
    def __init__(self, max_rosters: int = settings.MAX_ROSTERS, ttl_hours: int = settings.ROSTER_TTL_HOURS):
        self.max_rosters = max_rosters
        self.ttl_seconds = ttl_hours * 3600
        self._rosters: Dict[str, Roster] = {}

    def _purge_expired(self) -> None:
        now = time.time()
        expired_keys = [
            k for k, r in self._rosters.items()
            if now - r.created_at > self.ttl_seconds
        ]
        for k in expired_keys:
            del self._rosters[k]

    def _evict_lru(self) -> None:
        if len(self._rosters) >= self.max_rosters:
            sorted_keys = sorted(self._rosters.keys(), key=lambda k: self._rosters[k].last_accessed)
            num_to_evict = len(self._rosters) - self.max_rosters + 1
            for k in sorted_keys[:num_to_evict]:
                del self._rosters[k]

    def add(self, roster: Roster) -> None:
        self._purge_expired()
        self._evict_lru()
        self._rosters[roster.roster_id] = roster

    def get(self, roster_id: str) -> Optional[Roster]:
        self._purge_expired()
        roster = self._rosters.get(roster_id)
        if roster:
            roster.touch()
        return roster

    def clear(self) -> None:
        self._rosters.clear()

roster_store = RosterStore()
