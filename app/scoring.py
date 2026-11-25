"""Domain-agnostic scoring of one Docker Hub repository (0 to 100).

Pure functions: `score_repo(facts, rubric)` looks only at what Docker Hub shows
(the tag's step list, the README, tags and sizes) and never at what the app
inside does. Each check reports pass / partial / fail with evidence, so faculty
can see why points were given. The rubric (points, on/off, deadline, limits)
is editable per roster; see docs/plans/repo-scoring.md.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.config import settings
from app.layers import BaseSplit, Step

# ---------------------------------------------------------------------------
# Rubric
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckDef:
    id: str
    area: str
    label: str
    points: int
    help: str


CHECKS: List[CheckDef] = [
    CheckDef("S1", "Submission", "Image pushed", 10,
             "The selected tag has a pushed image. Without one the repo scores 0, even if this check is switched off."),
    CheckDef("S2", "Submission", "Pushed on time", 10,
             "Pushed on or before the deadline (the repo's own deadline, else the class deadline). Skipped when no deadline is set."),
    CheckDef("D1", "Documentation", "Short description", 4,
             "The repository's short description on Docker Hub is filled in."),
    CheckDef("D2", "Documentation", "README present", 6,
             "Full points for at least 300 characters of overview text, half for 50 to 299."),
    CheckDef("D3", "Documentation", "README explains how to run it", 6,
             "The README contains a docker run, docker pull or docker compose command."),
    CheckDef("D4", "Documentation", "README is structured", 4,
             "At least two headings or a code block (half points for a single heading)."),
    CheckDef("B1", "Build practices", "Start command", 5,
             "CMD or ENTRYPOINT in the student's own steps (half points if only inherited from the base image)."),
    CheckDef("B2", "Build practices", "Runs as non-root", 7,
             "The final image's user is not root (the last USER instruction)."),
    CheckDef("B3", "Build practices", "Health check", 5,
             "The final image has a HEALTHCHECK."),
    CheckDef("B4", "Build practices", "Port declared", 3,
             "EXPOSE in the student's own steps (half points if only inherited)."),
    CheckDef("B5", "Build practices", "Package caches cleaned", 6,
             "Steps that install packages also clean up (apt lists removed, apk --no-cache, pip --no-cache-dir, npm/yarn cache clean). Skipped if nothing is installed."),
    CheckDef("B6", "Build practices", "RUN steps combined", 4,
             "No run of 3 or more consecutive RUN steps (half points for exactly 3). Skipped without RUN steps."),
    CheckDef("B7", "Build practices", "Working directory set", 3,
             "WORKDIR in the student's own steps (half points if only inherited)."),
    CheckDef("B8", "Build practices", "Image labels", 3,
             "LABEL metadata (maintainer, version, org.opencontainers.*) in the student's own steps."),
    CheckDef("B9", "Build practices", "COPY rather than ADD", 4,
             "Local files are copied with COPY; ADD only for URLs or archives."),
    CheckDef("V1", "Versioning", "Version tag", 5,
             "A version tag besides latest, e.g. v1 or 1.0.2 (half points for another named tag)."),
    CheckDef("V2", "Versioning", "Pushed more than once", 3,
             "Tags were pushed at two or more different times."),
    CheckDef("V3", "Versioning", "Multi-architecture", 2,
             "The selected tag is available for more than one platform."),
    CheckDef("L1", "Lean image", "Minimal base image", 5,
             "Built on a small base: alpine, slim, distroless, busybox or scratch."),
    CheckDef("L2", "Lean image", "Image size", 5,
             "Compressed size within the size limit (half points up to twice the limit)."),
]
CHECK_BY_ID: Dict[str, CheckDef] = {c.id: c for c in CHECKS}
AREAS = ["Submission", "Documentation", "Build practices", "Versioning", "Lean image"]
BUILD_IDS = {c.id for c in CHECKS if c.area == "Build practices"}

FLAGS: Dict[str, str] = {
    "F1": "Same image as another student",
    "F2": "Same image in two of this student's repos",
    "F3": "No own build steps (re-tag of another image)",
    "F4": "Pushed after the deadline",
    "F5": "Needs re-check (Docker Hub error)",
    "F6": "Secret-like value in the image",
}

DEFAULT_SIZE_LIMIT_MB = 500
DEFAULT_SECRET_PENALTY = 10


def parse_deadline(value: Optional[str]) -> Optional[datetime]:
    """ISO 8601 date-time; a value without an offset is in the display time zone (IST)."""
    if value is None or not str(value).strip():
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"Deadline '{value}' is not a valid date and time.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=settings.display_tz)
    return dt


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        s = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo("UTC"))
    except ValueError:
        return None


def round1(x: float) -> float:
    """Round to 1 decimal, halves up (66.25 -> 66.3) like a teacher would, and like the demo's JS."""
    return math.floor(x * 10 + 0.5) / 10


def fmt_local(dt: Optional[datetime]) -> str:
    if dt is None:
        return "-"
    label = "IST" if settings.DISPLAY_TZ_NAME == "Asia/Kolkata" else settings.DISPLAY_TZ_NAME
    return dt.astimezone(settings.display_tz).strftime("%Y-%m-%d %H:%M") + f" {label}"


@dataclass
class Rubric:
    checks: Dict[str, Dict[str, float]] = field(default_factory=dict)
    deadline: Optional[str] = None            # ISO 8601 with offset
    size_limit_mb: int = DEFAULT_SIZE_LIMIT_MB
    late_penalty_percent: float = 0.0
    secret_penalty: float = DEFAULT_SECRET_PENALTY

    @classmethod
    def default(cls) -> "Rubric":
        return cls(checks={c.id: {"enabled": True, "points": float(c.points)} for c in CHECKS})

    def setting(self, check_id: str) -> Tuple[bool, float]:
        s = self.checks.get(check_id) or {}
        return bool(s.get("enabled", True)), float(s.get("points", CHECK_BY_ID[check_id].points))

    @property
    def deadline_dt(self) -> Optional[datetime]:
        return parse_deadline(self.deadline)

    def to_dict(self) -> dict:
        return {
            "checks": {cid: {"enabled": self.setting(cid)[0], "points": self.setting(cid)[1]} for cid in CHECK_BY_ID},
            "deadline": self.deadline,
            "size_limit_mb": self.size_limit_mb,
            "late_penalty_percent": self.late_penalty_percent,
            "secret_penalty": self.secret_penalty,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Rubric":
        """Validated rubric from user input. Raises ValueError with a readable message."""
        if not isinstance(data, dict):
            raise ValueError("Rubric must be an object.")
        rubric = cls.default()
        raw_checks = data.get("checks") or {}
        if not isinstance(raw_checks, dict):
            raise ValueError("'checks' must be an object.")
        for cid, s in raw_checks.items():
            if cid not in CHECK_BY_ID:
                raise ValueError(f"Unknown check '{cid}'.")
            if not isinstance(s, dict):
                raise ValueError(f"Settings for {cid} must be an object.")
            enabled = s.get("enabled", True)
            points = _number(s.get("points", CHECK_BY_ID[cid].points), f"Points for {cid}", 0, 100)
            rubric.checks[cid] = {"enabled": bool(enabled), "points": points}
        if not any(rubric.setting(cid)[0] and rubric.setting(cid)[1] > 0 for cid in CHECK_BY_ID):
            raise ValueError("Switch on at least one check with more than 0 points.")
        deadline = data.get("deadline")
        dt = parse_deadline(deadline)
        rubric.deadline = dt.isoformat() if dt else None
        rubric.size_limit_mb = int(_number(data.get("size_limit_mb", DEFAULT_SIZE_LIMIT_MB), "Size limit", 1, 100000))
        rubric.late_penalty_percent = _number(data.get("late_penalty_percent", 0), "Late penalty", 0, 100)
        rubric.secret_penalty = _number(data.get("secret_penalty", DEFAULT_SECRET_PENALTY), "Secret penalty", 0, 100)
        return rubric


def _number(value, label: str, lo: float, hi: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.")
    if n != n or n < lo or n > hi:  # NaN or out of range
        raise ValueError(f"{label} must be between {lo:g} and {hi:g}.")
    return n


def catalogue() -> List[dict]:
    return [{"id": c.id, "area": c.area, "label": c.label, "default_points": c.points, "help": c.help} for c in CHECKS]


# ---------------------------------------------------------------------------
# Facts and results
# ---------------------------------------------------------------------------


@dataclass
class RepoFacts:
    """Everything Docker Hub told us about one repo + tag. Kept in memory so a
    rubric change can rescore without fetching again."""
    repo: str
    tag: Optional[str]
    description: str = ""
    readme: str = ""
    tags: List[dict] = field(default_factory=list)       # [{"name", "pushed"}]
    platforms: List[str] = field(default_factory=list)
    platform: Optional[str] = None
    image_size: int = 0
    image_digest: Optional[str] = None
    steps: List[Step] = field(default_factory=list)
    split: Optional[BaseSplit] = None
    pushed_at: Optional[str] = None                      # selected tag's push time (ISO)
    has_image: bool = False
    error: str = ""                                      # non-empty: Docker Hub failed, no score
    warning: str = ""                                    # scored, but some data was incomplete


@dataclass
class CheckResult:
    id: str
    area: str
    label: str
    max_points: float
    fraction: float
    status: str        # pass | partial | fail | skip | off
    evidence: str

    @property
    def earned(self) -> float:
        return round(self.max_points * self.fraction, 2) if self.status not in ("skip", "off") else 0.0

    def to_dict(self) -> dict:
        return {"id": self.id, "area": self.area, "label": self.label, "max_points": self.max_points,
                "earned": self.earned, "status": self.status, "evidence": self.evidence}


@dataclass
class RepoResult:
    repo: str
    tag: Optional[str]
    status: str                      # scored | no_image | needs_recheck
    score: Optional[float]
    checks: List[CheckResult] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    penalties: List[str] = field(default_factory=list)
    base: str = ""
    confidence: str = ""
    own_steps: int = 0
    total_steps: int = 0
    platform: Optional[str] = None
    pushed_at: Optional[str] = None
    deadline: Optional[str] = None
    note: str = ""
    fingerprint: Optional[Tuple[str, ...]] = None

    def to_dict(self) -> dict:
        return {
            "repo": self.repo, "tag": self.tag, "status": self.status, "score": self.score,
            "checks": [c.to_dict() for c in self.checks], "flags": list(self.flags),
            "penalties": list(self.penalties), "base": self.base, "confidence": self.confidence,
            "own_steps": self.own_steps, "total_steps": self.total_steps, "platform": self.platform,
            "pushed_at": self.pushed_at, "deadline": self.deadline, "note": self.note,
        }


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------

def _num(i: int) -> str:
    return f"step {i + 1}"


def _short(ins: str, n: int = 70) -> str:
    s = ins.replace(" # buildkit", "")
    return s if len(s) <= n else s[: n - 1] + "…"


def _last(steps: Sequence[Tuple[int, Step]], kinds: Sequence[str]) -> Optional[Tuple[int, Step]]:
    for i, s in reversed(list(steps)):
        if s.kind in kinds:
            return i, s
    return None


INSTALLERS = [
    ("apt", re.compile(r"\bapt(?:-get)?\s+(?:-\S+\s+)*install\b"),
     [re.compile(r"rm\s+-[a-z]*r[a-z]*f?\s+/var/lib/apt/lists"), re.compile(r"apt(?:-get)?\s+clean")]),
    ("apk", re.compile(r"\bapk\s+(?:-\S+\s+)*add\b"),
     [re.compile(r"--no-cache\b"), re.compile(r"rm\s+-[a-z]*r[a-z]*f?\s+/var/cache/apk")]),
    ("yum/dnf", re.compile(r"\b(?:yum|dnf|microdnf)\s+(?:-\S+\s+)*install\b"),
     [re.compile(r"\b(?:yum|dnf|microdnf)\s+clean\s+all"), re.compile(r"rm\s+-[a-z]*r[a-z]*f?\s+/var/cache/(?:yum|dnf)")]),
    ("pip", re.compile(r"\bpip3?\s+(?:-\S+\s+)*install\b"),
     [re.compile(r"--no-cache-dir\b"), re.compile(r"rm\s+-[a-z]*r[a-z]*f?\s+\S*\.cache/pip")]),
    ("npm", re.compile(r"\bnpm\s+(?:install|i|ci)\b"),
     [re.compile(r"npm\s+cache\s+clean"), re.compile(r"rm\s+-[a-z]*r[a-z]*f?\s+\S*\.npm\b")]),
    ("yarn", re.compile(r"\byarn\s+(?:install|add)\b|\byarn\s*(?:&&|$)"),
     [re.compile(r"yarn\s+cache\s+clean")]),
]

SECRET_ASSIGN = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)[A-Za-z0-9_]*)"
    r"=(\"[^\"]*\"|'[^']*'|\S*)",
    re.IGNORECASE,
)
AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
PLACEHOLDERS = {
    "", "none", "null", "nil", "**none**", "changeme", "change_me", "change-me", "example", "dummy",
    "placeholder", "secret", "password", "token", "false", "true", "default", "test", "unset",
}


def _is_placeholder(value: str) -> bool:
    v = value.strip().strip("\"'").strip()
    low = v.lower()
    if low in PLACEHOLDERS or len(v) < 4:
        return True
    if v.startswith("$") or (v.startswith("<") and v.endswith(">")) or (v.startswith("{{") and v.endswith("}}")):
        return True
    return bool(re.fullmatch(r"[*xX.\-_#]+", v))


def find_secrets(steps: Sequence[Tuple[int, Step]]) -> List[str]:
    """Names of secret-like values in ENV/ARG/RUN steps. Never returns the values."""
    found: List[str] = []
    for i, s in steps:
        if s.kind not in ("ENV", "ARG", "RUN"):
            continue
        for m in SECRET_ASSIGN.finditer(s.instruction):
            if not _is_placeholder(m.group(2)):
                found.append(f"{m.group(1)} ({_num(i)})")
        if AWS_KEY.search(s.instruction):
            found.append(f"AWS access key ({_num(i)})")
        if PRIVATE_KEY.search(s.instruction):
            found.append(f"private key ({_num(i)})")
    return list(dict.fromkeys(found))


VERSION_TAG = re.compile(r"^v?\d+(?:\.\d+){0,3}(?:[-_.+][0-9A-Za-z.\-]+)?$")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.M)
SETEXT = re.compile(r"^\S.*\n\s{0,3}(?:=+|-{3,})\s*$", re.M)
RUN_CMD = re.compile(r"docker(?:-compose|\s+compose|\s+run|\s+pull|\s+container\s+run)\b", re.I)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_repo(facts: RepoFacts, rubric: Rubric, deadline_override: Optional[str] = None) -> RepoResult:
    deadline_dt = parse_deadline(deadline_override) if deadline_override else rubric.deadline_dt
    result = RepoResult(repo=facts.repo, tag=facts.tag, status="scored", score=None,
                        platform=facts.platform, pushed_at=facts.pushed_at,
                        deadline=deadline_dt.isoformat() if deadline_dt else None)
    if facts.error:
        result.status = "needs_recheck"
        result.flags = ["F5"]
        result.note = facts.error
        return result
    result.note = facts.warning

    steps = list(enumerate(facts.steps))
    split = facts.split
    estimated = split is None or split.confidence == "low"
    own_start = 0 if estimated else split.own_start
    own = steps[own_start:]
    inherited = steps[:own_start]
    result.base = split.base if split else ""
    result.confidence = split.confidence if split else ""
    result.total_steps = len(steps)
    result.own_steps = len(own)
    est = " (estimated: base image not identified)" if (estimated and facts.has_image) else ""
    retag = facts.has_image and not estimated and not own
    layer_steps = [s for _, s in own if s.kind in ("RUN", "COPY", "ADD")]

    checks: List[CheckResult] = []

    def add(cid: str, fraction: float, evidence: str, status: Optional[str] = None) -> None:
        enabled, points = rubric.setting(cid)
        d = CHECK_BY_ID[cid]
        if status is None:
            status = "pass" if fraction >= 1 else ("partial" if fraction > 0 else "fail")
        if not enabled:
            status = "off"
        checks.append(CheckResult(cid, d.area, d.label, points, max(0.0, min(1.0, fraction)), status, evidence))

    # Submission -----------------------------------------------------------
    pushed = parse_iso(facts.pushed_at)
    if facts.has_image:
        add("S1", 1, f"Tag '{facts.tag}' has a pushed image ({facts.platform or 'platform unknown'}), pushed {fmt_local(pushed)}.")
    else:
        add("S1", 0, f"Tag '{facts.tag}' has no pushed image." if facts.tag else "No tag with a pushed image.")
    late = False
    if deadline_dt is None:
        add("S2", 0, "No deadline set.", status="skip")
    elif pushed is None:
        add("S2", 0, f"No push time; deadline {fmt_local(deadline_dt)}.")
    elif pushed <= deadline_dt:
        add("S2", 1, f"Pushed {fmt_local(pushed)}, deadline {fmt_local(deadline_dt)}.")
    else:
        late = True
        add("S2", 0, f"Pushed {fmt_local(pushed)}, after the deadline {fmt_local(deadline_dt)}.")

    # Documentation --------------------------------------------------------
    desc = (facts.description or "").strip()
    add("D1", 1 if desc else 0, f"Description: \"{_short(desc, 80)}\"" if desc else "No short description.")
    readme = (facts.readme or "").strip()
    n = len(readme)
    if n >= 300:
        add("D2", 1, f"README has {n} characters.")
    elif n >= 50:
        add("D2", 0.5, f"README is short ({n} characters; 300 for full points).")
    else:
        add("D2", 0, "No README." if n == 0 else f"README is almost empty ({n} characters).")
    m = RUN_CMD.search(readme)
    add("D3", 1 if m else 0, f"README shows '{m.group(0)}'." if m else "README has no docker run / pull / compose command.")
    headings = len(HEADING.findall(readme)) + len(SETEXT.findall(readme))
    code = "```" in readme or "~~~" in readme
    if headings >= 2 or code:
        add("D4", 1, f"{headings} heading(s)" + (", code block" if code else "") + ".")
    elif headings == 1:
        add("D4", 0.5, "1 heading, no code block.")
    else:
        add("D4", 0, "No headings or code blocks.")

    # Build practices ------------------------------------------------------
    if not facts.has_image:
        for cid in ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9"):
            add(cid, 0, "No image to inspect.", status="skip")
    elif retag:
        for cid in ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9"):
            add(cid, 0, f"No own build steps: the image is a re-tag of {result.base}.")
    else:
        def own_or_inherited(cid: str, kinds: Sequence[str], what: str) -> None:
            mine = _last(own, kinds)
            if mine:
                add(cid, 1, f"{_short(mine[1].instruction)} ({_num(mine[0])}){est}.")
                return
            base_hit = _last(inherited, kinds)
            if base_hit:
                add(cid, 0.5, f"Only inherited from the base: {_short(base_hit[1].instruction)} ({_num(base_hit[0])}).")
            else:
                add(cid, 0, f"No {what}{est}.")

        own_or_inherited("B1", ("CMD", "ENTRYPOINT"), "CMD or ENTRYPOINT")

        user = _last(steps, ("USER",))
        if user is None:
            add("B2", 0, "No USER instruction: runs as root.")
        else:
            name = user[1].instruction.split(" ", 1)[1].strip() if " " in user[1].instruction else ""
            who = "own step" if user[0] >= own_start else "inherited"
            if name.split(":")[0] in ("root", "0", ""):
                add("B2", 0, f"USER {name} ({_num(user[0])}, {who}): runs as root.")
            else:
                add("B2", 1, f"USER {name} ({_num(user[0])}, {who}).")

        hc = _last(steps, ("HEALTHCHECK",))
        if hc and not re.search(r"\bNONE\b", hc[1].instruction):
            who = "own step" if hc[0] >= own_start else "inherited"
            add("B3", 1, f"{_short(hc[1].instruction, 60)} ({_num(hc[0])}, {who}).")
        else:
            add("B3", 0, "No HEALTHCHECK.")

        own_or_inherited("B4", ("EXPOSE",), "EXPOSE")

        installs, cleaned, misses = 0, 0, []
        pip_env = any(re.search(r"\bPIP_NO_CACHE_DIR=(?!0\b|false\b|off\b)\S+", s.instruction, re.I) for _, s in steps if s.kind == "ENV")
        for i, s in own:
            if s.kind != "RUN":
                continue
            for name, inst, cleans in INSTALLERS:
                if inst.search(s.instruction):
                    installs += 1
                    ok = any(c.search(s.instruction) for c in cleans) or (name == "pip" and pip_env)
                    cleaned += 1 if ok else 0
                    if not ok:
                        misses.append(f"{name} in {_num(i)}")
        if installs == 0:
            add("B5", 0, "No package installs in own steps.", status="skip")
        else:
            text = f"{cleaned} of {installs} install step(s) clean up"
            add("B5", cleaned / installs, text + (f"; not cleaned: {', '.join(misses[:4])}." if misses else ".") + est)

        runs = [i for i, s in own if s.kind == "RUN"]
        if not runs:
            add("B6", 0, "No RUN steps.", status="skip")
        else:
            longest, streak, prev = 0, 0, None
            for i, s in own:
                streak = streak + 1 if s.kind == "RUN" and prev == "RUN" else (1 if s.kind == "RUN" else 0)
                longest = max(longest, streak)
                prev = s.kind
            frac = 1 if longest <= 2 else (0.5 if longest == 3 else 0)
            add("B6", frac, f"{len(runs)} RUN step(s); longest run of consecutive RUN steps: {longest}{est}.")

        own_or_inherited("B7", ("WORKDIR",), "WORKDIR")

        label = _last(own, ("LABEL",))
        add("B8", 1 if label else 0, f"{_short(label[1].instruction)} ({_num(label[0])})." if label else f"No LABEL in own steps{est}.")

        adds = [(i, s) for i, s in own if s.kind == "ADD"]
        bad = []
        for i, s in adds:
            body = s.instruction.replace(" # buildkit", "")
            if re.search(r"https?://|git@", body) or re.search(r"\.(?:tar|tgz|txz|tbz2?)(?:\.(?:gz|xz|bz2))?\b", body):
                continue
            bad.append(_num(i))
        if bad:
            add("B9", 0, f"ADD used for local files ({', '.join(bad[:4])}); use COPY.")
        else:
            add("B9", 1, "No ADD for local files." if not adds else "ADD only for URLs/archives.")

    # Versioning -----------------------------------------------------------
    names = [t.get("name", "") for t in facts.tags]
    versions = [n for n in names if n != "latest" and VERSION_TAG.match(n)]
    others = [n for n in names if n != "latest" and n not in versions]
    if versions:
        add("V1", 1, f"Version tag(s): {', '.join(versions[:4])}.")
    elif others:
        add("V1", 0.5, f"Named tag(s) but no version number: {', '.join(others[:4])}.")
    else:
        add("V1", 0, "Only 'latest'." if names else "No tags.")
    times = {t.get("pushed", "")[:16] for t in facts.tags if t.get("pushed")}
    add("V2", 1 if len(times) >= 2 else 0,
        f"Pushes at {len(times)} different times." if len(times) >= 2 else "Only one push visible.")
    if not facts.has_image:
        add("V3", 0, "No image.", status="skip")
    else:
        add("V3", 1 if len(facts.platforms) >= 2 else 0, f"Platforms: {', '.join(facts.platforms) or 'unknown'}.")

    # Lean image -----------------------------------------------------------
    if not facts.has_image:
        add("L1", 0, "No image.", status="skip")
        add("L2", 0, "No image.", status="skip")
    else:
        small = re.search(r"alpine|slim|distroless|busybox|scratch", result.base or "", re.I)
        add("L1", 1 if small else 0, f"Base: {result.base or 'unknown'}{est}.")
        mb = facts.image_size / (1024 * 1024)
        limit = rubric.size_limit_mb
        frac = 1 if mb <= limit else (0.5 if mb <= 2 * limit else 0)
        add("L2", frac, f"{mb:.0f} MB compressed (limit {limit} MB).")

    result.checks = checks

    # Flags and penalties --------------------------------------------------
    if facts.has_image and not estimated and not layer_steps:
        result.flags.append("F3")
    if late:
        result.flags.append("F4")
    secrets = find_secrets(own if not estimated else steps) if facts.has_image else []
    if secrets:
        result.flags.append("F6")
    own_digests = tuple(s.digest for _, s in own if s.digest and s.size > 0)
    result.fingerprint = own_digests or None

    # Score ----------------------------------------------------------------
    if not facts.has_image:
        result.status = "no_image"
        result.score = 0.0
        return result
    counted = [c for c in checks if c.status not in ("skip", "off")]
    total = sum(c.max_points for c in counted)
    raw = sum(c.max_points * c.fraction for c in counted)
    score = 100.0 * raw / total if total > 0 else 0.0
    if late and rubric.late_penalty_percent:
        score *= 1 - rubric.late_penalty_percent / 100.0
        result.penalties.append(f"Late: -{rubric.late_penalty_percent:g}%")
    if secrets and rubric.secret_penalty:
        score -= rubric.secret_penalty
        result.penalties.append(f"Secret-like value ({', '.join(secrets[:3])}): -{rubric.secret_penalty:g}")
    result.score = round1(max(0.0, min(100.0, score)))
    return result
