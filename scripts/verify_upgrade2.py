import io
import json
import httpx
from pathlib import Path
from openpyxl import load_workbook, Workbook

BASE_URL = "http://127.0.0.1:8000"

def verify_all():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)

    print("==================================================")
    print("MANDATORY VERIFICATION STEP 2: Upload sample_roster.xlsx")
    print("==================================================")
    roster_path = Path("sample_roster.xlsx")
    assert roster_path.is_file(), "sample_roster.xlsx not found"
    with open(roster_path, "rb") as f:
        res = client.post("/api/roster", files={"file": ("sample_roster.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert res.status_code == 200, f"Upload failed: {res.text}"
    resp_roster = res.json()
    print("Status Code:", res.status_code)
    print("Response JSON:")
    print(json.dumps(resp_roster, indent=2))
    roster_59_id = resp_roster["roster_id"]
    assert resp_roster["students_loaded"] == 59
    assert len(resp_roster["skipped"]) == 0

    print("\n==================================================")
    print("MANDATORY VERIFICATION STEP 3: Upload sample_messy.xlsx")
    print("==================================================")
    messy_path = Path("sample_messy.xlsx")
    assert messy_path.is_file(), "sample_messy.xlsx not found"
    with open(messy_path, "rb") as f:
        res_m = client.post("/api/roster", files={"file": ("sample_messy.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert res_m.status_code == 200, f"Upload messy failed: {res_m.text}"
    resp_messy = res_m.json()
    print("Status Code:", res_m.status_code)
    print("Response JSON:")
    print(json.dumps(resp_messy, indent=2))
    roster_messy_id = resp_messy["roster_id"]
    assert resp_messy["students_loaded"] == 4
    assert len(resp_messy["skipped"]) == 3

    print("\n==================================================")
    print("MANDATORY VERIFICATION STEP 4: Preview & Single Export for demo-student-06")
    print("==================================================")
    # Preview
    res_prev = client.get(f"/api/roster/{roster_messy_id}/students/demo-student-06/preview")
    assert res_prev.status_code == 200, f"Preview failed: {res_prev.text}"
    prev_json = res_prev.json()
    print("Preview JSON:")
    print(json.dumps({
        "roll": prev_json["roll"],
        "name": prev_json["name"],
        "docker_id": prev_json["docker_id"],
        "total_repos": prev_json["total_repos"],
        "total_pulls": prev_json["total_pulls"],
        "repos_sample": prev_json["repos"][:2]
    }, indent=2))

    # Single Export
    res_exp = client.get(f"/api/roster/{roster_messy_id}/students/demo-student-06/export")
    assert res_exp.status_code == 200, f"Single export failed: {res_exp.text}"
    disp_header = res_exp.headers.get("content-disposition", "")
    print("Content-Disposition Header:", disp_header)
    assert "demo-student-06_" in disp_header
    assert not disp_header.startswith('attachment; filename="_')

    wb = load_workbook(io.BytesIO(res_exp.content))
    ws_repos = wb["Repositories"]
    row1_val = ws_repos.cell(row=1, column=1).value
    print("Repositories Sheet Row 1 Title:", row1_val)
    assert "-" in row1_val or "demo-student-06" in row1_val

    ws_sum = wb["Summary"]
    name_val = ws_sum.cell(row=3, column=2).value
    roll_val = ws_sum.cell(row=4, column=2).value
    print("Summary Sheet Student Name:", repr(name_val))
    print("Summary Sheet Roll Number:", repr(roll_val))
    assert name_val in ("'-", "-")
    assert roll_val in ("'-", "-")

    print("\n==================================================")
    print("MANDATORY VERIFICATION STEP 5: Formula Injection Protection")
    print("==================================================")
    # Student with formula-looking name is 'demo-student-03' in sample_messy
    res_demo-student-03 = client.get(f"/api/roster/{roster_messy_id}/students/demo-student-03/export")
    assert res_demo-student-03.status_code == 200
    wb_demo-student-03 = load_workbook(io.BytesIO(res_demo-student-03.content))
    ws_demo-student-03_sum = wb_demo-student-03["Summary"]
    formula_cell = ws_demo-student-03_sum.cell(row=3, column=2)
    print("Formula Name Cell Value:", repr(formula_cell.value))
    print("Formula Name Cell Data Type:", repr(formula_cell.data_type))
    assert formula_cell.data_type == 's'
    assert formula_cell.value.startswith("'")

    print("\n==================================================")
    print("MANDATORY VERIFICATION STEP 6: Roster Isolation")
    print("==================================================")
    # Upload an independent isolated roster with student 'octocat'
    wb_iso = Workbook()
    ws_iso = wb_iso.active
    ws_iso.title = "Students"
    ws_iso.append(["Roll No", "Name", "Docker ID"])
    ws_iso.append([999, "Isolated Student", "octocat"])
    buf_iso = io.BytesIO()
    wb_iso.save(buf_iso)
    buf_iso.seek(0)

    res_iso = client.post("/api/roster", files={"file": ("isolated.xlsx", buf_iso, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert res_iso.status_code == 200
    roster_iso_id = res_iso.json()["roster_id"]

    # 1. octocat exists in roster_iso_id but NOT in roster_59_id
    res_cross1 = client.get(f"/api/roster/{roster_59_id}/students/octocat/preview")
    print("Cross-Roster Lookup (octocat in 59-roster) Status:", res_cross1.status_code)
    print("Cross-Roster Detail:", res_cross1.json().get("detail"))
    assert res_cross1.status_code == 404

    # 2. demo-student-01 exists in roster_59_id but NOT in roster_iso_id
    res_cross2 = client.get(f"/api/roster/{roster_iso_id}/students/demo-student-01/preview")
    print("Cross-Roster Lookup (demo-student-01 in iso-roster) Status:", res_cross2.status_code)
    print("Cross-Roster Detail:", res_cross2.json().get("detail"))
    assert res_cross2.status_code == 404

    # 3. Non-existent or expired roster returns 404 with re-upload message
    res_expired = client.get("/api/roster/fake_expired_roster_id/students")
    print("Expired Roster Status:", res_expired.status_code)
    print("Expired Roster Detail:", res_expired.json().get("detail"))
    assert res_expired.status_code == 404
    assert "upload your student list again" in res_expired.json().get("detail", "").lower()

    print("\n==================================================")
    print("MANDATORY VERIFICATION STEP 7: Export-All Background Job")
    print("==================================================")
    import time
    # Test on sample_messy.xlsx (4 students)
    print("Starting export-all for sample_messy.xlsx (4 students)...")
    res_job_start = client.post(f"/api/roster/{roster_messy_id}/export-all/start")
    assert res_job_start.status_code == 200
    job_id = res_job_start.json()["job_id"]
    print(f"Export Job ID: {job_id}")

    t0 = time.time()
    while True:
        res_poll = client.get(f"/api/jobs/{job_id}")
        assert res_poll.status_code == 200
        j_data = res_poll.json()
        print(f"Poll Job: status={j_data['status']}, done={j_data['done']}/{j_data['total']}, current={j_data['current']}")
        if j_data["status"] in ("done", "failed"):
            break
        time.sleep(2)
    elapsed = time.time() - t0
    print(f"Job completed in {elapsed:.1f}s with status: {j_data['status']}")
    assert j_data["status"] == "done"

    # Download combined workbook
    res_dl = client.get(f"/api/jobs/{job_id}/download")
    assert res_dl.status_code == 200
    print("Combined Download Content-Disposition:", res_dl.headers.get("content-disposition"))
    wb_comb = load_workbook(io.BytesIO(res_dl.content))
    print("Combined Workbook Sheets:", wb_comb.sheetnames)
    assert "All Submissions" in wb_comb.sheetnames
    assert "Student Summary" in wb_comb.sheetnames
    ws_all = wb_comb["All Submissions"]
    print("All Submissions Max Rows:", ws_all.max_row)
    ws_stud = wb_comb["Student Summary"]
    print("Student Summary Max Rows (including header):", ws_stud.max_row)
    assert ws_stud.max_row == 5  # 1 header + 4 students

    print("\nALL VERIFICATIONS 2-7 SUCCEEDED!")

if __name__ == "__main__":
    verify_all()
