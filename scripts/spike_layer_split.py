"""Phase 1 spike: separate a Docker Hub image's own build steps from its base image's.

    python scripts/spike_layer_split.py

Hits the live Docker Hub API (public images only, never student data). The
findings are recorded in docs/plans/repo-scoring.md, "Spike results".

Approach tested ("official-base prefix match"):
1. Read the tag's layer list (Dockerfile instruction per step) from
   /v2/repositories/{ns}/{repo}/tags/{tag}/images. No pull quota is used.
2. Official images leave a signature ENV (PYTHON_VERSION, NGINX_VERSION, ...).
   The last one in the list names the most-derived official base family.
3. Fetch a few candidate tags of that official image (matching version and
   alpine/debian variant) and find the longest one whose step list is a prefix
   of ours, comparing instructions after normalising versions and hashes.
   Everything after that prefix is the image's own work.
4. No signature: strip only the OS root (alpine/debian/ubuntu/busybox rootfs
   and its CMD). No rootfs at all: FROM scratch, every step is its own.

Compared with the naive rule "own steps start after the last CMD/ENTRYPOINT
that is followed by more steps".
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
API = "https://hub.docker.com/v2"

FAMILIES = {
    "NGINX_VERSION": "nginx", "PYTHON_VERSION": "python", "NODE_VERSION": "node",
    "JAVA_VERSION": "eclipse-temurin", "GOLANG_VERSION": "golang", "PHP_VERSION": "php",
    "RUBY_VERSION": "ruby", "HTTPD_VERSION": "httpd", "TOMCAT_VERSION": "tomcat",
    "REDIS_VERSION": "redis", "PG_VERSION": "postgres", "MYSQL_VERSION": "mysql",
    "MONGO_VERSION": "mongo", "MARIADB_VERSION": "mariadb",
}
TERMINAL = ("CMD", "ENTRYPOINT")
_cache: dict[str, object] = {}
calls = 0


def get(url: str):
    global calls
    if url in _cache:
        return _cache[url]
    calls += 1
    req = urllib.request.Request(url, headers={"User-Agent": "faculty-toolkit-spike"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        data = {"_error": e.code}
    _cache[url] = data
    time.sleep(0.3)
    return data


def layers_for(repo: str, tag: str, arch: str = "amd64") -> list[str] | None:
    data = get(f"{API}/repositories/{repo}/tags/{urllib.parse.quote(tag)}/images")
    if not isinstance(data, list):
        return None
    img = next((i for i in data if i.get("os") == "linux" and i.get("architecture") == arch), None)
    if img is None:
        return None
    return [" ".join((l.get("instruction") or "").split()) for l in img.get("layers") or []]


def kind(ins: str) -> str:
    return ins.split(" ", 1)[0].upper() if ins else ""


def rootfs_kind(ins: str) -> str | None:
    s = ins.lower()
    if "alpine-minirootfs" in s:
        return "alpine"
    if s.startswith("# debian.sh"):
        return "debian"
    if "ubuntu" in s and ("umoci" in s or "add file:" in s):
        return "ubuntu"
    if s.startswith("busybox") or "busybox" in s[:40]:
        return "busybox"
    if re.fullmatch(r"add file:[0-9a-f]+ in /", s):
        return "rootfs"
    return None


def os_root_index(steps: list[str]) -> int | None:
    """Index of the OS root layer. Official ubuntu puts ARG/LABEL steps before it."""
    for i, s in enumerate(steps[:6]):
        if rootfs_kind(s):
            return i
        if kind(s) not in ("ARG", "LABEL"):
            return None
    return None


def norm(ins: str, first: bool = False) -> str:
    if rootfs_kind(ins):
        return "ROOTFS"  # any OS root matches any other; the variant is chosen by os_hint
    s = ins.replace(" # buildkit", "")
    if s.startswith("/bin/sh -c "):  # classic builder omits the RUN keyword
        s = "RUN " + s
    s = re.sub(r"^RUN \|\d+ (?:\S+=\S* )*", "RUN ", s)   # ARG prefix BuildKit adds
    s = re.sub(r"[0-9a-f]{7,}", "H", s)
    s = re.sub(r"\d+", "9", s)
    return s


def prefix_len(base: list[str], img: list[str]) -> int:
    """Length of base if it is a (normalised) prefix of img, else 0."""
    if len(base) > len(img):
        return 0
    for i, (a, b) in enumerate(zip(base, img)):
        if norm(a, i == 0) != norm(b, i == 0):
            return 0
    return len(base)


def signature(steps: list[str]):
    found = None
    for i, s in enumerate(steps):
        if kind(s) != "ENV":
            continue
        for key, fam in FAMILIES.items():
            m = re.search(rf"\b{key}=(\S+)", s)
            if m:
                found = (i, fam, m.group(1))
    return found


def os_hint(steps: list[str]):
    head = " ".join(steps[:2]).lower()
    alpine = "alpine-minirootfs" in head
    r = os_root_index(steps)
    if not alpine and r is not None and rootfs_kind(steps[r]) == "rootfs":  # generic ADD file:... root
        alpine = any("apk add" in s for s in steps)
    m = re.search(r"'(bookworm|trixie|bullseye|buster|forky)'", head)
    return alpine, (m.group(1) if m else None)


def candidate_tags(fam: str, version: str, steps: list[str]) -> list[str]:
    alpine, codename = os_hint(steps)
    num = re.search(r"\d+(?:\.\d+)*", version)
    parts = num.group(0).split(".") if num else []
    filters = [".".join(parts[:n]) for n in (len(parts), 2, 1) if 0 < n <= len(parts)]
    for f in dict.fromkeys(filters):
        data = get(f"{API}/repositories/library/{fam}/tags?page_size=100&name={urllib.parse.quote(f)}")
        names = [t["name"] for t in (data.get("results") or [])] if isinstance(data, dict) else []
        names = [n for n in names if not re.search(r"windows|nanoserver|servercore", n)]
        if alpine:
            names = [n for n in names if "alpine" in n]
        else:
            names = [n for n in names if "alpine" not in n]
            if codename:
                names = [n for n in names if codename in n or not re.search(r"bookworm|trixie|bullseye|buster|forky", n)]
        if names:
            return sorted(set(names), key=lambda n: (len(n), n))[:8]
    return []


def split_official(steps: list[str]):
    sig = signature(steps)
    if sig:
        _, fam, ver = sig
        best = (0, None)
        for tag in candidate_tags(fam, ver, steps):
            base = layers_for(f"library/{fam}", tag)
            if base:
                n = prefix_len(base, steps)
                if n > best[0]:
                    best = (n, f"{fam}:{tag}")
        if best[1]:
            return best[0], best[1], "high"
    # OS root only
    r = os_root_index(steps)
    rk = rootfs_kind(steps[r]) if r is not None else None
    if rk:
        n = r + 1
        if rk == "ubuntu":
            while n < len(steps) and "umoci" in steps[n]:
                n += 1
        if n < len(steps) and kind(steps[n]) == "CMD":
            n += 1
        return n, f"{rk} root", "medium" if not sig else "low"
    return 0, "scratch", "high" if not sig else "low"


def split_naive(steps: list[str]) -> int:
    last = 0
    for i, s in enumerate(steps[:-1]):
        if kind(s) in TERMINAL and kind(steps[i + 1]) not in TERMINAL:
            last = i + 1
    return last


# (image, expected own-start index, note). Expected values come from reading
# each image's step list against its published Dockerfile chain.
CASES = [
    ("library/python:3.11-slim", None, "pure re-tag of an official image: expect no own steps"),
    ("library/nginx:alpine", None, "pure re-tag; nginx:alpine has steps after its CMD"),
    ("swaggerapi/swagger-ui:latest", 20, "FROM nginx:alpine (older 1.31.9 build)"),
    ("tiangolo/uvicorn-gunicorn-fastapi:python3.11", 13, "FROM python:3.11 via tiangolo base"),
    ("nodered/node-red:latest", 9, "FROM node:alpine, multi-stage"),
    ("traefik/whoami:latest", 0, "FROM scratch"),
    ("library/wordpress:latest", "php", "FROM php:*-apache"),
    ("library/ghost:latest", "node", "FROM node:*-bookworm-slim"),
    ("library/mongo-express:latest", "node", "FROM node:*-alpine"),
    ("mher/flower:latest", "python", "FROM python:*"),
    ("prom/prometheus:latest", 1, "FROM prometheus busybox (non-official base)"),
    ("library/ubuntu:22.04", None, "pure re-tag of ubuntu (ARG/LABEL before the root layer)"),
    ("library/alpine:3.20", None, "pure re-tag of alpine"),
    ("library/debian:bookworm-slim", None, "pure re-tag of debian"),
    ("ollama/ollama:latest", 5, "FROM ubuntu:24.04"),
]


def main():
    for ref, expected, note in CASES:
        repo, tag = ref.split(":")
        steps = layers_for(repo, tag)
        if not steps:
            print(f"\n{ref}: no linux/amd64 layers")
            continue
        own, base, conf = split_official(steps)
        naive = split_naive(steps)
        exp = len(steps) if expected is None else expected
        print(f"\n{ref}  ({len(steps)} steps) - {note}")
        print(f"  prefix-match: own from {own:>2}  base={base} [{conf}]")
        print(f"  naive CMD   : own from {naive:>2}")
        if isinstance(exp, int):
            print(f"  expected    : own from {exp:>2}  -> prefix {'OK' if own == exp else 'WRONG'}, naive {'OK' if naive == exp else 'WRONG'}")
        else:
            print(f"  expected    : base family {exp!r}; check the boundary below by eye")
        for i in range(max(0, own - 2), min(len(steps), own + 3)):
            mark = ">>" if i == own else "  "
            print(f"   {mark}{i:2} {steps[i][:105]}")
    print(f"\nHub API calls made: {calls}")


if __name__ == "__main__":
    main()
