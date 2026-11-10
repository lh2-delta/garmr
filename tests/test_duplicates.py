"""Duplication among the repositories of one run (Squirrel L10.1 / L10.2). Synthetic data only; all WARN."""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import batch  # noqa: E402
import check_export as ce  # noqa: E402
from conftest import b_read, tar_it  # noqa: E402
from garmr import duplicates  # noqa: E402


def test_common_rules():
    for p in ("vendor/lib/x.php", "node_modules/a/index.js", "public/assets/plugins/x.js", "package-lock.json",
              "LICENSE.md", "app.min.js", "logo.PNG", "artisan", "sub/public/index.php", ".eslintrc.json"):
        assert duplicates.common(p), p
    for p in ("app/Http/Controllers/Home.php", "src/index.js", "README.md", "config/app.php", "lib/x.js"):
        assert not duplicates.common(p), p


def fp(tree, blobs, commits=()):
    return {"tree": tree, "blobs": set(blobs), "commits": set(commits), "common": 0}


def test_compare_identical_near_and_history():
    res = duplicates.compare({
        "a": fp("T1", range(10), ["c1", "c2"]),
        "b": fp("T1", range(10), ["c1", "c2"]),
        "c": fp("T2", range(9), ["x"]),
        "d": fp("T3", range(100, 110), ["y"]),
    })
    assert res["a"]["identical"] == ["b"]
    assert res["c"]["nearest"][0] in ("a", "b") and res["c"]["nearest"][1] == 0.9
    assert res["a"]["shared_history"] == ("b", 1.0)
    assert res["d"]["nearest"] is None and res["d"]["shared_files"] == (0, 10)
    assert res["c"]["shared_files"] == (9, 9)


def test_findings_and_column():
    res = duplicates.compare({"a": fp("T", range(10), ["c"]), "b": fp("T", range(10), ["c"])})
    out, col = duplicates.findings_and_column("a", res["a"], ce.finding)
    assert {x["item"] for x in out} == {"identical", "history"}
    assert all(x["level"] == "WARN" and x["check"] == "duplicate_repo" for x in out)
    assert col.startswith("identical:b") and "shared_files=10/10" in col


def git(cwd, *args):
    e = {"PATH": os.environ["PATH"], "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "dev",
         "GIT_AUTHOR_EMAIL": "dev@example.com", "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
         "GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000"}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=e)


def plain_repo(inp, name, files):
    d = inp / name
    d.mkdir()
    git(d, "init", "-q", "-b", "main")
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "init")
    return d


def test_fingerprint_leaves_out_small_and_common_files(tmp_path):
    d = plain_repo(tmp_path, "r", {"app.py": "x" * 100, "tiny.py": "x", "vendor/lib.py": "y" * 100})
    f = ce.fingerprint(d / ".git", "HEAD")
    assert len(f["blobs"]) == 1 and f["common"] == 1 and len(f["commits"]) == 1 and f["tree"]


def test_batch_reports_duplicates_between_archives(tmp_path):
    inp, out, work = tmp_path / "in", tmp_path / "out", tmp_path / "work"
    inp.mkdir()
    src = {f"src/m{i}.py": f"print({i})\n" * 20 for i in range(5)}
    build = tmp_path / "build"
    build.mkdir()
    for name, files in (("alpha", src), ("beta", src), ("gamma", {"other.py": "z" * 200})):
        tar_it(plain_repo(build, name, files), inp / f"{name}.tar.gz")
    batch.run(inp, out, work, want_loc=False)
    rows = {r["repo"]: r for r in b_read(out / "completeness_report.csv")}
    a = [r for k, r in rows.items() if k.endswith("alpha")][0]
    g = [r for k, r in rows.items() if k.endswith("gamma")][0]
    assert "duplicate_repo" in a["warned_checks"] and "identical:" in a["duplicates"]
    assert "duplicate_repo" not in g["warned_checks"] and "shared_files=0/1" in g["duplicates"]
