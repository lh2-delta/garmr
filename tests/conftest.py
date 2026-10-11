import json
import subprocess
import tarfile
from pathlib import Path

import pytest

OWNER, REPO = "acme", "widgets"
BASE = f"https://github.com/{OWNER}/{REPO}"


def full_pr(n):
    return {"type": "pull_request", "url": f"{BASE}/pull/{n}", "repository": BASE, "labels": [],
            "user": "https://github.com/alice", "title": f"pr {n}", "created_at": "2025-01-01T00:00:00Z",
            "base": {"ref": "main", "sha": "0" * 40, "repo": BASE}, "head": {"ref": f"b{n}", "sha": "1" * 40, "repo": BASE}}


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd)})


def make_bare(dest: Path, pr_numbers):
    work = dest.parent / (dest.name + "_work")
    work.mkdir(parents=True)
    git(work, "init", "-q", "-b", "main")
    (work / "a.txt").write_text("hello\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "init")
    for n in pr_numbers:
        git(work, "update-ref", f"refs/pull/{n}/head", "HEAD")
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(work.parent, "clone", "-q", "--bare", str(work), str(dest))
    # clone --bare does not copy refs/pull/*; mirror semantics are what GitHub archives hold
    for n in pr_numbers:
        git(dest, "update-ref", f"refs/pull/{n}/head", "HEAD")
    git(dest, "pack-refs", "--all")
    return dest


def build_github_tree(root: Path, prs=(1, 2, 3), pr_meta=None, labels=("bug", "feature"),
                      issue_labels=("bug",), zero_byte=None):
    pr_meta = list(prs) if pr_meta is None else pr_meta
    root.mkdir(parents=True, exist_ok=True)
    (root / "schema.json").write_text(json.dumps({"version": "1.2.0"}))
    (root / "repositories_000001.json").write_text(json.dumps(
        [{"type": "repository", "url": BASE, "name": REPO, "owner": f"https://github.com/{OWNER}"}]))
    (root / "pull_requests_000001.json").write_text(json.dumps(
        [full_pr(n) for n in pr_meta]))
    (root / "labels_000001.json").write_text(json.dumps(
        [{"type": "label", "url": f"{BASE}/labels/{n}", "name": n, "color": "d73a4a"} for n in labels]))
    (root / "issues_000001.json").write_text(json.dumps(
        [{"type": "issue", "url": f"{BASE}/issues/10", "repository": BASE, "user": "https://github.com/alice",
          "title": "an issue", "created_at": "2025-01-01T00:00:00Z",
          "labels": [f"{BASE}/labels/{n}" for n in issue_labels]}]))
    (root / "users_000001.json").write_text(json.dumps(
        [{"type": "user", "url": "https://github.com/alice", "login": "alice"},
         {"type": "user", "url": f"https://github.com/{OWNER}", "login": OWNER}]))
    make_bare(root / "repositories" / OWNER / f"{REPO}.git", prs)
    if zero_byte:
        (root / zero_byte).write_text("")
    return root


def tar_it(src: Path, out: Path):
    with tarfile.open(out, "w:gz") as t:
        for p in sorted(src.iterdir()):
            t.add(p, arcname=p.name)
    return out


COUNTS_OK = {"prs": 3, "issues": 1, "labels": 2, "milestones": 0, "releases": 0, "issue_comments": 0,
             "review_comments": 0, "commit_comments": 0, "branches": 1, "tags": 0, "collaborators": 0, "commits": 1,
             "default_branch": "main"}


def make_for_check(archive: Path, tree: Path, *, before=None, after=None, refs_before=None, refs_after=None,
                   meta=None, requested=("acme/widgets",), exported=("acme/widgets",), files=None, objects=None,
                   label="acme/widgets", write=True):
    """Write <archive stem>.for-check.csv the way migration-downloader does (see its README)."""
    import csv
    import hashlib
    from check_export import archive_stem
    bare = tree / "repositories" / OWNER / f"{REPO}.git"
    if refs_before is None:
        out = subprocess.run(["git", "-C", str(bare), "for-each-ref", "--format=%(refname) %(objectname)"],
                             capture_output=True, text=True).stdout.splitlines()
        refs_before = dict(line.split(" ") for line in out)
    refs_after = refs_before if refs_after is None else refs_after
    if files is None:
        files, ocount, obytes = {}, 0, 0
        with tarfile.open(archive) as t:
            for ti in t:
                if not ti.isfile():
                    continue
                name = ti.name.lstrip("./")
                if "/objects/" in name and ".git/" in name:
                    ocount += 1
                    obytes += ti.size
                else:
                    files[name] = ti.size
    else:
        ocount, obytes = objects if objects else (0, 0)
    data = archive.read_bytes()
    m = {"format_version": 1, "provider": "github", "host": "https://api.github.com", "scope": "user", "org": "",
         "migration_id": "42", "state": "exported", "archive": archive.name, "archive_bytes": len(data),
         "archive_sha256": hashlib.sha256(data).hexdigest(), "content_length": len(data), "opt_excludes": "none"}
    m.update(meta or {})
    rows = [(label, "meta", k, v) for k, v in m.items()]
    rows += [(label, "requested_repo", r, "") for r in requested] + [(label, "export_repo", r, "") for r in exported]
    for tag, counts, refs in (("before", before, refs_before), ("after", after, refs_after)):
        c = COUNTS_OK if (before if tag == "before" else after) is None else (before if tag == "before" else after)
        rows += [(label, f"counts_{tag}", k, v) for k, v in c.items()]
        rows += [(label, f"ref_{tag}", k, v) for k, v in refs.items()
                 if k.startswith(("refs/heads/", "refs/tags/")) or k.endswith("/head")]
    rows += [(label, "file", k, v) for k, v in files.items()]
    rows += [(label, "file_summary", "objects_count", ocount), (label, "file_summary", "objects_bytes", obytes)]
    path = archive.with_name(archive_stem(archive.name) + ".for-check.csv")
    if write:
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["repo", "section", "key", "value"])
            w.writerows(rows)
    return path


@pytest.fixture
def gh_tree(tmp_path):
    return build_github_tree(tmp_path / "ok")


# ---------------------------------------------------------------- batch hardening helpers (b_ prefix)

def b_env(tmp_path, monkeypatch):
    """in/out/work folders wired through the env vars main() reads."""
    inp, out, work = tmp_path / "in", tmp_path / "out", tmp_path / "work"
    inp.mkdir()
    monkeypatch.setenv("EXPORT_CHECK_INPUT", str(inp))
    monkeypatch.setenv("EXPORT_CHECK_OUTPUT", str(out))
    monkeypatch.setenv("EXPORT_CHECK_WORK", str(work))
    return inp, out, work


def b_archive(inp: Path, tmp_path: Path, name="acme-widgets", dest=None, **tree_kw):
    tree = build_github_tree(tmp_path / f"tree_{name}", **tree_kw)
    archive = tar_it(tree, (dest or inp) / f"{name}.tar.gz")
    return archive, tree


def b_read(path):
    import csv
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def b_zip(zpath: Path, members: dict):
    """members: arcname -> bytes | Path."""
    import zipfile
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
        for arc, src in members.items():
            if isinstance(src, bytes):
                z.writestr(arc, src)
            else:
                z.write(src, arc)
    return zpath
