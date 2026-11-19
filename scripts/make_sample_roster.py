import re
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
THIN_BORDER = Border(
    left=Side(style='thin', color='D9D9D9'),
    right=Side(style='thin', color='D9D9D9'),
    top=Side(style='thin', color='D9D9D9'),
    bottom=Side(style='thin', color='D9D9D9')
)

MARKDOWN_LINK_PATTERN = re.compile(r"\[(.*?)\](?:\(.*\))?")

def parse_docker_id_md(md_path: Path):
    students = []
    with open(md_path, "r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if not line_str.startswith("|"):
                continue
            cells = [c.strip() for c in line_str.split("|")]
            if len(cells) < 4:
                continue
            roll = cells[1]
            name = cells[2]
            docker_cell = cells[3]
            if not roll or roll.lower() == "roll number" or set(roll).issubset({"-", ":"}):
                continue
            if not name or name.lower() == "name" or set(name).issubset({"-", ":"}):
                continue
            m = MARKDOWN_LINK_PATTERN.search(docker_cell)
            docker_id = m.group(1).strip() if m else docker_cell.strip()
            students.append((roll, name, docker_id))
    return students

def main():
    root = Path(__file__).resolve().parent.parent
    md_file = root / "docker_id.md"
    out_file = root / "sample_roster.xlsx"

    students = parse_docker_id_md(md_file)
    print(f"Parsed {len(students)} students from {md_file.name}")

    wb = Workbook()
    ws = wb.active
    ws.title = "Students"

    headers = ["Roll No", "Name", "Docker ID"]
    for col_idx, h_text in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=h_text)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 24

    for row_idx, (roll, name, docker_id) in enumerate(students, start=2):
        c1 = ws.cell(row=row_idx, column=1, value=roll)
        c2 = ws.cell(row=row_idx, column=2, value=name)
        c3 = ws.cell(row=row_idx, column=3, value=docker_id)
        c1.border = THIN_BORDER
        c2.border = THIN_BORDER
        c3.border = THIN_BORDER

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    wb.save(out_file)
    print(f"Successfully generated {out_file.name} with {len(students)} student rows.")

if __name__ == "__main__":
    main()
