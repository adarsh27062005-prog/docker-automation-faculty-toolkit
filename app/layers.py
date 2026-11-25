"""Image step lists from Docker Hub, and separating a student's own steps from the base image.

Docker Hub's tag images endpoint lists every step (layer) of an image with its
Dockerfile instruction. That list starts with the base image's steps, so before
judging "how was this image built" we strip the base. Method (chosen in the
Phase 1 spike, see docs/plans/repo-scoring.md):

1. Official images leave a signature ENV (PYTHON_VERSION, NGINX_VERSION, ...).
   The last one names the most-derived official base family.
2. Candidate tags of that official image (matching version and alpine/debian
   variant) are fetched, and the longest one whose normalised steps are a prefix
   of ours is the base.
3. No signature: strip only the OS root (alpine/debian/ubuntu/busybox).
   No root at all: FROM scratch, every step is the student's own.

Everything here is pure except `split_base`, which takes the two fetch
functions as arguments so tests can run offline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Sequence

FAMILIES = {
    "NGINX_VERSION": "nginx", "PYTHON_VERSION": "python", "NODE_VERSION": "node",
    "JAVA_VERSION": "eclipse-temurin", "GOLANG_VERSION": "golang", "PHP_VERSION": "php",
    "RUBY_VERSION": "ruby", "HTTPD_VERSION": "httpd", "TOMCAT_VERSION": "tomcat",
    "REDIS_VERSION": "redis", "PG_VERSION": "postgres", "MYSQL_VERSION": "mysql",
    "MONGO_VERSION": "mongo", "MARIADB_VERSION": "mariadb",
}
DEBIAN_CODENAMES = ("bookworm", "trixie", "bullseye", "buster", "forky")
MAX_CANDIDATES = 8


@dataclass
class Step:
    instruction: str
    size: int = 0
    digest: Optional[str] = None

    @property
    def kind(self) -> str:
        return step_kind(self.instruction)


@dataclass
class BaseSplit:
    own_start: int
    base: str          # e.g. "python:3.11.18-slim", "alpine root", "scratch"
    confidence: str    # "high" | "medium" | "low"


def clean_instruction(raw: Optional[str]) -> str:
    return " ".join((raw or "").split())


def steps_from_layers(layers: Optional[Sequence[dict]]) -> List[Step]:
    return [
        Step(instruction=clean_instruction(l.get("instruction")), size=int(l.get("size") or 0), digest=l.get("digest"))
        for l in (layers or [])
    ]


def step_kind(ins: str) -> str:
    """Dockerfile keyword of a step. Classic-builder RUN steps lack the keyword."""
    if not ins:
        return ""
    if ins.startswith("/bin/sh -c ") or ins.startswith("|"):
        return "RUN"
    word = ins.split(" ", 1)[0].upper()
    return "LABEL" if word == "MAINTAINER" else word


def pick_image(images: Optional[Sequence[dict]]) -> Optional[dict]:
    """Prefer linux/amd64, else the first real platform (skips unknown/unknown attestations)."""
    real = [i for i in (images or []) if i.get("os") not in (None, "", "unknown") and i.get("architecture") not in (None, "", "unknown")]
    for i in real:
        if i.get("os") == "linux" and i.get("architecture") == "amd64":
            return i
    return real[0] if real else None


def platforms_of(images: Optional[Sequence[dict]]) -> List[str]:
    out = []
    for i in images or []:
        os_, arch = i.get("os"), i.get("architecture")
        if os_ in (None, "", "unknown") or arch in (None, "", "unknown"):
            continue
        p = f"{os_}/{arch}" + (f"/{i['variant']}" if i.get("variant") else "")
        if p not in out:
            out.append(p)
    return out


def rootfs_kind(ins: str) -> Optional[str]:
    s = ins.lower()
    if "alpine-minirootfs" in s:
        return "alpine"
    if s.startswith("# debian.sh"):
        return "debian"
    if "ubuntu" in s and ("umoci" in s or "add file:" in s):
        return "ubuntu"
    if "busybox" in s[:40]:
        return "busybox"
    if re.fullmatch(r"add file:[0-9a-f]+ in /", s):
        return "rootfs"
    return None


def os_root_index(steps: Sequence[str]) -> Optional[int]:
    """Index of the OS root layer. Official ubuntu puts ARG/LABEL steps before it."""
    for i, s in enumerate(steps[:6]):
        if rootfs_kind(s):
            return i
        if step_kind(s) not in ("ARG", "LABEL"):
            return None
    return None


def normalise(ins: str) -> str:
    if rootfs_kind(ins):
        return "ROOTFS"
    s = ins.replace(" # buildkit", "")
    if s.startswith("/bin/sh -c "):
        s = "RUN " + s
    s = re.sub(r"^RUN \|\d+ (?:\S+=\S* )*", "RUN ", s)
    s = re.sub(r"[0-9a-f]{7,}", "H", s)
    return re.sub(r"\d+", "9", s)


def prefix_len(base: Sequence[str], img: Sequence[str]) -> int:
    """len(base) if base is a (normalised) prefix of img, else 0."""
    if not base or len(base) > len(img):
        return 0
    for a, b in zip(base, img):
        if normalise(a) != normalise(b):
            return 0
    return len(base)


def signature(steps: Sequence[str]):
    """(index, official family, version) of the last official-image signature ENV, or None."""
    found = None
    for i, s in enumerate(steps):
        if step_kind(s) != "ENV":
            continue
        for key, fam in FAMILIES.items():
            m = re.search(rf"\b{key}=(\S+)", s)
            if m:
                found = (i, fam, m.group(1))
    return found


def os_hint(steps: Sequence[str]):
    head = " ".join(steps[:6]).lower()
    alpine = "alpine-minirootfs" in head
    r = os_root_index(steps)
    if not alpine and r is not None and rootfs_kind(steps[r]) == "rootfs":
        alpine = any("apk add" in s for s in steps)
    m = re.search(r"'(" + "|".join(DEBIAN_CODENAMES) + r")'", head)
    return alpine, (m.group(1) if m else None)


def version_filters(version: str) -> List[str]:
    num = re.search(r"\d+(?:\.\d+)*", version or "")
    parts = num.group(0).split(".") if num else []
    out = [".".join(parts[:n]) for n in (len(parts), 2, 1) if 0 < n <= len(parts)]
    return list(dict.fromkeys(out))


def filter_candidates(names: Sequence[str], steps: Sequence[str]) -> List[str]:
    alpine, codename = os_hint(steps)
    out = [n for n in names if not re.search(r"windows|nanoserver|servercore", n)]
    if alpine:
        out = [n for n in out if "alpine" in n]
    else:
        out = [n for n in out if "alpine" not in n]
        if codename:
            pattern = "|".join(DEBIAN_CODENAMES)
            out = [n for n in out if codename in n or not re.search(pattern, n)]
    return sorted(set(out), key=lambda n: (len(n), n))[:MAX_CANDIDATES]


def split_os_root(steps: Sequence[str], had_signature: bool) -> BaseSplit:
    r = os_root_index(steps)
    rk = rootfs_kind(steps[r]) if r is not None else None
    if rk:
        n = r + 1
        if rk == "ubuntu":
            while n < len(steps) and "umoci" in steps[n]:
                n += 1
        if n < len(steps) and step_kind(steps[n]) == "CMD":
            n += 1
        return BaseSplit(n, f"{rk} root", "low" if had_signature else "medium")
    return BaseSplit(0, "scratch", "low" if had_signature else "high")


TagLister = Callable[[str, str], Awaitable[Optional[List[str]]]]
LayerFetcher = Callable[[str, str], Awaitable[Optional[List[str]]]]


async def split_base(steps: Sequence[str], list_tags: TagLister, fetch_steps: LayerFetcher) -> BaseSplit:
    """Find where the student's own steps start.

    list_tags(family, name_filter) -> official tag names containing name_filter
    fetch_steps(family, tag)       -> that official tag's instructions (same platform)
    """
    sig = signature(steps)
    if sig:
        _, fam, ver = sig
        best_n, best_name = 0, None
        for f in version_filters(ver):
            names = await list_tags(fam, f) or []
            candidates = filter_candidates(names, steps)
            if not candidates:
                continue
            for tag in candidates:
                base = await fetch_steps(fam, tag)
                n = prefix_len(base or [], steps)
                if n > best_n:
                    best_n, best_name = n, f"{fam}:{tag}"
            if best_name:
                break
        if best_name:
            return BaseSplit(best_n, best_name, "high")
    return split_os_root(steps, had_signature=bool(sig))
