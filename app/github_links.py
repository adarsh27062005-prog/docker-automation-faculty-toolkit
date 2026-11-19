import csv
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Regex pattern matching GitHub repositories
RAW_GITHUB_REGEX = re.compile(r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

def clean_github_url(raw_url: str) -> Optional[str]:
    r"""
    Cleans a discovered GitHub URL:
    - Strips leading and trailing punctuation (.,;:!?()<>[]"'\`)
    - Strips .git suffix
    - Strips path suffixes beyond owner/repo (e.g., /tree/..., /blob/...)
    - Normalizes scheme to https
    """
    if not raw_url:
        return None
    url = raw_url.strip()
    
    # Strip common leading and trailing punctuation/enclosing brackets
    url = url.strip(".,;:!?()\"'<>[]`")
    
    # Match the owner/repo pattern
    m = re.match(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", url)
    if not m:
        return None
    
    owner, repo = m.group(1), m.group(2)
    # Strip .git if present at the end of repo
    if repo.endswith(".git"):
        repo = repo[:-4]
    
    # Strip any trailing punctuation that might remain on repo name
    repo = repo.strip(".,;:!?()\"'<>[]`")
    if not owner or not repo:
        return None
        
    return f"https://github.com/{owner}/{repo}"

def load_overrides(file_path: Path = settings.OVERRIDES_FILE) -> Dict[str, str]:
    """
    Loads manual GitHub URL overrides from CSV (docker_id,github_url).
    Returns mapping with lowercase docker_id as key.
    """
    overrides: Dict[str, str] = {}
    if not file_path.is_file():
        return overrides

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for row in reader:
                if len(row) >= 2:
                    did = row[0].strip().lower()
                    gh = row[1].strip()
                    cleaned = clean_github_url(gh)
                    if did and cleaned:
                        overrides[did] = cleaned
    except Exception as e:
        logger.warning("Could not read overrides file %s: %s", file_path, e)

    return overrides

def extract_github_urls_from_text(text: Optional[str]) -> List[str]:
    """Finds and cleans all distinct GitHub URLs from a text string."""
    if not text:
        return []
    matches = RAW_GITHUB_REGEX.findall(text)
    cleaned_urls: List[str] = []
    seen = set()
    for m in matches:
        cleaned = clean_github_url(m)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            cleaned_urls.append(cleaned)
    return cleaned_urls

def discover_github_links(
    docker_id: str,
    full_description: Optional[str] = None,
    description: Optional[str] = None,
    overrides: Optional[Dict[str, str]] = None,
    use_source_fields: bool = False,
    source: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """
    Discovers GitHub links following Section 6.2 priority:
    1. overrides.csv (docker_id -> github_url)
    2. (Conditional) source fields if enabled
    3. full_description (README)
    4. description
    5. Returns ("Not found", []) if nothing found.
    Returns (primary_url, list_of_other_urls).
    """
    # 1. Check overrides
    if overrides is None:
        overrides = load_overrides()
    
    did_lower = docker_id.lower().strip()
    if did_lower in overrides:
        return (overrides[did_lower], [])

    # 2. Check source if enabled
    if use_source_fields and source:
        source_links = extract_github_urls_from_text(source)
        if source_links:
            return (source_links[0], source_links[1:])

    # 3. Check full_description (README)
    readme_links = extract_github_urls_from_text(full_description)
    if readme_links:
        return (readme_links[0], readme_links[1:])

    # 4. Check description
    desc_links = extract_github_urls_from_text(description)
    if desc_links:
        return (desc_links[0], desc_links[1:])

    return ("Not found", [])

async def enrich_github_repo(
    github_url: str,
    client: httpx.AsyncClient,
    token: Optional[str] = settings.GITHUB_TOKEN,
) -> Optional[dict]:
    """
    Enriches a discovered GitHub URL via GitHub REST API if GITHUB_TOKEN is configured.
    Returns metadata dict: created_at, pushed_at, stars, forks, is_fork.
    """
    if not token or not github_url or github_url == "Not found":
        return None

    m = re.match(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", github_url)
    if not m:
        return None

    owner, repo = m.group(1), m.group(2)
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "DockerSubmissionApp/1.0",
    }

    try:
        resp = await client.get(api_url, headers=headers, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "created_at": data.get("created_at"),
                "pushed_at": data.get("pushed_at"),
                "stars": data.get("stargazers_count", 0),
                "forks": data.get("forks_count", 0),
                "is_fork": data.get("fork", False),
            }
        else:
            logger.debug("GitHub API returned %d for %s", resp.status_code, api_url)
            return None
    except Exception as e:
        logger.debug("Failed to enrich GitHub repo %s: %s", github_url, e)
        return None
