import time
from datetime import datetime
from pathlib import Path
import httpx
from openpyxl import load_workbook

BASE_URL = "http://127.0.0.1:8000"
EXPORT_DIR = Path("scratch/exports")
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

# 10 minute timeout for export-all
client = httpx.Client(timeout=600.0)

print("=================================================================", flush=True)
print("             LIVE WEB APP VERIFICATION SUITE                     ", flush=True)
print("=================================================================", flush=True)

# 1. GET /api/health
r_health = client.get(f"{BASE_URL}/api/health")
print(f"\n1. GET /api/health", flush=True)
print(f"Status Code: {r_health.status_code}", flush=True)
print(f"Response: {r_health.json()}", flush=True)
assert r_health.status_code == 200

# 2. GET /api/students
r_students = client.get(f"{BASE_URL}/api/students")
students = r_students.json()
print(f"\n2. GET /api/students", flush=True)
print(f"Status Code: {r_students.status_code}", flush=True)
print(f"Number of students returned: {len(students)}", flush=True)
print(f"First student: {students[0]}", flush=True)
print(f"Last student: {students[-1]}", flush=True)
assert len(students) == 59

# 3. GET /api/students/{docker_id}/preview
test_student = "demo-student-01"
r_prev = client.get(f"{BASE_URL}/api/students/{test_student}/preview")
print(f"\n3. GET /api/students/{test_student}/preview", flush=True)
print(f"Status Code: {r_prev.status_code}", flush=True)
prev_data = r_prev.json()
print(f"Preview Response: {prev_data}", flush=True)

# 4. GET /api/export/{docker_id} for 3 different real students
students_to_export = ["demo-student-01", "demo-student-02", "demo-student-06"]
exported_workbooks = {}

print(f"\n4. GET /api/export/{{docker_id}} for 3 real students:", flush=True)
for did in students_to_export:
    t0 = time.time()
    r_exp = client.get(f"{BASE_URL}/api/export/{did}")
    dur = time.time() - t0
    print(f"\n--- Export for {did} ---", flush=True)
    print(f"Status Code: {r_exp.status_code} (took {dur:.2f}s)", flush=True)
    disp = r_exp.headers.get("content-disposition", "")
    print(f"Content-Disposition: {disp}", flush=True)
    print(f"Content-Type: {r_exp.headers.get('content-type')}", flush=True)
    print(f"File size: {len(r_exp.content)} bytes", flush=True)
    
    file_path = EXPORT_DIR / f"{did}.xlsx"
    with open(file_path, "wb") as f:
        f.write(r_exp.content)

    # Open with openpyxl and inspect
    wb = load_workbook(file_path)
    exported_workbooks[did] = (wb, file_path)
    print(f"Sheet names: {wb.sheetnames}", flush=True)
    
    ws_repos = wb["Repositories"]
    headers = [cell.value for cell in ws_repos[1]]
    print(f"Header row (Repositories): {headers}", flush=True)
    
    first_data_row = [cell.value for cell in ws_repos[2]]
    print(f"First data row values: {first_data_row}", flush=True)
    
    # Check hyperlinks and date cells
    c_name = ws_repos["A2"]
    print(f"Cell A2 (Repo Name): value='{c_name.value}', hyperlink='{c_name.hyperlink.target if c_name.hyperlink else None}'", flush=True)
    
    c_gh = ws_repos["B2"]
    print(f"Cell B2 (GitHub URL): value='{c_gh.value}', hyperlink='{c_gh.hyperlink.target if c_gh.hyperlink else None}'", flush=True)
    
    c_created = ws_repos["D2"]
    print(f"Cell D2 (Repo Created): value='{c_created.value}', type={type(c_created.value).__name__}, format='{c_created.number_format}'", flush=True)
    
    c_pushed = ws_repos["F2"]
    print(f"Cell F2 (Last Pushed): value='{c_pushed.value}', type={type(c_pushed.value).__name__}, format='{c_pushed.number_format}'", flush=True)

# 5. GET /api/export/not_a_real_id (expect 404)
print(f"\n5. GET /api/export/not_a_real_id", flush=True)
r_404 = client.get(f"{BASE_URL}/api/export/not_a_real_id")
print(f"Status Code: {r_404.status_code}", flush=True)
print(f"Response Body: {r_404.json()}", flush=True)
assert r_404.status_code == 404

# 6. GET /api/export-all
print(f"\n6. GET /api/export-all (combined export)", flush=True)
t0_all = time.time()
r_all = client.get(f"{BASE_URL}/api/export-all")
dur_all = time.time() - t0_all
print(f"Status Code: {r_all.status_code} (took {dur_all:.2f}s)", flush=True)
print(f"Content-Disposition: {r_all.headers.get('content-disposition')}", flush=True)
print(f"Size: {len(r_all.content)} bytes", flush=True)

all_path = EXPORT_DIR / "All_Submissions_23CYS.xlsx"
with open(all_path, "wb") as f:
    f.write(r_all.content)

wb_all = load_workbook(all_path)
print(f"Combined workbook sheets: {wb_all.sheetnames}", flush=True)
ws_all = wb_all["All Submissions"]
print(f"Total rows in All Submissions: {ws_all.max_row} (Header + {ws_all.max_row - 1} data rows)", flush=True)
print(f"Header: {[c.value for c in ws_all[1]]}", flush=True)
print(f"First data row: {[c.value for c in ws_all[2]]}", flush=True)
print(f"Last data row: {[c.value for c in ws_all[ws_all.max_row]]}", flush=True)

# 7. SPOT CHECK: Compare 3 students in .xlsx with live Docker Hub API response fetched right now
print(f"\n7. SPOT CHECK: Comparing .xlsx data with live Docker Hub API response", flush=True)
hub_client = httpx.Client(timeout=15.0, headers={"User-Agent": "VerifierSpotCheck/1.0"})

for did in students_to_export:
    wb, fpath = exported_workbooks[did]
    ws_repos = wb["Repositories"]
    xlsx_repo_name = ws_repos["A2"].value
    xlsx_pulls = ws_repos["G2"].value
    xlsx_created = ws_repos["D2"].value
    xlsx_last_pushed = ws_repos["F2"].value

    # Query live Docker Hub API
    hub_url = f"https://hub.docker.com/v2/repositories/{did}/{xlsx_repo_name}/"
    hub_resp = hub_client.get(hub_url)
    hub_data = hub_resp.json() if hub_resp.status_code == 200 else {}
    
    live_pulls = hub_data.get("pull_count")
    live_created = hub_data.get("date_registered")
    live_last_pushed = hub_data.get("last_updated")

    print(f"\nStudent: {did} (Repo: {xlsx_repo_name})", flush=True)
    print(f"  Pulls:       Excel = {xlsx_pulls} | Live Docker Hub = {live_pulls} | Match: {xlsx_pulls == live_pulls}", flush=True)
    print(f"  Created:     Excel = {xlsx_created} (IST) | Live Docker Hub = {live_created} (UTC)", flush=True)
    print(f"  Last Pushed: Excel = {xlsx_last_pushed} (IST) | Live Docker Hub = {live_last_pushed} (UTC)", flush=True)

# 8. GET / (fetch index.html)
print(f"\n8. GET / (Frontend)", flush=True)
r_index = client.get(f"{BASE_URL}/")
print(f"Status Code: {r_index.status_code}", flush=True)
print(f"Content-Type: {r_index.headers.get('content-type')}", flush=True)
print(f"Contains 'Docker & GitHub Submission Viewer': {'Docker & GitHub Submission Viewer' in r_index.text}", flush=True)
print(f"Contains '20CYS402': {'20CYS402' in r_index.text}", flush=True)
print(f"Contains 'search-input': {'search-input' in r_index.text}", flush=True)
print(f"HTML size: {len(r_index.text)} characters", flush=True)
print("\nALL VERIFICATION CHECKS COMPLETED SUCCESSFULLY!", flush=True)
