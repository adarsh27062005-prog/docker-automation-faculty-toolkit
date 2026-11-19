import io
import re
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from app.config import settings
from app.dockerhub import StudentProfileSummary, RepoSubmission

# Colors and styling
HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
LINK_FONT = Font(name="Calibri", size=11, color="0563C1", underline="single")
REGULAR_FONT = Font(name="Calibri", size=11)
BOLD_FONT = Font(name="Calibri", size=11, bold=True)
TITLE_FONT = Font(name="Calibri", size=14, bold=True, color="1F4E78")
SUBTITLE_FONT = Font(name="Calibri", size=10, italic=True, color="555555")
THIN_BORDER = Border(
    left=Side(style='thin', color='D9D9D9'),
    right=Side(style='thin', color='D9D9D9'),
    top=Side(style='thin', color='D9D9D9'),
    bottom=Side(style='thin', color='D9D9D9')
)

DATE_FORMAT = "yyyy-mm-dd hh:mm"

def sanitize_filename(name: str) -> str:
    """Sanitizes filename for safe filesystem and content-disposition usage."""
    clean = re.sub(r'[\\/*?:"<>|]', '_', name)
    clean = re.sub(r'\s+', '_', clean)
    return clean.strip("._")

def make_student_filename(roll: Optional[str], docker_id: str) -> str:
    """
    Generates single-student Excel filename:
    - If roll is present and non-empty: {roll}_{docker_id}_{YYYY-MM-DD}.xlsx (non-alphanumeric in roll replaced by _)
    - If roll is missing/empty: {docker_id}_{YYYY-MM-DD}.xlsx
    """
    today = datetime.now().strftime("%Y-%m-%d")
    clean_id = sanitize_filename(docker_id)
    if roll and roll.strip() and roll.strip() != "-":
        clean_roll = re.sub(r'[^a-zA-Z0-9]+', '_', roll.strip()).strip('_')
        if clean_roll:
            return f"{clean_roll}_{clean_id}_{today}.xlsx"
    return f"{clean_id}_{today}.xlsx"

def set_safe_cell(cell, value):
    """
    Sets cell value safely with formula injection mitigation.
    If value is a string starting with '=', '+', '-', '@', '\t', or '\r',
    prefix with single quote "'" and set cell.data_type = 's'.
    """
    if isinstance(value, str) and len(value) > 0:
        if value[0] in ('=', '+', '-', '@', '\t', '\r'):
            cell.value = "'" + value
            cell.data_type = 's'
            return cell
    cell.value = value
    return cell

def parse_and_convert_to_display_tz_naive(
    iso_str: Optional[str],
    target_tz: ZoneInfo = settings.display_tz
) -> Optional[datetime]:
    """
    Parses an ISO datetime string, converts to target timezone (default Asia/Kolkata),
    and returns a naive datetime for openpyxl compatibility.
    """
    if not iso_str:
        return None
    try:
        s = iso_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        dt_local = dt.astimezone(target_tz)
        return dt_local.replace(tzinfo=None)
    except Exception:
        return None

def auto_fit_columns(ws, min_width: int = 10, max_width: int = 55):
    """Adjusts column widths based on maximum cell content length."""
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            val = cell.value
            if val is not None:
                if isinstance(val, datetime):
                    cell_len = 16
                else:
                    cell_len = len(str(val))
                if cell_len > max_len:
                    max_len = cell_len
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, min_width), max_width)

def build_student_workbook(profile: StudentProfileSummary) -> Workbook:
    """
    Builds the single-student Excel workbook:
    - Sheet 1: "Repositories" FIRST and set as active sheet
    - Sheet 2: "Summary"
    - Sheet 3: "Tags"
    - Sheet 4: "Notes"
    """
    wb = Workbook()
    
    tz_label = "IST" if settings.DISPLAY_TZ_NAME == "Asia/Kolkata" else settings.DISPLAY_TZ_NAME
    has_deadline = (settings.submission_deadline is not None)
    basis_str = settings.SUBMISSION_DATE_BASIS

    disp_roll = profile.roll.strip() if (profile.roll and profile.roll.strip()) else "-"
    disp_name = profile.name.strip() if (profile.name and profile.name.strip()) else "-"
    disp_id = profile.docker_id.strip() if (profile.docker_id and profile.docker_id.strip()) else "-"

    # Check Decision 5: Has at least one GitHub URL across submissions
    has_any_github = any(
        (s.github_url and s.github_url != "Not found") or (len(s.other_github_urls) > 0)
        for s in profile.submissions
    )

    # ----------------------------------------------------
    # Sheet 1: Repositories (FIRST & ACTIVE)
    # ----------------------------------------------------
    ws_repos = wb.active
    ws_repos.title = "Repositories"
    ws_repos.views.sheetView[0].showGridLines = True

    # Row 1: Title
    set_safe_cell(
        ws_repos.cell(row=1, column=1),
        f"Submitted Repositories: {disp_name} ({disp_roll}) - Docker ID: {disp_id}"
    ).font = TITLE_FONT
    ws_repos.row_dimensions[1].height = 24

    # Row 2: Subtitle
    now_ist = datetime.now(settings.display_tz).strftime("%Y-%m-%d %H:%M")
    set_safe_cell(
        ws_repos.cell(row=2, column=1),
        f"Total repositories: {profile.total_repos} | Report generated: {now_ist} {tz_label} | Date basis: {basis_str}"
    ).font = SUBTITLE_FONT

    # Row 3: Blank

    # Row 4: Header
    headers = [
        "S.No",
        "Repository Name",
        f"Submission Date ({tz_label})",
        f"Repo Created ({tz_label})",
        f"First Push ({tz_label})",
        f"Last Push ({tz_label})",
        "Downloads (Pulls)",
        "Stars",
        "Tags Count",
    ]
    if has_deadline:
        headers.append("Status")
    if has_any_github:
        headers.extend(["GitHub URL", "Other GitHub Links"])
    headers.append("Notes")

    header_row_idx = 4
    for col_idx, h_text in enumerate(headers, start=1):
        c = ws_repos.cell(row=header_row_idx, column=col_idx)
        set_safe_cell(c, h_text)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws_repos.row_dimensions[header_row_idx].height = 24

    # Freeze panes below row 4
    ws_repos.freeze_panes = "A5"

    # Data Rows
    row_idx = 5
    if not profile.submissions:
        ws_repos.cell(row=row_idx, column=1, value=1).border = THIN_BORDER
        c_empty = ws_repos.cell(row=row_idx, column=2)
        set_safe_cell(c_empty, "No repositories")
        c_empty.border = THIN_BORDER
        note_col = len(headers)
        c_note = ws_repos.cell(row=row_idx, column=note_col)
        set_safe_cell(c_note, profile.error_note or "No public repositories")
        c_note.border = THIN_BORDER
        row_idx += 1
    else:
        for s_no, sub in enumerate(profile.submissions, start=1):
            col = 1
            # 1. S.No
            ws_repos.cell(row=row_idx, column=col, value=s_no).border = THIN_BORDER
            col += 1

            # 2. Repository Name (hyperlink)
            c_name = ws_repos.cell(row=row_idx, column=col)
            set_safe_cell(c_name, sub.name)
            c_name.hyperlink = sub.repo_url
            c_name.font = LINK_FONT
            c_name.border = THIN_BORDER
            col += 1

            # 3. Submission Date (IST)
            c_sub_date = ws_repos.cell(row=row_idx, column=col)
            dt_sub = parse_and_convert_to_display_tz_naive(sub.submission_date)
            if dt_sub:
                c_sub_date.value = dt_sub
                c_sub_date.number_format = DATE_FORMAT
            c_sub_date.border = THIN_BORDER
            col += 1

            # 4. Repo Created (IST)
            c_created = ws_repos.cell(row=row_idx, column=col)
            dt_created = parse_and_convert_to_display_tz_naive(sub.date_registered)
            if dt_created:
                c_created.value = dt_created
                c_created.number_format = DATE_FORMAT
            c_created.border = THIN_BORDER
            col += 1

            # 5. First Push (IST)
            c_first = ws_repos.cell(row=row_idx, column=col)
            dt_first = parse_and_convert_to_display_tz_naive(sub.first_tag_pushed)
            if dt_first:
                c_first.value = dt_first
                c_first.number_format = DATE_FORMAT
            c_first.border = THIN_BORDER
            col += 1

            # 6. Last Push (IST)
            c_last = ws_repos.cell(row=row_idx, column=col)
            dt_last = parse_and_convert_to_display_tz_naive(sub.latest_tag_pushed)
            if dt_last:
                c_last.value = dt_last
                c_last.number_format = DATE_FORMAT
            c_last.border = THIN_BORDER
            col += 1

            # 7. Downloads (Pulls)
            ws_repos.cell(row=row_idx, column=col, value=sub.pull_count).border = THIN_BORDER
            col += 1

            # 8. Stars
            ws_repos.cell(row=row_idx, column=col, value=sub.star_count).border = THIN_BORDER
            col += 1

            # 9. Tags Count
            ws_repos.cell(row=row_idx, column=col, value=sub.tags_count).border = THIN_BORDER
            col += 1

            # 10. Status (conditional)
            if has_deadline:
                c_stat = ws_repos.cell(row=row_idx, column=col)
                set_safe_cell(c_stat, sub.status or "")
                c_stat.border = THIN_BORDER
                col += 1

            # 11 & 12. GitHub URL / Other GitHub Links (conditional)
            if has_any_github:
                c_gh = ws_repos.cell(row=row_idx, column=col)
                set_safe_cell(c_gh, sub.github_url)
                if sub.github_url and sub.github_url != "Not found":
                    c_gh.hyperlink = sub.github_url
                    c_gh.font = LINK_FONT
                c_gh.border = THIN_BORDER
                col += 1

                c_oth = ws_repos.cell(row=row_idx, column=col)
                set_safe_cell(c_oth, "; ".join(sub.other_github_urls))
                c_oth.border = THIN_BORDER
                col += 1

            # Notes
            c_not = ws_repos.cell(row=row_idx, column=col)
            set_safe_cell(c_not, sub.notes)
            c_not.border = THIN_BORDER
            row_idx += 1

    # Autofilter on header row
    last_col_letter = get_column_letter(len(headers))
    last_row = max(row_idx - 1, header_row_idx)
    ws_repos.auto_filter.ref = f"A{header_row_idx}:{last_col_letter}{last_row}"

    auto_fit_columns(ws_repos)

    # ----------------------------------------------------
    # Sheet 2: Summary (SECOND)
    # ----------------------------------------------------
    ws_summary = wb.create_sheet(title="Summary")
    ws_summary.views.sheetView[0].showGridLines = True

    summary_rows = [
        ("Student Name", disp_name),
        ("Roll Number", disp_roll),
        ("Docker ID", disp_id),
        ("Total Repositories", profile.total_repos),
        ("Total Downloads (Pulls)", profile.total_pulls),
        ("Repos with GitHub Link", profile.repos_with_github),
        ("Status / Note", profile.error_note if profile.error_note else "Active"),
        ("Submission Date Basis", f"{basis_str} (SUBMISSION_DATE_BASIS)"),
        ("Report Generated At", datetime.now(settings.display_tz).strftime("%Y-%m-%d %H:%M:%S") + f" ({tz_label})"),
    ]

    ws_summary.merge_cells("A1:B1")
    t_cell = ws_summary["A1"]
    set_safe_cell(t_cell, f"Docker Submission Summary: {disp_name}")
    t_cell.font = TITLE_FONT
    ws_summary.row_dimensions[1].height = 25

    s_idx = 3
    for label, val in summary_rows:
        c1 = ws_summary.cell(row=s_idx, column=1)
        set_safe_cell(c1, label)
        c1.font = BOLD_FONT
        c1.border = THIN_BORDER

        c2 = ws_summary.cell(row=s_idx, column=2)
        set_safe_cell(c2, val)
        c2.font = REGULAR_FONT
        c2.border = THIN_BORDER
        if label == "Docker ID" and profile.docker_id_lower:
            c2.hyperlink = f"https://hub.docker.com/u/{profile.docker_id_lower}"
            c2.font = LINK_FONT
        s_idx += 1

    auto_fit_columns(ws_summary, min_width=18, max_width=45)

    # ----------------------------------------------------
    # Sheet 3: Tags
    # ----------------------------------------------------
    ws_tags = wb.create_sheet(title="Tags")
    ws_tags.views.sheetView[0].showGridLines = True
    ws_tags.freeze_panes = "A2"

    tag_headers = ["Repository", "Tag Name", f"Pushed At ({tz_label})", "Size (MB)", "Architecture", "OS"]
    for c_i, t_head in enumerate(tag_headers, start=1):
        c = ws_tags.cell(row=1, column=c_i)
        set_safe_cell(c, t_head)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center")
    ws_tags.row_dimensions[1].height = 24

    tag_row_idx = 2
    for sub in profile.submissions:
        for t in sub.tags:
            c_rname = ws_tags.cell(row=tag_row_idx, column=1)
            set_safe_cell(c_rname, sub.name)
            c_rname.border = THIN_BORDER

            c_tname = ws_tags.cell(row=tag_row_idx, column=2)
            set_safe_cell(c_tname, t.name)
            c_tname.border = THIN_BORDER

            c_pushed = ws_tags.cell(row=tag_row_idx, column=3)
            dt_pushed = parse_and_convert_to_display_tz_naive(t.tag_last_pushed)
            if dt_pushed:
                c_pushed.value = dt_pushed
                c_pushed.number_format = DATE_FORMAT
            c_pushed.border = THIN_BORDER

            size_mb = round(t.full_size / (1024 * 1024), 2) if t.full_size else 0.0
            ws_tags.cell(row=tag_row_idx, column=4, value=size_mb).border = THIN_BORDER

            c_arch = ws_tags.cell(row=tag_row_idx, column=5)
            set_safe_cell(c_arch, t.architecture)
            c_arch.border = THIN_BORDER

            c_os = ws_tags.cell(row=tag_row_idx, column=6)
            set_safe_cell(c_os, t.os)
            c_os.border = THIN_BORDER

            tag_row_idx += 1

    auto_fit_columns(ws_tags)

    # ----------------------------------------------------
    # Sheet 4: Notes
    # ----------------------------------------------------
    ws_notes = wb.create_sheet(title="Notes")
    ws_notes.views.sheetView[0].showGridLines = True

    notes_content = [
        ("Field / Metric", "Definition & Specification"),
        ("Submission Date", f"The official submission datetime based on SUBMISSION_DATE_BASIS='{basis_str}'. Default is 'last_push' (datetime of most recent tag push). Blank if no tags were pushed."),
        ("Downloads (Pulls)", "Cumulative count of pulls for the entire repository (pull_count). Includes pulls made by automated systems, faculty, peers, and anyone pulling the image."),
        ("Repo Created", "Timestamp (date_registered) when the repository was first created on Docker Hub."),
        ("First Push", "Earliest tag_last_pushed date among all tags in the repository."),
        ("Last Push", "Most recent tag_last_pushed date among all tags in the repository."),
        ("No image pushed", "Indicates that the repository was created on Docker Hub but no Docker image tags were ever pushed to it."),
        ("Timezone", f"All dates and times are converted to {tz_label} (UTC+5:30) and formatted as yyyy-mm-dd hh:mm."),
        ("GitHub URL", f"Discovered from overrides.csv or Docker Hub repository description. README lookup: {'Enabled' if settings.GITHUB_README_LOOKUP else 'Disabled (GITHUB_README_LOOKUP=false)'}. Hidden if no GitHub links exist in the workbook."),
        ("Data Source", "Live queries to the public Docker Hub v2 API (https://hub.docker.com/v2/)."),
    ]

    for r_i, (k, v) in enumerate(notes_content, start=1):
        c1 = ws_notes.cell(row=r_i, column=1)
        set_safe_cell(c1, k)
        c2 = ws_notes.cell(row=r_i, column=2)
        set_safe_cell(c2, v)
        c1.border = THIN_BORDER
        c2.border = THIN_BORDER
        if r_i == 1:
            c1.font = HEADER_FONT
            c1.fill = HEADER_FILL
            c2.font = HEADER_FONT
            c2.fill = HEADER_FILL
        else:
            c1.font = BOLD_FONT
            c2.font = REGULAR_FONT

    auto_fit_columns(ws_notes, min_width=20, max_width=80)

    # Ensure Repositories sheet is active when file opens
    wb.active = ws_repos

    return wb

def build_combined_workbook(profiles: List[StudentProfileSummary]) -> Workbook:
    """
    Builds the combined export workbook:
    - Sheet 1: "All Submissions" (sorted by Roll Number, then Submission Date newest first)
    - Sheet 2: "Student Summary" (one row per student)
    """
    wb = Workbook()
    
    tz_label = "IST" if settings.DISPLAY_TZ_NAME == "Asia/Kolkata" else settings.DISPLAY_TZ_NAME
    has_deadline = (settings.submission_deadline is not None)

    # Check Decision 5 across all profiles
    has_any_github = any(
        any((s.github_url and s.github_url != "Not found") or (len(s.other_github_urls) > 0) for s in p.submissions)
        for p in profiles
    )

    # ----------------------------------------------------
    # Sheet 1: All Submissions
    # ----------------------------------------------------
    ws_all = wb.active
    ws_all.title = "All Submissions"
    ws_all.views.sheetView[0].showGridLines = True
    ws_all.freeze_panes = "A2"

    headers = [
        "Roll Number",
        "Student Name",
        "Docker ID",
        "Repository Name",
        f"Submission Date ({tz_label})",
        f"Repo Created ({tz_label})",
        f"First Push ({tz_label})",
        f"Last Push ({tz_label})",
        "Downloads (Pulls)",
        "Stars",
        "Tags Count",
    ]
    if has_deadline:
        headers.append("Status")
    if has_any_github:
        headers.extend(["GitHub URL", "Other GitHub Links"])
    headers.append("Notes")

    for col_idx, h_text in enumerate(headers, start=1):
        cell = ws_all.cell(row=1, column=col_idx)
        set_safe_cell(cell, h_text)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws_all.row_dimensions[1].height = 24

    flat_rows = []
    for profile in profiles:
        r_roll = profile.roll.strip() if (profile.roll and profile.roll.strip()) else "-"
        r_name = profile.name.strip() if (profile.name and profile.name.strip()) else "-"
        r_id = profile.docker_id.strip() if (profile.docker_id and profile.docker_id.strip()) else "-"

        if not profile.submissions:
            flat_rows.append({
                "roll": r_roll,
                "name": r_name,
                "docker_id": r_id,
                "docker_id_lower": profile.docker_id_lower,
                "repo_name": "-",
                "repo_url": None,
                "submission_date": None,
                "date_registered": None,
                "first_tag_pushed": None,
                "latest_tag_pushed": None,
                "pull_count": 0,
                "star_count": 0,
                "tags_count": 0,
                "status": None,
                "github_url": "Not found",
                "other_github_urls": [],
                "notes": profile.error_note or "No public repositories",
            })
        else:
            for sub in profile.submissions:
                flat_rows.append({
                    "roll": r_roll,
                    "name": r_name,
                    "docker_id": r_id,
                    "docker_id_lower": profile.docker_id_lower,
                    "repo_name": sub.name,
                    "repo_url": sub.repo_url,
                    "submission_date": sub.submission_date,
                    "date_registered": sub.date_registered,
                    "first_tag_pushed": sub.first_tag_pushed,
                    "latest_tag_pushed": sub.latest_tag_pushed,
                    "pull_count": sub.pull_count,
                    "star_count": sub.star_count,
                    "tags_count": sub.tags_count,
                    "status": sub.status,
                    "github_url": sub.github_url,
                    "other_github_urls": sub.other_github_urls,
                    "notes": sub.notes,
                })

    # Group by student (Roll if available, else docker_id) and sort within group by submission_date descending
    from itertools import groupby

    def group_sort_key(item):
        roll_val = item["roll"]
        if roll_val and roll_val != "-":
            return (0, roll_val.lower())
        return (1, item["docker_id_lower"])

    flat_rows.sort(key=group_sort_key)

    sorted_flat_rows = []
    for _, group in groupby(flat_rows, key=group_sort_key):
        items = list(group)
        items.sort(
            key=lambda r: (1 if r["submission_date"] is not None else 0, r["submission_date"] or ""),
            reverse=True
        )
        sorted_flat_rows.extend(items)

    row_idx = 2
    for r in sorted_flat_rows:
        col = 1
        c_roll = ws_all.cell(row=row_idx, column=col)
        set_safe_cell(c_roll, r["roll"])
        c_roll.border = THIN_BORDER
        col += 1

        c_sname = ws_all.cell(row=row_idx, column=col)
        set_safe_cell(c_sname, r["name"])
        c_sname.border = THIN_BORDER
        col += 1

        c_did = ws_all.cell(row=row_idx, column=col)
        set_safe_cell(c_did, r["docker_id"])
        if r["docker_id_lower"] and r["docker_id"] != "-":
            c_did.hyperlink = f"https://hub.docker.com/u/{r['docker_id_lower']}"
            c_did.font = LINK_FONT
        c_did.border = THIN_BORDER
        col += 1

        c_repo = ws_all.cell(row=row_idx, column=col)
        set_safe_cell(c_repo, r["repo_name"])
        if r["repo_url"]:
            c_repo.hyperlink = r["repo_url"]
            c_repo.font = LINK_FONT
        c_repo.border = THIN_BORDER
        col += 1

        # Submission Date
        c_sub = ws_all.cell(row=row_idx, column=col)
        dt_sub = parse_and_convert_to_display_tz_naive(r["submission_date"])
        if dt_sub:
            c_sub.value = dt_sub
            c_sub.number_format = DATE_FORMAT
        c_sub.border = THIN_BORDER
        col += 1

        # Repo Created
        c_created = ws_all.cell(row=row_idx, column=col)
        dt_created = parse_and_convert_to_display_tz_naive(r["date_registered"])
        if dt_created:
            c_created.value = dt_created
            c_created.number_format = DATE_FORMAT
        c_created.border = THIN_BORDER
        col += 1

        # First Push
        c_first = ws_all.cell(row=row_idx, column=col)
        dt_first = parse_and_convert_to_display_tz_naive(r["first_tag_pushed"])
        if dt_first:
            c_first.value = dt_first
            c_first.number_format = DATE_FORMAT
        c_first.border = THIN_BORDER
        col += 1

        # Last Push
        c_last = ws_all.cell(row=row_idx, column=col)
        dt_last = parse_and_convert_to_display_tz_naive(r["latest_tag_pushed"])
        if dt_last:
            c_last.value = dt_last
            c_last.number_format = DATE_FORMAT
        c_last.border = THIN_BORDER
        col += 1

        ws_all.cell(row=row_idx, column=col, value=r["pull_count"]).border = THIN_BORDER
        col += 1
        ws_all.cell(row=row_idx, column=col, value=r["star_count"]).border = THIN_BORDER
        col += 1
        ws_all.cell(row=row_idx, column=col, value=r["tags_count"]).border = THIN_BORDER
        col += 1

        if has_deadline:
            c_stat = ws_all.cell(row=row_idx, column=col)
            set_safe_cell(c_stat, r["status"] or "")
            c_stat.border = THIN_BORDER
            col += 1

        if has_any_github:
            c_gh = ws_all.cell(row=row_idx, column=col)
            set_safe_cell(c_gh, r["github_url"])
            if r["github_url"] and r["github_url"] != "Not found":
                c_gh.hyperlink = r["github_url"]
                c_gh.font = LINK_FONT
            c_gh.border = THIN_BORDER
            col += 1

            c_oth = ws_all.cell(row=row_idx, column=col)
            set_safe_cell(c_oth, "; ".join(r["other_github_urls"]))
            c_oth.border = THIN_BORDER
            col += 1

        c_not = ws_all.cell(row=row_idx, column=col)
        set_safe_cell(c_not, r["notes"])
        c_not.border = THIN_BORDER
        row_idx += 1

    last_col_l = get_column_letter(len(headers))
    ws_all.auto_filter.ref = f"A1:{last_col_l}{row_idx - 1}"
    auto_fit_columns(ws_all)

    # ----------------------------------------------------
    # Sheet 2: Student Summary
    # ----------------------------------------------------
    ws_students = wb.create_sheet(title="Student Summary")
    ws_students.views.sheetView[0].showGridLines = True
    ws_students.freeze_panes = "A2"

    sum_headers = [
        "Roll Number",
        "Student Name",
        "Docker ID",
        "Total Repositories",
        "Total Downloads (Pulls)",
        f"Latest Submission Date ({tz_label})",
        f"Earliest Submission Date ({tz_label})",
        "Repos With No Image Pushed",
    ]

    for c_i, h_txt in enumerate(sum_headers, start=1):
        cell = ws_students.cell(row=1, column=c_i)
        set_safe_cell(cell, h_txt)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws_students.row_dimensions[1].height = 24

    st_row_idx = 2
    for p in profiles:
        disp_p_roll = p.roll.strip() if (p.roll and p.roll.strip()) else "-"
        disp_p_name = p.name.strip() if (p.name and p.name.strip()) else "-"
        disp_p_id = p.docker_id.strip() if (p.docker_id and p.docker_id.strip()) else "-"

        c_r = ws_students.cell(row=st_row_idx, column=1)
        set_safe_cell(c_r, disp_p_roll)
        c_r.border = THIN_BORDER

        c_n = ws_students.cell(row=st_row_idx, column=2)
        set_safe_cell(c_n, disp_p_name)
        c_n.border = THIN_BORDER

        c_did = ws_students.cell(row=st_row_idx, column=3)
        set_safe_cell(c_did, disp_p_id)
        if p.docker_id_lower and disp_p_id != "-":
            c_did.hyperlink = f"https://hub.docker.com/u/{p.docker_id_lower}"
            c_did.font = LINK_FONT
        c_did.border = THIN_BORDER

        ws_students.cell(row=st_row_idx, column=4, value=p.total_repos).border = THIN_BORDER
        ws_students.cell(row=st_row_idx, column=5, value=p.total_pulls).border = THIN_BORDER

        # Submission dates
        valid_dates = [s.submission_date for s in p.submissions if s.submission_date]
        latest_date_str = max(valid_dates) if valid_dates else None
        earliest_date_str = min(valid_dates) if valid_dates else None

        c_latest = ws_students.cell(row=st_row_idx, column=6)
        dt_lat = parse_and_convert_to_display_tz_naive(latest_date_str)
        if dt_lat:
            c_latest.value = dt_lat
            c_latest.number_format = DATE_FORMAT
        c_latest.border = THIN_BORDER

        c_earliest = ws_students.cell(row=st_row_idx, column=7)
        dt_ear = parse_and_convert_to_display_tz_naive(earliest_date_str)
        if dt_ear:
            c_earliest.value = dt_ear
            c_earliest.number_format = DATE_FORMAT
        c_earliest.border = THIN_BORDER

        no_push_count = sum(1 for s in p.submissions if s.tags_count == 0 or not s.submission_date)
        ws_students.cell(row=st_row_idx, column=8, value=no_push_count).border = THIN_BORDER

        st_row_idx += 1

    last_s_col = get_column_letter(len(sum_headers))
    ws_students.auto_filter.ref = f"A1:{last_s_col}{st_row_idx - 1}"
    auto_fit_columns(ws_students)

    return wb

def generate_template_workbook() -> Workbook:
    """
    Builds the roster_template.xlsx workbook for faculty download:
    - Sheet 1: "Students" with sample headers and valid formats
    - Sheet 2: "Instructions" explaining columns, formats, and limits
    """
    wb = Workbook()

    # Sheet 1: Students
    ws_students = wb.active
    ws_students.title = "Students"
    ws_students.views.sheetView[0].showGridLines = True

    headers = ["Roll No", "Name", "Docker ID"]
    for col_idx, h_text in enumerate(headers, start=1):
        c = ws_students.cell(row=1, column=col_idx)
        set_safe_cell(c, h_text)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center")
    ws_students.row_dimensions[1].height = 24

    sample_rows = [
        ("CS101", "Sample Student A", "sampleuser1"),
        ("CS102", "Sample Student B", "https://hub.docker.com/u/sampleuser2"),
        ("", "Sample Student C", "sampleuser3"),
    ]

    for r_idx, row_vals in enumerate(sample_rows, start=2):
        for c_idx, val in enumerate(row_vals, start=1):
            c = ws_students.cell(row=r_idx, column=c_idx)
            set_safe_cell(c, val)
            c.border = THIN_BORDER

    auto_fit_columns(ws_students, min_width=15, max_width=45)

    # Sheet 2: Instructions
    ws_inst = wb.create_sheet(title="Instructions")
    ws_inst.views.sheetView[0].showGridLines = True

    inst_headers = ["Field / Rule", "Description"]
    for c_idx, h_text in enumerate(inst_headers, start=1):
        c = ws_inst.cell(row=1, column=c_idx)
        set_safe_cell(c, h_text)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
    ws_inst.row_dimensions[1].height = 24

    instructions = [
        ("File Format", "Must be a standard Excel spreadsheet (.xlsx). Other formats (.xls, .csv, .xlsm) are not accepted."),
        ("Required Columns", "Only 'Docker ID' is mandatory. 'Roll No' and 'Name' are optional (displayed as '-' if omitted)."),
        ("Header Row", "Scanned in the first 10 rows. Aliases accepted: 'Docker ID', 'docker_id', 'Docker Username', 'Docker URL'."),
        ("Docker ID Format", "Can be a simple username (e.g., 'sampleuser1') or a full Docker Hub URL (e.g., 'https://hub.docker.com/u/sampleuser2')."),
        ("Row Limit", f"Maximum {settings.MAX_STUDENTS} students per upload."),
        ("File Size Limit", f"Maximum {settings.MAX_UPLOAD_MB} MB file size."),
        ("Duplicates & Blank Rows", "Blank rows are ignored. Duplicate Docker IDs are automatically skipped with a warning."),
    ]

    for r_idx, (k, v) in enumerate(instructions, start=2):
        c1 = ws_inst.cell(row=r_idx, column=1)
        set_safe_cell(c1, k)
        c1.font = BOLD_FONT
        c1.border = THIN_BORDER

        c2 = ws_inst.cell(row=r_idx, column=2)
        set_safe_cell(c2, v)
        c2.font = REGULAR_FONT
        c2.border = THIN_BORDER

    auto_fit_columns(ws_inst, min_width=20, max_width=85)

    wb.active = ws_students
    return wb

def workbook_to_bytes(wb: Workbook) -> bytes:
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output.getvalue()
