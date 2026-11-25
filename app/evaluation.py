"""Per-roster repo selection, scoring results and faculty overrides.

State lives on the Roster object (in memory, same 12 h lifetime). Facts fetched
from Docker Hub are kept with each result, so changing the rubric rescores
everything instantly without fetching again.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from app.dockerhub import RepoSubmission, StudentProfileSummary, dockerhub_client
from app.roster import Roster, Student
from app.scoring import FLAGS, RepoFacts, RepoResult, Rubric, parse_deadline, parse_iso, round1, score_repo

MAX_SELECTED_REPOS = 50
AUTO_RULES = ("keep", "name_contains", "latest", "all_pushed")


class SelectionError(ValueError):
    pass


@dataclass
class Selection:
    repo: str
    tag: Optional[str] = None          # None: chosen automatically at evaluation time
    weight: float = 1.0
    deadline: Optional[str] = None     # ISO; overrides the class deadline for this repo

    def to_dict(self) -> dict:
        return {"repo": self.repo, "tag": self.tag, "weight": self.weight, "deadline": self.deadline}


@dataclass
class RepoEval:
    selection: Selection
    facts: Optional[RepoFacts] = None
    result: Optional[RepoResult] = None
    override_score: Optional[float] = None
    override_note: str = ""

    @property
    def final_score(self) -> Optional[float]:
        if self.override_score is not None:
            return self.override_score
        return self.result.score if self.result else None


@dataclass
class StudentEval:
    selections: List[Selection] = field(default_factory=list)
    repos: Dict[str, RepoEval] = field(default_factory=dict)
    override_score: Optional[float] = None
    override_note: str = ""
    evaluated_at: Optional[float] = None
    error: str = ""


@dataclass
class RosterScoring:
    rubric: Rubric = field(default_factory=Rubric.default)
    students: Dict[str, StudentEval] = field(default_factory=dict)


def scoring_for(roster: Roster) -> RosterScoring:
    if roster.scoring is None:
        roster.scoring = RosterScoring()
    return roster.scoring


# ---------------------------------------------------------------- selection

def choose_tag(sub: RepoSubmission, deadline: Optional[datetime]) -> Optional[str]:
    """Default tag: the last one pushed before the deadline, else the most recently pushed."""
    pushed = [(t.tag_last_pushed, t.name) for t in sub.tags if t.tag_last_pushed]
    if deadline is not None:
        before = [p for p in pushed if (parse_iso(p[0]) or deadline) <= deadline]
        if before:
            return max(before)[1]
    if pushed:
        return max(pushed)[1]
    names = [t.name for t in sub.tags]
    return "latest" if "latest" in names else (names[0] if names else None)


def validate_selections(profile: StudentProfileSummary, raw: object) -> List[Selection]:
    """Selections from user input, checked against the student's own Docker Hub listing."""
    if not isinstance(raw, list):
        raise SelectionError("'repos' must be a list.")
    if len(raw) > MAX_SELECTED_REPOS:
        raise SelectionError(f"Select at most {MAX_SELECTED_REPOS} repositories.")
    by_name = {s.name.lower(): s for s in profile.submissions}
    out: List[Selection] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise SelectionError("Each selected repository must be an object.")
        name = str(item.get("repo") or "").strip().lower()
        sub = by_name.get(name)
        if sub is None:
            raise SelectionError(f"Repository '{name}' is not in this student's Docker Hub profile.")
        if sub.name in seen:
            raise SelectionError(f"Repository '{sub.name}' is selected twice.")
        seen.add(sub.name)
        tag = item.get("tag")
        tag = str(tag).strip() if tag not in (None, "") else None
        if tag is not None and tag not in {t.name for t in sub.tags}:
            raise SelectionError(f"Tag '{tag}' does not exist in {sub.name}.")
        try:
            weight = float(item.get("weight", 1) if item.get("weight") not in (None, "") else 1)
        except (TypeError, ValueError):
            raise SelectionError(f"Weight for {sub.name} must be a number.")
        if not 0 < weight <= 100:
            raise SelectionError(f"Weight for {sub.name} must be more than 0 and at most 100.")
        try:
            dl = parse_deadline(item.get("deadline"))
        except ValueError as e:
            raise SelectionError(str(e))
        out.append(Selection(repo=sub.name, tag=tag, weight=weight, deadline=dl.isoformat() if dl else None))
    return out


def auto_select(profile: StudentProfileSummary, rule: str, text: str = "") -> List[Selection]:
    subs = profile.submissions
    if rule == "name_contains":
        needle = (text or "").strip().lower()
        if not needle:
            raise SelectionError("Type the text the repository name should contain.")
        return [Selection(repo=s.name) for s in subs if needle in s.name.lower()]
    if rule == "latest":
        pushed = [s for s in subs if s.submission_date]
        return [Selection(repo=max(pushed, key=lambda s: s.submission_date).name)] if pushed else []
    if rule == "all_pushed":
        return [Selection(repo=s.name) for s in subs if s.submission_date]
    raise SelectionError(f"Unknown selection rule '{rule}'.")


# ---------------------------------------------------------------- evaluation

async def evaluate_student(roster: Roster, student: Student, selections: Optional[List[Selection]] = None,
                           profile: Optional[StudentProfileSummary] = None) -> StudentEval:
    scoring = scoring_for(roster)
    key = student.docker_id_lower
    previous = scoring.students.get(key)
    if selections is None:
        selections = previous.selections if previous else []
    if profile is None:
        profile = await dockerhub_client.get_student_profile(
            roll=student.roll, name=student.name, docker_id=student.docker_id, use_cache=True)

    se = StudentEval(selections=selections, evaluated_at=time.time())
    if previous:
        se.override_score, se.override_note = previous.override_score, previous.override_note
    by_name = {s.name: s for s in profile.submissions}
    rubric = scoring.rubric

    async def one(sel: Selection) -> RepoEval:
        sub = by_name.get(sel.repo)
        if sub is None:
            facts = RepoFacts(repo=sel.repo, tag=sel.tag,
                              error=profile.error_note or "Repository not found on Docker Hub any more - re-check later")
        else:
            deadline = parse_deadline(sel.deadline) if sel.deadline else rubric.deadline_dt
            tag = sel.tag or choose_tag(sub, deadline)
            try:
                facts = await dockerhub_client.get_repo_facts(sub, tag)
            except Exception as exc:  # network trouble must not look like a real result
                facts = RepoFacts(repo=sel.repo, tag=tag, error=f"Fetch failed ({type(exc).__name__}) - re-check later")
        re_ = RepoEval(selection=sel, facts=facts, result=score_repo(facts, rubric, sel.deadline))
        old = previous.repos.get(sel.repo) if previous else None
        if old is not None:
            re_.override_score, re_.override_note = old.override_score, old.override_note
        return re_

    results = await asyncio.gather(*(one(s) for s in selections))
    se.repos = {r.selection.repo: r for r in results}
    if selections:
        scoring.students[key] = se
    else:
        scoring.students.pop(key, None)
    return se


def rescore_all(roster: Roster) -> None:
    scoring = scoring_for(roster)
    for se in scoring.students.values():
        for re_ in se.repos.values():
            if re_.facts is not None:
                re_.result = score_repo(re_.facts, scoring.rubric, re_.selection.deadline)


def set_override(roster: Roster, student: Student, repo: Optional[str], score: Optional[float], note: str) -> None:
    scoring = scoring_for(roster)
    se = scoring.students.get(student.docker_id_lower)
    if se is None:
        raise SelectionError("Evaluate this student before overriding a score.")
    if score is not None:
        try:
            score = round(float(score), 1)
        except (TypeError, ValueError):
            raise SelectionError("Score must be a number.")
        if not 0 <= score <= 100:
            raise SelectionError("Score must be between 0 and 100.")
    note = (note or "").strip()[:500]
    if repo:
        re_ = se.repos.get(repo)
        if re_ is None:
            raise SelectionError(f"Repository '{repo}' is not selected for this student.")
        re_.override_score, re_.override_note = score, (note if score is not None else "")
    else:
        se.override_score, se.override_note = score, (note if score is not None else "")


# ---------------------------------------------------------------- read models

def class_flags(scoring: RosterScoring) -> Dict[Tuple[str, str], List[str]]:
    """F1 (same image as another student) and F2 (same image in two of one student's repos)."""
    owners: Dict[Tuple[str, ...], List[Tuple[str, str]]] = {}
    for did, se in scoring.students.items():
        for name, re_ in se.repos.items():
            fp = re_.result.fingerprint if re_.result else None
            if fp:
                owners.setdefault(fp, []).append((did, name))
    out: Dict[Tuple[str, str], List[str]] = {}
    for fp, items in owners.items():
        students = {d for d, _ in items}
        for did, name in items:
            flags = out.setdefault((did, name), [])
            if len(students) > 1:
                flags.append("F1")
            if sum(1 for d, _ in items if d == did) > 1:
                flags.append("F2")
    return out


def _weighted(values: List[Tuple[float, float]]) -> Optional[float]:
    total = sum(w for w, _ in values)
    return round1(sum(w * v for w, v in values) / total) if total > 0 else None


def student_scores(se: StudentEval) -> dict:
    scored = [r for r in se.repos.values() if r.result and r.result.score is not None]
    suggested = _weighted([(r.selection.weight, r.result.score) for r in scored])
    final = _weighted([(r.selection.weight, r.final_score) for r in scored])
    overridden = se.override_score is not None or any(r.override_score is not None for r in scored)
    if se.override_score is not None:
        final = se.override_score
    recheck = sum(1 for r in se.repos.values() if r.result and r.result.status == "needs_recheck")
    return {"suggested": suggested, "final": final, "overridden": overridden,
            "needs_recheck": recheck, "provisional": recheck > 0}


def _repo_flags(se_key: str, re_: RepoEval, extra: Dict[Tuple[str, str], List[str]]) -> List[str]:
    flags = list(re_.result.flags) if re_.result else []
    for f in extra.get((se_key, re_.selection.repo), []):
        if f not in flags:
            flags.append(f)
    return sorted(flags)


def student_summary(scoring: RosterScoring, student: Student, extra=None) -> dict:
    extra = class_flags(scoring) if extra is None else extra
    se = scoring.students.get(student.docker_id_lower)
    base = {"docker_id": student.docker_id, "roll": student.roll, "name": student.name}
    if se is None:
        return {**base, "status": "not_evaluated", "repos": 0, "suggested": None, "final": None,
                "flags": [], "overridden": False, "provisional": False}
    s = student_scores(se)
    flags = sorted({f for re_ in se.repos.values() for f in _repo_flags(student.docker_id_lower, re_, extra)})
    status = "needs_recheck" if s["provisional"] else ("overridden" if s["overridden"] else "scored")
    return {**base, "status": status, "repos": len(se.repos), "suggested": s["suggested"], "final": s["final"],
            "flags": flags, "overridden": s["overridden"], "provisional": s["provisional"],
            "evaluated_at": se.evaluated_at}


def student_detail(scoring: RosterScoring, student: Student) -> dict:
    extra = class_flags(scoring)
    se = scoring.students.get(student.docker_id_lower)
    summary = student_summary(scoring, student, extra)
    repos = []
    if se:
        for sel in se.selections:
            re_ = se.repos.get(sel.repo)
            if re_ is None:
                continue
            d = re_.result.to_dict() if re_.result else {"repo": sel.repo}
            d.update({"selection": sel.to_dict(), "flags": _repo_flags(student.docker_id_lower, re_, extra),
                      "override_score": re_.override_score, "override_note": re_.override_note,
                      "final_score": re_.final_score})
            repos.append(d)
    return {"summary": summary, "repos": repos,
            "selections": [s.to_dict() for s in se.selections] if se else [],
            "override_score": se.override_score if se else None,
            "override_note": se.override_note if se else "", "flag_labels": FLAGS}
