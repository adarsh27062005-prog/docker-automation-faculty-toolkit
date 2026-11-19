import asyncio
import csv
import json
import os
import re
import sys
from pathlib import Path
import httpx

DOCKER_ID_MD = Path("docker_id.md")
OUTPUT_DIR = Path("scripts/survey_output")
JSON_OUTPUT = OUTPUT_DIR / "survey.json"
CSV_OUTPUT = OUTPUT_DIR / "survey.csv"

GITHUB_REGEX = re.compile(r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
GITHUB_MENTION = re.compile(r"github", re.IGNORECASE)

SEMAPHORE = asyncio.Semaphore(3)
RATE_LIMIT_DELAY = 0.05  # seconds

def parse_docker_id_md(file_path: Path):
    students = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("| DEMO.U4CYS23"):
                continue
            parts = [p.strip() for p in line.split("|")[1:-1]]
            if len(parts) >= 3:
                roll = parts[0]
                name = parts[1]
                docker_cell = parts[2]
                m = re.search(r"\[(.*?)\]", docker_cell)
                docker_id = m.group(1).strip() if m else docker_cell.strip()
                students.append({
                    "roll": roll,
                    "name": name,
                    "docker_id": docker_id,
                    "docker_id_lower": docker_id.lower()
                })
    return students

async def fetch_with_retry(client: httpx.AsyncClient, url: str, max_retries=3):
    delay = 1.0
    for attempt in range(max_retries):
        async with SEMAPHORE:
            await asyncio.sleep(RATE_LIMIT_DELAY)
            try:
                resp = await client.get(url)
                if resp.status_code == 429:
                    print(f"[429 Rate Limit] {url} - waiting {delay}s (attempt {attempt+1}/{max_retries})")
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                return resp
            except (httpx.RequestError, httpx.TimeoutException) as e:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(delay)
                delay *= 2
    return None

def check_unmatched_github(text: str, matched_urls: list):
    if not text:
        return []
    unmatched = []
    # Find all occurrences of word github
    if GITHUB_MENTION.search(text):
        # Check if the text has mentions that aren't covered by matched_urls
        # Break into lines or sentences containing github
        for line in text.splitlines():
            if GITHUB_MENTION.search(line):
                # If no matched URL is in this line, record it
                if not any(u in line for u in matched_urls):
                    unmatched.append(line.strip())
    return unmatched

async def survey_student(client: httpx.AsyncClient, student: dict):
    did_lower = student["docker_id_lower"]
    
    # 1. Check user status
    user_url = f"https://hub.docker.com/v2/users/{did_lower}/"
    user_resp = await fetch_with_retry(client, user_url)
    user_status = user_resp.status_code if user_resp else 500
    user_exists = (user_status == 200)

    # 2. Get user repos
    repos_url = f"https://hub.docker.com/v2/repositories/{did_lower}/?page_size=100"
    repo_list_resp = await fetch_with_retry(client, repos_url)
    
    repos_data = []
    all_raw_repos = []
    
    if repo_list_resp and repo_list_resp.status_code == 200:
        page_data = repo_list_resp.json()
        all_raw_repos.extend(page_data.get("results", []))
        next_page = page_data.get("next")
        while next_page:
            next_resp = await fetch_with_retry(client, next_page)
            if next_resp and next_resp.status_code == 200:
                next_data = next_resp.json()
                all_raw_repos.extend(next_data.get("results", []))
                next_page = next_data.get("next")
            else:
                break

    for r_item in all_raw_repos:
        rname = r_item.get("name")
        pull_count = r_item.get("pull_count", 0)
        desc = r_item.get("description") or ""
        source_field = str(r_item.get("source") or "")
        is_automated_field = str(r_item.get("is_automated", ""))
        
        # Check single repo endpoint for full_description
        single_url = f"https://hub.docker.com/v2/repositories/{did_lower}/{rname}/"
        single_resp = await fetch_with_retry(client, single_url)
        single_data = single_resp.json() if single_resp and single_resp.status_code == 200 else {}
        
        has_full_desc = "full_description" in single_data and bool(single_data.get("full_description"))
        full_desc = single_data.get("full_description") or ""
        
        # Check tags
        tags_url = f"https://hub.docker.com/v2/repositories/{did_lower}/{rname}/tags?page_size=100"
        tags_resp = await fetch_with_retry(client, tags_url)
        all_tags = []
        if tags_resp and tags_resp.status_code == 200:
            t_data = tags_resp.json()
            all_tags.extend(t_data.get("results", []))
            next_t_page = t_data.get("next")
            while next_t_page:
                next_t_resp = await fetch_with_retry(client, next_t_page)
                if next_t_resp and next_t_resp.status_code == 200:
                    nt_data = next_t_resp.json()
                    all_tags.extend(nt_data.get("results", []))
                    next_t_page = nt_data.get("next")
                else:
                    break
        
        tag_count = len(all_tags)
        pushed_dates = [t.get("tag_last_pushed") for t in all_tags if t.get("tag_last_pushed")]
        earliest_tag_pushed = min(pushed_dates) if pushed_dates else None

        # GitHub URL extraction
        gh_desc = list(dict.fromkeys(GITHUB_REGEX.findall(desc)))
        gh_full_desc = list(dict.fromkeys(GITHUB_REGEX.findall(full_desc)))
        gh_source = list(dict.fromkeys(GITHUB_REGEX.findall(source_field)))
        
        all_matched_gh = list(set(gh_desc + gh_full_desc + gh_source))
        
        # Unmatched github mentions
        unmatched_desc = check_unmatched_github(desc, all_matched_gh)
        unmatched_full = check_unmatched_github(full_desc, all_matched_gh)
        unmatched_source = check_unmatched_github(source_field, all_matched_gh)
        
        unmatched_all = unmatched_desc + unmatched_full + unmatched_source

        repos_data.append({
            "name": rname,
            "pull_count": pull_count,
            "has_description": bool(desc.strip()),
            "description": desc,
            "has_full_description": has_full_desc,
            "full_description_len": len(full_desc) if full_desc else 0,
            "github_urls_desc": gh_desc,
            "github_urls_full_desc": gh_full_desc,
            "source_field": source_field,
            "is_automated": is_automated_field,
            "github_in_source": gh_source,
            "tag_count": tag_count,
            "earliest_tag_last_pushed": earliest_tag_pushed,
            "unmatched_github_mentions": unmatched_all
        })

    return {
        "roll": student["roll"],
        "name": student["name"],
        "docker_id": student["docker_id"],
        "docker_id_lower": did_lower,
        "user_http_status": user_status,
        "user_exists": user_exists,
        "repo_count": len(repos_data),
        "repos": repos_data
    }

async def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    students = parse_docker_id_md(DOCKER_ID_MD)
    print(f"Loaded {len(students)} students from {DOCKER_ID_MD}")
    
    timeout = httpx.Timeout(15.0, connect=10.0)
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
    headers = {"User-Agent": "SurveyScript/1.0"}
    
    async with httpx.AsyncClient(timeout=timeout, limits=limits, headers=headers) as client:
        # Run across all students in batches of 3
        results = []
        for i in range(0, len(students), 3):
            batch = students[i:i+3]
            print(f"Processing batch {i+1}-{min(i+3, len(students))}/{len(students)}...")
            batch_tasks = [survey_student(client, s) for s in batch]
            batch_results = await asyncio.gather(*batch_tasks)
            results.extend(batch_results)

    # Save JSON output
    with open(JSON_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"Saved raw JSON to {JSON_OUTPUT}")

    # Save CSV output
    csv_rows = []
    for s in results:
        if s["repo_count"] == 0:
            csv_rows.append({
                "roll": s["roll"],
                "name": s["name"],
                "docker_id": s["docker_id"],
                "docker_id_lower": s["docker_id_lower"],
                "user_status": s["user_http_status"],
                "repo_count": 0,
                "repo_name": "",
                "pull_count": 0,
                "has_description": False,
                "has_full_description": False,
                "github_in_desc": "",
                "github_in_full_desc": "",
                "tag_count": 0,
                "earliest_tag_pushed": "",
                "unmatched_github_notes": ""
            })
        else:
            for r in s["repos"]:
                csv_rows.append({
                    "roll": s["roll"],
                    "name": s["name"],
                    "docker_id": s["docker_id"],
                    "docker_id_lower": s["docker_id_lower"],
                    "user_status": s["user_http_status"],
                    "repo_count": s["repo_count"],
                    "repo_name": r["name"],
                    "pull_count": r["pull_count"],
                    "has_description": r["has_description"],
                    "has_full_description": r["has_full_description"],
                    "github_in_desc": "; ".join(r["github_urls_desc"]),
                    "github_in_full_desc": "; ".join(r["github_urls_full_desc"]),
                    "tag_count": r["tag_count"],
                    "earliest_tag_pushed": r["earliest_tag_last_pushed"] or "",
                    "unmatched_github_notes": "; ".join(r["unmatched_github_mentions"])
                })

    with open(CSV_OUTPUT, "w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "roll", "name", "docker_id", "docker_id_lower", "user_status", "repo_count",
            "repo_name", "pull_count", "has_description", "has_full_description",
            "github_in_desc", "github_in_full_desc", "tag_count", "earliest_tag_pushed",
            "unmatched_github_notes"
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)
    print(f"Saved flat summary CSV to {CSV_OUTPUT}")

    # Compute and print survey summary metrics
    total_students = len(results)
    users_found = sum(1 for s in results if s["user_exists"])
    users_not_found = sum(1 for s in results if not s["user_exists"])
    users_zero_repos = sum(1 for s in results if s["user_exists"] and s["repo_count"] == 0)
    total_repos = sum(s["repo_count"] for s in results)
    
    repos_gh_desc = 0
    repos_gh_full = 0
    repos_gh_neither = 0
    source_gh_found = []
    is_automated_vals = set()
    students_no_gh = []
    all_unmatched = []

    for s in results:
        student_has_any_gh = False
        for r in s["repos"]:
            is_automated_vals.add(r["is_automated"])
            if r["github_in_source"]:
                source_gh_found.append((s["docker_id"], r["name"], r["github_in_source"]))
            in_d = len(r["github_urls_desc"]) > 0
            in_f = len(r["github_urls_full_desc"]) > 0
            if in_d:
                repos_gh_desc += 1
            if in_f:
                repos_gh_full += 1
            if not in_d and not in_f:
                repos_gh_neither += 1
            if in_d or in_f:
                student_has_any_gh = True
            if r["unmatched_github_mentions"]:
                all_unmatched.append({
                    "student": s["docker_id"],
                    "repo": r["name"],
                    "unmatched": r["unmatched_github_mentions"]
                })
        if not student_has_any_gh:
            students_no_gh.append(f"{s['roll']} ({s['name']} - {s['docker_id']}, repos: {s['repo_count']})")

    print("\n================== SURVEY SUMMARY METRICS ==================")
    print(f"Total students in docker_id.md: {total_students}")
    print(f"Users found (HTTP 200 on /v2/users/): {users_found}")
    print(f"Users not found (HTTP 404 on /v2/users/): {users_not_found}")
    print(f"Users with zero repos: {users_zero_repos}")
    print(f"Total repos discovered: {total_repos}")
    print(f"Repos with GitHub URL in description: {repos_gh_desc}")
    print(f"Repos with GitHub URL in full_description: {repos_gh_full}")
    print(f"Repos with GitHub URL in neither: {repos_gh_neither}")
    print(f"Did source ever have a GitHub link? {source_gh_found if source_gh_found else 'Never'}")
    print(f"Values seen for is_automated: {is_automated_vals}")
    print(f"Students with NO GitHub URL found anywhere: {len(students_no_gh)}")
    print(f"Unmatched 'github' text cases count: {len(all_unmatched)}")

if __name__ == "__main__":
    asyncio.run(main())
