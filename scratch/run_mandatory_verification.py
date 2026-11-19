import io
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx
from openpyxl import load_workbook

BASE_URL = "http://127.0.0.1:8000"
EXPORT_DIR = Path("scratch/exports")
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
IST = ZoneInfo("Asia/Kolkata")

client = httpx.Client(timeout=600.0)
hub_client = httpx.Client(timeout=20.0, headers={"User-Agent": "MandatoryVerifier/1.0"})

print("=================================================================")
print("             MANDATORY VERIFICATION SCRIPT                       ")
print("=================================================================")

# -------------------------------------------------------------
# STEP 2: Download /api/export/demo-student-01 and inspect
# -------------------------------------------------------------
print("\n--- STEP 2: Inspect /api/export/demo-student-01 ---")
r_first = client.get(f"{BASE_URL}/api/export/demo-student-01")
print(f"Status Code: {r_first.status_code}")
first_path = EXPORT_DIR / "demo-student-01.xlsx"
first_path.write_bytes(r_first.content)

wb_first = load_workbook(first_path)
print(f"Sheet names in order: {wb_first.sheetnames}")
print(f"Active sheet: {wb_first.active.title}")
assert wb_first.sheetnames == ["Repositories", "Summary", "Tags", "Notes"]
assert wb_first.active.title == "Repositories"

ws_repos = wb_first["Repositories"]
print("\nRows 1 to 6 of 'Repositories' sheet:")
for r_idx in range(1, 7):
    row_vals = [cell.value for cell in ws_repos[r_idx]]
    print(f"Row {r_idx}: {row_vals}")

# Count total repo rows (starting from row 5)
repo_rows_count = ws_repos.max_row - 4
print(f"Total number of repo rows: {repo_rows_count}")

# Check hyperlinks on repo-name cells (column B)
cell_b5 = ws_repos["B5"]
cell_b6 = ws_repos["B6"]
print(f"\nHyperlink target for cell B5 ({cell_b5.value}): {cell_b5.hyperlink.target if cell_b5.hyperlink else None}")
print(f"Hyperlink target for cell B6 ({cell_b6.value}): {cell_b6.hyperlink.target if cell_b6.hyperlink else None}")

# Check number format of Submission Date (column C)
cell_c5 = ws_repos["C5"]
print(f"Cell C5 ({cell_c5.value}) type: {type(cell_c5.value).__name__}, number_format: '{cell_c5.number_format}'")

# -------------------------------------------------------------
# STEP 3: Live comparisons for demo-student-01, demo-student-02, demo-student-06
# -------------------------------------------------------------
print("\n--- STEP 3: Live Docker Hub comparisons for 3 students ---")
test_students = ["demo-student-01", "demo-student-02", "demo-student-06"]
student_workbooks = {"demo-student-01": wb_first}

for did in ["demo-student-02", "demo-student-06"]:
    r = client.get(f"{BASE_URL}/api/export/{did}")
    p = EXPORT_DIR / f"{did}.xlsx"
    p.write_bytes(r.content)
    student_workbooks[did] = load_workbook(p)

print("\n(a) Live Docker Hub Repo Count vs Excel Repo Rows:")
print(f"{'Student':<15} | {'Live Count':<12} | {'Excel Repo Rows':<15} | {'Match?':<6}")
print("-" * 55)
for did in test_students:
    # Query live Docker Hub list endpoint
    resp = hub_client.get(f"https://hub.docker.com/v2/repositories/{did}/?page_size=100")
    live_count = resp.json().get("count", 0) if resp.status_code == 200 else "ERR"
    wb = student_workbooks[did]
    excel_rows = wb["Repositories"].max_row - 4
    match = (live_count == excel_rows)
    print(f"{did:<15} | {live_count:<12} | {excel_rows:<15} | {str(match):<6}")

print("\n(b) Submission Date (Excel IST) vs Newest tag_last_pushed (Live Docker Hub -> IST):")
print(f"{'Student':<12} | {'Repository':<22} | {'Excel Date (IST)':<18} | {'Live Newest Tag Push (IST)':<26} | {'Match?'}")
print("-" * 88)
for did in test_students:
    wb = student_workbooks[did]
    ws = wb["Repositories"]
    # Check first 3 repos (rows 5, 6, 7)
    for r_idx in range(5, min(8, ws.max_row + 1)):
        r_name = ws.cell(row=r_idx, column=2).value
        excel_date_val = ws.cell(row=r_idx, column=3).value
        excel_date_str = excel_date_val.strftime("%Y-%m-%d %H:%M") if isinstance(excel_date_val, datetime) else "Blank"

        # Fetch live tags for this repo
        tags_resp = hub_client.get(f"https://hub.docker.com/v2/repositories/{did}/{r_name}/tags?page_size=100")
        live_tags = tags_resp.json().get("results", []) if tags_resp.status_code == 200 else []
        pushed_list = [t.get("tag_last_pushed") for t in live_tags if t.get("tag_last_pushed")]
        if pushed_list:
            newest_utc = max(pushed_list)
            # Convert UTC to IST naive
            dt_utc = datetime.fromisoformat(newest_utc.replace("Z", "+00:00"))
            dt_ist = dt_utc.astimezone(IST)
            live_ist_str = dt_ist.strftime("%Y-%m-%d %H:%M")
        else:
            live_ist_str = "Blank"

        date_match = (excel_date_str == live_ist_str)
        print(f"{did:<12} | {r_name:<22} | {excel_date_str:<18} | {live_ist_str:<26} | {date_match}")

# -------------------------------------------------------------
# STEP 4: Download /api/export-all and inspect
# -------------------------------------------------------------
print("\n--- STEP 4: Download /api/export-all and inspect ---")
t0 = time.time()
r_all = client.get(f"{BASE_URL}/api/export-all")
dur = time.time() - t0
print(f"Status: {r_all.status_code} (took {dur:.2f}s)")
all_path = EXPORT_DIR / "All_Submissions_23CYS.xlsx"
all_path.write_bytes(r_all.content)

wb_all = load_workbook(all_path)
print(f"Combined sheet names: {wb_all.sheetnames}")

ws_all_sub = wb_all["All Submissions"]
print(f"\nFirst 5 rows of 'All Submissions':")
for r_i in range(1, 6):
    print(f"Row {r_i}: {[c.value for c in ws_all_sub[r_i]]}")

total_all_rows = ws_all_sub.max_row
data_rows = total_all_rows - 1
print(f"Total rows in 'All Submissions': {total_all_rows} (1 Header + {data_rows} data rows)")

ws_std_sum = wb_all["Student Summary"]
print(f"\n'Student Summary' total rows: {ws_std_sum.max_row} (1 Header + {ws_std_sum.max_row - 1} student rows)")
print(f"First 3 rows of 'Student Summary':")
for r_i in range(1, 4):
    print(f"Row {r_i}: {[c.value for c in ws_std_sum[r_i]]}")

# -------------------------------------------------------------
# STEP 5: Call /api/students/demo-student-01/preview
# -------------------------------------------------------------
print("\n--- STEP 5: Call /api/students/demo-student-01/preview ---")
r_prev = client.get(f"{BASE_URL}/api/students/demo-student-01/preview")
print(f"Status: {r_prev.status_code}")
prev_data = r_prev.json()
print("Top-level summary:")
for k in ["roll", "name", "docker_id", "total_repos", "total_pulls"]:
    print(f"  {k}: {prev_data.get(k)}")

print(f"\nFirst 3 repos in preview JSON:")
first_3_repos = prev_data.get("repos", [])[:3]
print(json.dumps(first_3_repos, indent=2))

# -------------------------------------------------------------
# STEP 7: Health check
# -------------------------------------------------------------
print("\n--- STEP 7: Health check ---")
r_h = client.get(f"{BASE_URL}/api/health")
print(f"Status: {r_h.status_code}, Body: {r_h.json()}")
assert r_h.status_code == 200
print("\nALL MANDATORY VERIFICATIONS 2 TO 7 FINISHED!")
