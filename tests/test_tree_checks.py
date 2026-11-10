"""Checks of the measured branch's tree, ported from Squirrel's L2 (0-byte files), L9 (file modes, symlinks, submodules,
external pointers) and L10.4 (committed operating-system and dependency folders). Synthetic repos only; all WARN."""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_export as ce  # noqa: E402


def run(cwd, *args):
    e = {"PATH": os.environ["PATH"], "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "dev",
         "GIT_AUTHOR_EMAIL": "dev@example.com", "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@example.com"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=e)


def make(tmp_path, build, after_add=None):
    d = tmp_path / "r"
    d.mkdir()
    run(d, "init", "-q", "-b", "main")
    (d / "main.py").write_text("print('hi')\n")
    build(d)
    run(d, "add", "-A")
    if after_add:
        after_add(d)
    run(d, "commit", "-q", "-m", "c")
    return d / ".git"


def by_check(findings):
    out = {}
    for f in findings:
        out.setdefault(f["check"], []).append(f)
    return out


def test_clean_tree_has_no_findings(tmp_path):
    bare = make(tmp_path, lambda d: (d / ".gitkeep").write_text(""))
    assert ce.tree_findings(bare, "HEAD", "r") == []


def test_zero_byte_files_outside_the_allowlist_warn(tmp_path):
    def b(d):
        (d / "empty.js").write_text("")
        (d / "pkg").mkdir()
        (d / "pkg" / "__init__.py").write_text("")
    f = by_check(ce.tree_findings(make(tmp_path, b), "HEAD", "r"))
    z = f["tree_zero_byte"][0]
    assert z["level"] == "WARN" and z["found"] == 1 and "empty.js" in z["detail"]


def test_symlinks_that_escape_or_are_absolute_warn(tmp_path):
    def b(d):
        os.symlink("/etc/passwd", d / "abs")
        os.symlink("../../outside", d / "up")
        os.symlink("main.py", d / "ok")
    f = by_check(ce.tree_findings(make(tmp_path, b), "HEAD", "r"))
    s = f["tree_symlink"][0]
    assert s["level"] == "WARN" and s["found"] == 2


def test_gitlink_without_gitmodules_warns(tmp_path):
    def b(d):
        sub = tmp_path / "sub"
        sub.mkdir()
        run(sub, "init", "-q", "-b", "main")
        (sub / "x").write_text("x\n")
        run(sub, "add", ".")
        run(sub, "commit", "-q", "-m", "s")
        sha = run(sub, "rev-parse", "HEAD").stdout.strip()
        run(d, "update-index", "--add", "--cacheinfo", f"160000,{sha},vendor/lib")
    f = by_check(ce.tree_findings(make(tmp_path, lambda d: None, after_add=b), "HEAD", "r"))
    s = f["submodule"][0]
    assert s["level"] == "WARN" and "vendor/lib" in s["detail"] and ".gitmodules" in s["detail"]


def test_committed_os_and_dependency_folders_warn(tmp_path):
    def b(d):
        (d / ".DS_Store").write_text("x" * 10)
        (d / "node_modules" / "left-pad").mkdir(parents=True)
        (d / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n")
        (d / "__MACOSX").mkdir()
        (d / "__MACOSX" / "._main.py").write_text("junk")
    f = by_check(ce.tree_findings(make(tmp_path, b), "HEAD", "r"))
    j = f["committed_junk"][0]
    assert j["level"] == "WARN"
    for word in (".DS_Store", "node_modules", "__MACOSX"):
        assert word in j["detail"]


def test_git_facts_reports_tree_findings_for_the_measured_branch(tmp_path):
    bare = make(tmp_path, lambda d: (d / "empty.js").write_text(""))
    findings, facts = ce.git_facts(bare, "r", scan_lfs=False)
    assert any(f["check"] == "tree_zero_byte" for f in findings)
    assert facts["metrics"]["files"] == 2


def test_dvc_pointer_files_are_noted(tmp_path):
    def b(d):
        (d / "data.csv.dvc").write_text("outs:\n- md5: abc\n  path: data.csv\n")
    f = by_check(ce.tree_findings(make(tmp_path, b), "HEAD", "r"))
    assert f["external_pointers"][0]["level"] == "WARN"
