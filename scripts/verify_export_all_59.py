import io
import time
import httpx
from pathlib import Path
from openpyxl import load_workbook

client = httpx.Client(base_url="http://127.0.0.1:8000", timeout=300.0)

print("Uploading sample_roster.xlsx...")
with open("sample_roster.xlsx", "rb") as f:
    res = client.post("/api/roster", files={"file": ("sample_roster.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
assert res.status_code == 200
roster_id = res.json()["roster_id"]

print("Starting export-all for 59 students...")
res_start = client.post(f"/api/roster/{roster_id}/export-all/start")
assert res_start.status_code == 200
job_id = res_start.json()["job_id"]

t0 = time.time()
while True:
    res_job = client.get(f"/api/jobs/{job_id}")
    j = res_job.json()
    print(f"Status: {j['status']} | Done: {j['done']}/{j['total']} | Current: {j['current']}")
    if j["status"] in ("done", "failed"):
        break
    time.sleep(3)

elapsed = time.time() - t0
print(f"Export-all for 59 students completed in {elapsed:.1f}s")
assert j["status"] == "done"

res_dl = client.get(f"/api/jobs/{job_id}/download")
assert res_dl.status_code == 200
wb = load_workbook(io.BytesIO(res_dl.content))
print("Combined Sheets:", wb.sheetnames)
ws_summary = wb["Student Summary"]
print(f"Student Summary rows: {ws_summary.max_row} (expected 60: 1 header + 59 students)")
assert ws_summary.max_row == 60

ws_all = wb["All Submissions"]
print(f"All Submissions rows: {ws_all.max_row}")
assert ws_all.max_row >= 60
