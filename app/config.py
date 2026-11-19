import os
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

class Settings:
    CACHE_TTL_SECONDS: int = int(os.environ.get("CACHE_TTL_SECONDS", "900"))
    SUBMISSION_DEADLINE_STR: Optional[str] = os.environ.get("SUBMISSION_DEADLINE")
    DISPLAY_TZ_NAME: str = os.environ.get("DISPLAY_TZ", "Asia/Kolkata")
    GITHUB_TOKEN: Optional[str] = os.environ.get("GITHUB_TOKEN")
    OVERRIDES_FILE: Path = Path(os.environ.get("OVERRIDES_FILE", "overrides.csv"))
    SUBMISSION_DATE_BASIS: str = os.environ.get("SUBMISSION_DATE_BASIS", "last_push").strip().lower()

    # Upgrade 2: Roster upload & Job limits
    MAX_UPLOAD_MB: int = int(os.environ.get("MAX_UPLOAD_MB", "2"))
    MAX_STUDENTS: int = int(os.environ.get("MAX_STUDENTS", "500"))
    ROSTER_TTL_HOURS: int = int(os.environ.get("ROSTER_TTL_HOURS", "12"))
    MAX_ROSTERS: int = int(os.environ.get("MAX_ROSTERS", "50"))
    JOB_TTL_HOURS: int = int(os.environ.get("JOB_TTL_HOURS", "2"))
    GITHUB_README_LOOKUP: bool = os.environ.get("GITHUB_README_LOOKUP", "false").strip().lower() in ("true", "1", "yes")

    @property
    def display_tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.DISPLAY_TZ_NAME)
        except Exception:
            return ZoneInfo("Asia/Kolkata")

    @property
    def submission_deadline(self) -> Optional[datetime]:
        if not self.SUBMISSION_DEADLINE_STR or not self.SUBMISSION_DEADLINE_STR.strip():
            return None
        deadline_str = self.SUBMISSION_DEADLINE_STR.strip()
        try:
            if deadline_str.endswith("Z"):
                deadline_str = deadline_str[:-1] + "+00:00"
            dt = datetime.fromisoformat(deadline_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            return dt
        except Exception:
            return None

settings = Settings()
