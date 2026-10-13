"""Reading a public GitHub repo: what in it can RUN, and what it is at now.

Two questions the deploy flow keeps asking, which used to be answered by showing
a directory listing and asking a person to pick a filename out of it:

  * what can actually be run here?      -> scan_projects()
  * has it changed since we deployed?   -> head_commit()

Both are read-only calls to the public GitHub API (no token, no auth), both are
cached, and both degrade to "unknown" instead of raising. A rate limit on a
shared Render exit IP must not make somebody's project look deleted, and it must
never block a deploy that would otherwise have worked — the caller falls back to
"deploy the whole repo and let the runner detect the entry", which is what it did
before this module existed.

The runner remains the authority on what actually starts: it clones the repo and
runs its own _detect_entry(). This module exists so the CHAT can offer a button
per runnable thing instead of a paragraph of placeholders to type back.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from urllib.parse import quote

import requests

logger = logging.getLogger("codenest.github")

API = "https://api.github.com"
UA = os.getenv("GITHUB_UA", "CodeNest/1.0 (+https://github.com/tajhatAti/host)")
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()      # optional: raises the rate limit
TREE_CACHE_S = int(os.getenv("GITHUB_TREE_CACHE_S", "900"))
COMMIT_CACHE_S = int(os.getenv("GITHUB_COMMIT_CACHE_S", "120"))
TIMEOUT_S = float(os.getenv("GITHUB_TIMEOUT_S", "12"))
MAX_PROJECTS = int(os.getenv("GITHUB_MAX_PROJECTS", "8"))

# Same order the runner's _ENTRY_CANDIDATES uses, so what the chat offers and
# what the runner starts cannot disagree. (lang, filename)
ENTRY_CANDIDATES = (
    ("python", "main.py"), ("python", "app.py"), ("python", "bot.py"),
    ("python", "server.py"), ("python", "index.py"), ("python", "run.py"),
    ("python", "manage.py"), ("python", "dashboard.py"),
    ("javascript", "index.js"), ("javascript", "server.js"),
    ("javascript", "app.js"), ("javascript", "main.js"), ("javascript", "bot.js"),
    ("typescript", "index.ts"), ("typescript", "main.ts"), ("typescript", "bot.ts"),
    ("ruby", "app.rb"), ("ruby", "main.rb"), ("ruby", "server.rb"),
    ("php", "index.php"), ("php", "main.php"),
    ("lua", "main.lua"), ("lua", "bot.lua"),
    ("go", "main.go"), ("rust", "main.rs"),
    ("bash", "start.sh"), ("bash", "run.sh"), ("bash", "main.sh"),
)

_EXT_LANG = {
    "py": "python", "js": "javascript", "mjs": "javascript", "ts": "typescript",
    "rb": "ruby", "php": "php", "sh": "bash", "lua": "lua", "go": "go",
    "rs": "rust", "c": "c", "cpp": "cpp", "cc": "cpp", "java": "java",
    "pl": "perl", "sql": "sql",
}

# Dependency manifests, in the order a build step would want them.
MANIFESTS = ("requirements.txt", "pyproject.toml", "package.json", "Gemfile",
             "composer.json", "requirements-dev.txt")

# Directories that are never a project of their own.
_SKIP_DIRS = {
    "test", "tests", "testing", "docs", "doc", "examples", "example", "scripts",
    "assets", "static", "public", "media", "img", "images", "data", "backup",
    "backups", "node_modules", "venv", ".venv", "env", "__pycache__", ".git",
    ".github", ".idea", ".vscode", "dist", "build", "out", "target", "vendor",
    "migrations", "locale", "locales", "tmp", "temp", "cache",
}

_lock = threading.Lock()
_cache: dict = {}          # key -> (expires_at, value)


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
def parse_repo(url: str) -> tuple:
    """(owner, repo, branch) from anything a person is likely to paste.

    Accepts `owner/repo`, `github.com/owner/repo`, the same with `.git`, a
    browser folder view `/tree/<branch>` (including a branch that contains
    slashes, which is what `arena/01a0ba14-b` is), and `owner/repo#<branch>`.
    Returns ("", "", "") when there is no repo in the string at all.
    """
    raw = (url or "").strip()
    if not raw:
        return ("", "", "")
    raw = re.sub(r"^https?://", "", raw)
    raw = re.sub(r"^(www\.)?github\.com[:/]", "", raw, flags=re.I)
    raw = raw.strip("/")
    branch = ""
    if "#" in raw:
        raw, _, frag = raw.partition("#")
        branch = frag.strip()
        raw = raw.strip("/")
    m = re.match(r"^([^/\s]+)/([^/\s]+?)(?:\.git)?(?:/(.*))?$", raw)
    if not m:
        return ("", "", "")
    owner, repo, rest = m.group(1), m.group(2), (m.group(3) or "")
    if rest.startswith("tree/"):
        # Everything after /tree/ is the branch, slashes included. A deeper
        # folder view (/tree/br/app/sub) still yields the branch: the runner
        # retries the longest candidates first, so a too-long branch fails over
        # to the real one.
        branch = branch or rest[len("tree/"):].strip("/")
    elif rest and not branch:
        # /blob/<branch>/... or /settings, /actions — not a branch we can trust.
        if rest.startswith("blob/"):
            branch = rest[len("blob/"):].split("/", 1)[0]
    return (owner, repo.rstrip(".git") if repo.endswith(".git") else repo, branch)


def repo_url(owner: str, repo: str, branch: str = "") -> str:
    """A clone URL the runner accepts, with the branch in the /tree/ form."""
    base = f"https://github.com/{owner}/{repo}"
    return f"{base}/tree/{branch}" if branch else base


# ---------------------------------------------------------------------------
# GitHub API, cached
# ---------------------------------------------------------------------------
def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": UA,
         "X-GitHub-Api-Version": "2022-11-28"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def _cached(key: str, ttl: int, fetch):
    """One shared TTL cache: GitHub is rate-limited per source IP, and a
    shared Render exit IP runs out fast enough that every call must count."""
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = fetch()
    if value is not None:
        with _lock:
            _cache[key] = (now + ttl, value)
        return value
    # A failure keeps the previous answer alive a little longer rather than
    # replacing it with nothing: "the project vanished" is a worse lie than
    # "this list is 20 minutes old".
    with _lock:
        stale = _cache.get(key)
        if stale:
            _cache[key] = (now + 60, stale[1])
            return stale[1]
    return None


def tree(owner: str, repo: str, branch: str = "") -> list:
    """Every path in the repo (recursive), or [] when it cannot be read."""
    if not owner or not repo:
        return []
    ref = quote(branch or "HEAD", safe="")

    def fetch():
        url = f"{API}/repos/{owner}/{repo}/git/trees/{ref}?recursive=1"
        try:
            r = requests.get(url, headers=_headers(), timeout=TIMEOUT_S)
        except Exception as exc:                                   # noqa: BLE001
            logger.warning("github tree %s/%s: %s", owner, repo, exc)
            return None
        if r.status_code != 200:
            logger.warning("github tree %s/%s -> HTTP %s", owner, repo, r.status_code)
            return None
        try:
            entries = (r.json() or {}).get("tree") or []
        except Exception:                                          # noqa: BLE001
            return None
        return [{"path": str(e.get("path") or ""), "type": e.get("type"),
                 "size": int(e.get("size") or 0)}
                for e in entries if e.get("path")]

    return _cached(f"tree:{owner}/{repo}:{branch}", TREE_CACHE_S, fetch) or []


def head_commit(owner: str, repo: str, branch: str = "") -> str:
    """The commit SHA the branch points at right now, or "" when unknown.

    This is the whole "is there a newer version?" question: the deploy records
    the SHA it cloned, and comparing it to this is one small API call.
    """
    if not owner or not repo:
        return ""
    ref = quote(branch or "HEAD", safe="")

    def fetch():
        url = f"{API}/repos/{owner}/{repo}/commits/{ref}"
        try:
            r = requests.get(url, headers=_headers(), timeout=TIMEOUT_S,
                             params={"per_page": 1})
        except Exception as exc:                                   # noqa: BLE001
            logger.warning("github head %s/%s: %s", owner, repo, exc)
            return None
        if r.status_code != 200:
            return None
        try:
            sha = str((r.json() or {}).get("sha") or "")
        except Exception:                                          # noqa: BLE001
            return None
        return sha or None

    return _cached(f"head:{owner}/{repo}:{branch}", COMMIT_CACHE_S, fetch) or ""


def list_branches(owner: str, repo: str, limit: int = 8) -> list:
    """Branch names (main/master/dev first), or [] when GitHub won't say."""
    if not owner or not repo:
        return []

    def fetch():
        url = f"{API}/repos/{owner}/{repo}/branches"
        try:
            r = requests.get(url, headers=_headers(), timeout=TIMEOUT_S,
                             params={"per_page": 50})
        except Exception as exc:                                   # noqa: BLE001
            logger.warning("github branches %s/%s: %s", owner, repo, exc)
            return None
        if r.status_code != 200:
            return None
        try:
            return [str(b.get("name")) for b in (r.json() or []) if b.get("name")]
        except Exception:                                          # noqa: BLE001
            return None

    names = _cached(f"branches:{owner}/{repo}", COMMIT_CACHE_S, fetch) or []
    first = [n for n in ("main", "master", "dev", "develop") if n in names]
    return (first + [n for n in names if n not in first])[:limit]


def readme(owner: str, repo: str, branch: str = "", limit: int = 3500) -> str:
    """The repo's README as plain text (best effort), for the 📖 button."""
    if not owner or not repo:
        return ""
    ref = quote(branch or "HEAD", safe="")

    def fetch():
        url = f"{API}/repos/{owner}/{repo}/readme"
        try:
            r = requests.get(url, headers=dict(_headers(),
                                               **{"Accept": "application/vnd.github.raw+json"}),
                             params={"ref": branch or ""} if branch else None,
                             timeout=TIMEOUT_S)
        except Exception as exc:                                   # noqa: BLE001
            logger.warning("github readme %s/%s: %s", owner, repo, exc)
            return None
        if r.status_code != 200:
            return None
        return (r.text or "")[:limit] or None

    _ = ref
    return _cached(f"readme:{owner}/{repo}:{branch}", TREE_CACHE_S, fetch) or ""


# ---------------------------------------------------------------------------
# What in the repo can actually run
# ---------------------------------------------------------------------------
def _manifests_in(paths: set, prefix: str) -> list:
    """Dependency files that live in `prefix` ("" = the repo root)."""
    found = []
    for name in MANIFESTS:
        if (prefix + name) in paths:
            found.append(prefix + name)
    # requirements-web.txt and friends: a second runnable thing in one repo
    # usually declares its own extras, and naming them is what tells the owner
    # why a deploy needs an extra step.
    for p in sorted(paths):
        if not p.startswith(prefix) or "/" in p[len(prefix):]:
            continue
        base = p[len(prefix):]
        if base.startswith("requirements") and base.endswith(".txt") and p not in found:
            found.append(p)
    return found[:4]


def _lang_of(entry: str) -> str:
    ext = entry.rsplit(".", 1)[-1].lower() if "." in entry else ""
    return _EXT_LANG.get(ext, "")


def scan_projects(owner: str, repo: str, branch: str = "") -> list:
    """Every runnable thing in the repo, best guess first.

    A "project" is a directory with an entry file in it: the repo root, and any
    first-level folder that holds one. Each result carries enough to deploy it
    without asking another question — the path to run, its language, and the
    dependency files beside it:

        {"name": "web", "dir": "web/", "entry": "web/dashboard.py",
         "language": "python", "manifests": ["requirements-web.txt"],
         "kind": "web", "label": "web — python · web/dashboard.py"}

    Returns [] when the repo cannot be listed; the caller then falls back to
    deploying the whole repo and letting the runner detect the entry.
    """
    entries = tree(owner, repo, branch)
    if not entries:
        return []
    blobs = {e["path"] for e in entries if e.get("type") == "blob"}
    dirs = {e["path"] for e in entries if e.get("type") == "tree"}
    if not blobs:
        return []

    projects = []

    def add(name, dir_prefix, entry, manifests, kind):
        if not entry or entry not in blobs:
            return
        lang = _lang_of(entry)
        if not lang:
            return
        projects.append({
            "name": name, "dir": dir_prefix, "entry": entry, "language": lang,
            "manifests": manifests, "kind": kind,
            "label": f"{name} — {lang} · {entry}",
        })

    def first_candidate(prefix):
        for lang, fname in ENTRY_CANDIDATES:
            if prefix + fname in blobs:
                return prefix + fname, lang
        return "", ""

    # 1. the repo root
    root_entry, root_lang = first_candidate("")
    root_manifests = _manifests_in(blobs, "")
    if root_entry:
        kind = "static" if ("index.html" in blobs and not root_manifests) else "app"
        add(repo, "", root_entry, root_manifests, kind)
    elif "index.html" in blobs and not root_manifests:
        # A plain static site: the runner serves the folder over HTTP.
        projects.append({"name": repo, "dir": "", "entry": "index.html",
                         "language": "python", "manifests": [], "kind": "static",
                         "label": f"{repo} — static site · index.html"})

    # 2. first-level folders that hold something runnable
    for d in sorted(dirs):
        if "/" in d or d.startswith(".") or d.lower() in _SKIP_DIRS:
            continue
        prefix = d + "/"
        entry, _lang = first_candidate(prefix)
        if not entry:
            continue
        manifests = _manifests_in(blobs, prefix)
        if not manifests:
            # Nothing of its own: prefer the root file NAMED for this folder
            # (requirements-web.txt for web/) over the generic one, because that
            # is the convention a repo with two runnable halves uses, and
            # installing the wrong half is a deploy that starts and then dies on
            # an import.
            hinted = [m for m in root_manifests
                      if d.lower() in os.path.basename(m).lower()]
            manifests = hinted or root_manifests[:1]
        kind = "web" if any(x in entry for x in ("dashboard", "server", "web", "app")) else "app"
        add(d, prefix, entry, manifests, kind)

    return projects[:MAX_PROJECTS]


def describe_scan(owner: str, repo: str, branch: str = "") -> str:
    """One line per runnable thing, for a chat message. [] -> ""."""
    projects = scan_projects(owner, repo, branch)
    if not projects:
        return ""
    lines = []
    for p in projects:
        where = "repo root" if not p["dir"] else f"`{p['dir']}`"
        deps = ", ".join(f"`{os.path.basename(m)}`" for m in p["manifests"][:2])
        lines.append(f"• *{p['name']}* — {where} · runs `{p['entry']}` "
                     f"({p['language']})" + (f" · installs {deps}" if deps else ""))
    return "\n".join(lines)
