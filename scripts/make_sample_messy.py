from pathlib import Path
from openpyxl import Workbook

def main():
    wb = Workbook()
    ws = wb.active
    ws.title = "Students"

    # Row 1: Headers with alias "Docker Hub ID"
    ws.append(["Roll No", "Name", "Docker Hub ID"])

    # Row 2: valid student
    ws.append([1, "Alice", "demo-student-01"])

    # Row 3: valid student with full URL and mixed-case
    ws.append([2, "Bob", "https://hub.docker.com/u/demo-student-02/"])

    # Row 4: duplicate Docker ID (demo-student-01)
    ws.append([3, "Charlie", "demo-student-01"])

    # Row 5: missing Docker ID
    ws.append([4, "Dave", ""])

    # Row 6: invalid characters
    ws.append([5, "Eve", "bad id!"])

    # Row 7: completely blank row
    ws.append([None, None, None])

    # Row 8: Docker ID only, no roll/name
    ws.append(["", "", "demo-student-06"])

    # Row 9: formula-looking name
    ws.append([7, '=HYPERLINK("http://evil.com","Click")', "demo-student-03"])

    out_path = Path(__file__).resolve().parent.parent / "sample_messy.xlsx"
    wb.save(out_path)
    print(f"Created {out_path.name}")

if __name__ == "__main__":
    main()
