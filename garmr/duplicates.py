"""Garmr: duplication among the repositories of one run (ported from Squirrel's L10.1 / L10.2).

Each repository is fingerprinted once while it is checked (check_export.fingerprint): the tree id of the measured
branch, the set of blob ids of its files of at least MIN_BLOB bytes that are not common shared files, and the set of
its commit ids. After every archive is checked, the fingerprints are compared:

- identical tree: the same tree id (every path, mode and content) as another repository;
- near duplicate: Jaccard similarity of the blob sets >= NEAR_DUP with the nearest repository;
- shared history: at least SHARED_HISTORY of this repository's commits also exist in one other repository;
- shared files: how many of this repository's blobs also exist in any other repository (column only).

Common files (third-party libraries, build output, lock files, media, licences, editor/CI configuration, stock framework
files) are left out of the blob set, so two repositories built on the same template or libraries are not counted as
copies. The tree id still covers every file. All findings are WARN: duplication is a fact to read, not a missing item.
"""
from __future__ import annotations

import posixpath

MIN_BLOB = 64
NEAR_DUP = 0.8
SHARED_HISTORY = 0.8

DIRS = {"vendor", "vendors", "node_modules", "bower_components", "jspm_packages", "third_party", "third-party", "thirdparty",
        "libs", "dist", "build", "out", ".next", ".nuxt", "target", "pods", "carthage", ".gradle", ".idea", ".vscode",
        ".github", ".gitlab", ".circleci", "__pycache__", "site-packages", ".venv", "venv", ".dart_tool", "coverage",
        ".cache", ".sass-cache"}
DIR_PATHS = ("storage/framework", "bootstrap/cache", "public/assets/plugins", "assets/plugins", "public/plugins")
FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json", "composer.lock", "gemfile.lock",
         "podfile.lock", "poetry.lock", "pipfile.lock", "cargo.lock", "go.sum", "pubspec.lock", "packages.lock.json",
         ".gitignore", ".gitattributes", ".gitmodules", ".editorconfig", ".npmrc", ".nvmrc", ".browserslistrc",
         ".styleci.yml", ".travis.yml", ".gitlab-ci.yml", ".ds_store", "thumbs.db"}
PREFIXES = ("license", "licence", "copying", ".eslintrc", ".prettierrc", ".stylelintrc", ".babelrc")
SUFFIXES = (".min.js", ".min.css", ".map", ".bundle.js", ".chunk.js", ".chunk.css")
EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp", ".tif", ".tiff", ".psd", ".ai", ".eps",
              ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".wav", ".ogg", ".webm", ".mov", ".avi", ".pdf",
              ".zip", ".gz", ".tgz", ".tar", ".rar", ".7z", ".jar", ".war", ".aar", ".so", ".dll", ".dylib", ".exe",
              ".bin", ".class", ".pyc", ".o", ".a"}
STOCK = ("artisan", "server.php", "public/index.php", "public/.htaccess", "public/robots.txt", "public/web.config",
         "bin/rails", "bin/rake", "bin/setup", "config.ru", "manage.py", "next-env.d.ts")


def common(p: str) -> bool:
    """True for a file codebases normally share without being related (case-insensitive)."""
    p = p.replace("\\", "/").strip("/").lower()
    if not p:
        return False
    parts = p.split("/")
    name = parts[-1]
    if any(d in DIRS for d in parts[:-1]):
        return True
    folder = "/" + "/".join(parts[:-1]) + "/"
    if any(f"/{x}/" in folder for x in DIR_PATHS):
        return True
    if name in FILES or name.startswith(PREFIXES) or name.endswith(SUFFIXES):
        return True
    if posixpath.splitext(name)[1] in EXTENSIONS:
        return True
    return any(p == x or p.endswith("/" + x) for x in STOCK)


def compare(fps: dict) -> dict:
    """fps: label -> {"tree": str | None, "blobs": set, "commits": set, "common": int}. -> label -> result with
    identical [labels], nearest (label, similarity) or None, shared_history (label, fraction) or None,
    shared_files (count, total)."""
    labels = sorted(fps)
    by_tree: dict = {}
    blob_owners: dict = {}
    commit_owners: dict = {}
    for lab in labels:
        fp = fps[lab]
        if fp.get("tree"):
            by_tree.setdefault(fp["tree"], []).append(lab)
        for b in fp.get("blobs") or ():
            blob_owners.setdefault(b, []).append(lab)
        for c in fp.get("commits") or ():
            commit_owners.setdefault(c, []).append(lab)
    out = {}
    for lab in labels:
        fp = fps[lab]
        blobs, commits = fp.get("blobs") or set(), fp.get("commits") or set()
        inter: dict = {}
        shared = 0
        for b in blobs:
            owners = blob_owners[b]
            if len(owners) > 1:
                shared += 1
                for o in owners:
                    if o != lab:
                        inter[o] = inter.get(o, 0) + 1
        nearest = None
        for o, n in inter.items():
            union = len(blobs) + len(fps[o].get("blobs") or ()) - n
            sim = n / union if union else 0.0
            if nearest is None or sim > nearest[1] or (sim == nearest[1] and o < nearest[0]):
                nearest = (o, sim)
        hist: dict = {}
        for c in commits:
            for o in commit_owners[c]:
                if o != lab:
                    hist[o] = hist.get(o, 0) + 1
        top = max(sorted(hist.items()), key=lambda kv: kv[1], default=None)
        out[lab] = {
            "identical": [o for o in by_tree.get(fp.get("tree"), []) if o != lab] if fp.get("tree") else [],
            "nearest": nearest,
            "shared_history": (top[0], top[1] / len(commits)) if top and commits else None,
            "shared_files": (shared, len(blobs)),
            "common": fp.get("common", 0),
        }
    return out


def findings_and_column(label: str, res: dict, finding):
    """-> (WARN findings, `duplicates` column text) for one repository's comparison result."""
    out, col = [], []
    if res["identical"]:
        out.append(finding("WARN", "duplicate_repo", f"{label}: the tree (every path, mode and file) is identical to "
                           f"{', '.join(res['identical'][:5])}", found=len(res["identical"]), item="identical"))
        col.append("identical:" + ",".join(res["identical"]))
    near = res["nearest"]
    if near and near[1] >= NEAR_DUP and near[0] not in res["identical"]:
        out.append(finding("WARN", "duplicate_repo", f"{label}: nearest repository is {near[0]} with file similarity "
                           f"{near[1]:.2f} (Jaccard over file contents, common files left out), at or above {NEAR_DUP}",
                           item="near"))
    if near:
        col.append(f"nearest:{near[0]}={near[1]:.2f}")
    sh = res["shared_history"]
    if sh and sh[1] >= SHARED_HISTORY:
        out.append(finding("WARN", "duplicate_repo", f"{label}: {sh[1]:.0%} of its commits also exist in {sh[0]} "
                           "(a fork or a re-upload)", item="history"))
    if sh:
        col.append(f"shared_history:{sh[0]}={sh[1]:.0%}")
    n, total = res["shared_files"]
    if total:
        col.append(f"shared_files={n}/{total}")
    return out, ";".join(col)
