"""Forged / rewritten / recreated history signals. Synthetic repos only; every signal is a WARN (heuristic, not proof)."""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_export as ce  # noqa: E402

DAY = 86400


def run(cwd, *args, env=None):
    e = {"PATH": os.environ["PATH"], "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1"}
    e.update(env or {})
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=e)


def commit(cwd, n, *, author=("a", "a@x"), committer=None, adate=None, cdate=None, msg=None):
    committer = committer or author
    (cwd / f"f{n}.txt").write_text(f"{n}\n")
    run(cwd, "add", ".")
    env = {"GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
           "GIT_COMMITTER_NAME": committer[0], "GIT_COMMITTER_EMAIL": committer[1]}
    if adate is not None:
        env["GIT_AUTHOR_DATE"] = f"{int(adate)} +0000"
    if cdate is not None:
        env["GIT_COMMITTER_DATE"] = f"{int(cdate)} +0000"
    run(cwd, "commit", "-q", "-m", msg or f"c{n}", env=env)


def repo(tmp_path, name="r"):
    d = tmp_path / name
    d.mkdir()
    run(d, "init", "-q", "-b", "main")
    return d


def checks(findings, name="history_forgery"):
    return [f for f in findings if f["check"] == name]


def scan(d):
    return ce.history_scan(d / ".git", "r")


def test_clean_history_has_no_findings(tmp_path):
    d = repo(tmp_path)
    t = time.time() - 30 * DAY
    for i in range(5):
        commit(d, i, author=(f"u{i % 2}", f"u{i % 2}@x"), adate=t + i * DAY, cdate=t + i * DAY)
    findings, first = scan(d)
    assert findings == []
    assert int(first) == int(t)


def test_future_dated_commit_warns(tmp_path):
    d = repo(tmp_path)
    commit(d, 0, adate=time.time() + 400 * DAY, cdate=time.time() + 400 * DAY)
    f = checks(scan(d)[0])
    assert f and f[0]["level"] == "WARN" and "future" in f[0]["detail"]


def test_author_date_after_committer_date_warns(tmp_path):
    d = repo(tmp_path)
    t = time.time() - 100 * DAY
    commit(d, 0, adate=t + 30 * DAY, cdate=t)
    f = checks(scan(d)[0])
    assert any("author date is later than committer date" in x["detail"] for x in f)


def test_child_older_than_parent_warns(tmp_path):
    d = repo(tmp_path)
    t = time.time() - 100 * DAY
    commit(d, 0, adate=t + 20 * DAY, cdate=t + 20 * DAY)
    commit(d, 1, adate=t, cdate=t)
    f = checks(scan(d)[0])
    assert any("older than its parent" in x["detail"] for x in f)


def test_small_clock_skew_is_tolerated(tmp_path):
    d = repo(tmp_path)
    t = time.time() - 100 * DAY
    commit(d, 0, adate=t + 600, cdate=t + 600)
    commit(d, 1, adate=t, cdate=t)
    assert scan(d)[0] == []


def test_bulk_same_timestamp_warns(tmp_path):
    d = repo(tmp_path)
    t = time.time() - 50 * DAY
    for i in range(ce.BULK_TS_MIN_COMMITS + 5):
        commit(d, i, adate=t - i * DAY, cdate=t)
    f = checks(scan(d)[0])
    assert any("same second" in x["detail"] for x in f)


def test_single_committer_over_many_authors_warns(tmp_path):
    d = repo(tmp_path)
    t = time.time() - 80 * DAY
    for i in range(ce.SINGLE_COMMITTER_MIN_COMMITS + 2):
        commit(d, i, author=(f"dev{i % 4}", f"dev{i % 4}@x"), committer=("bot", "bot@x"),
               adate=t + i * 60, cdate=t + i * 60)
    f = checks(scan(d)[0])
    assert any("one committer" in x["detail"] for x in f)


def test_solo_author_is_not_flagged(tmp_path):
    d = repo(tmp_path)
    t = time.time() - 80 * DAY
    for i in range(ce.SINGLE_COMMITTER_MIN_COMMITS + 2):
        commit(d, i, adate=t + i * 60, cdate=t + i * 60)
    assert not any("one committer" in x["detail"] for x in checks(scan(d)[0]))


def test_squashed_single_commit_with_many_files_warns(tmp_path):
    d = repo(tmp_path)
    for i in range(ce.SQUASH_MIN_FILES):
        (d / f"g{i}.txt").write_text(str(i))
    commit(d, 0, adate=time.time() - DAY, cdate=time.time() - DAY)
    f = checks(scan(d)[0])
    assert any("single commit" in x["detail"] for x in f)


def test_two_unrelated_roots_warns(tmp_path):
    d = repo(tmp_path)
    t = time.time() - 40 * DAY
    commit(d, 0, adate=t, cdate=t)
    run(d, "checkout", "-q", "--orphan", "other")
    commit(d, 1, adate=t + DAY, cdate=t + DAY)
    run(d, "checkout", "-q", "main")
    run(d, "merge", "-q", "--allow-unrelated-histories", "-m", "join", "other",
        env={"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@x"})
    f = checks(scan(d)[0])
    assert any("root commits" in x["detail"] for x in f)


def test_unreachable_commit_warns(tmp_path):
    d = repo(tmp_path)
    t = time.time() - 40 * DAY
    commit(d, 0, adate=t, cdate=t)
    commit(d, 1, adate=t + DAY, cdate=t + DAY)
    run(d, "reset", "-q", "--hard", "HEAD~1")
    run(d, "reflog", "expire", "--expire=now", "--all")
    f = checks(scan(d)[0])
    assert any("unreachable commit" in x["detail"] for x in f)


def test_rewrite_in_reflog_warns(tmp_path):
    d = repo(tmp_path)
    t = time.time() - 40 * DAY
    commit(d, 0, adate=t, cdate=t)
    commit(d, 1, adate=t + DAY, cdate=t + DAY)
    run(d, "commit", "-q", "--amend", "-m", "rewritten",
        env={"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@x"})
    f = checks(scan(d)[0])
    assert any("reflog" in x["detail"] for x in f)


def test_bad_signature_warns(tmp_path, monkeypatch):
    import shutil
    import tempfile
    if not shutil.which("gpg"):
        import pytest
        pytest.skip("gpg not installed")
    gh = tempfile.mkdtemp(prefix="gh")
    os.chmod(gh, 0o700)
    monkeypatch.setenv("GNUPGHOME", gh)
    subprocess.run(["gpg", "--batch", "--passphrase", "", "--quick-gen-key", "t <t@x>", "default", "default", "never"],
                   check=True, capture_output=True)
    d = repo(tmp_path)
    t = time.time() - 40 * DAY
    commit(d, 0, adate=t, cdate=t)
    (d / "s.txt").write_text("s")
    run(d, "add", ".")
    run(d, "-c", "user.signingkey=t@x", "commit", "-q", "-S", "-m", "signed",
        env={"GNUPGHOME": gh, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a",
             "GIT_COMMITTER_EMAIL": "a@x", "GIT_AUTHOR_DATE": f"{int(t) + 9} +0000", "GIT_COMMITTER_DATE": f"{int(t) + 9} +0000"})
    assert checks(scan(d)[0]) == []          # genuine signature: no finding
    raw = run(d, "cat-file", "commit", "HEAD").stdout.replace("\n\nsigned", "\n\nforged")
    p = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=d, input=raw, text=True,
                       capture_output=True, check=True)
    run(d, "update-ref", "refs/heads/main", p.stdout.strip())
    f = checks(scan(d)[0])
    assert any("BAD signature" in x["detail"] for x in f)
    assert all(x["level"] == "WARN" for x in f)


def test_all_history_findings_are_warn(tmp_path):
    d = repo(tmp_path)
    commit(d, 0, adate=time.time() + 400 * DAY, cdate=time.time() + 400 * DAY)
    assert {x["level"] for x in scan(d)[0]} == {"WARN"}


def test_git_facts_runs_history_scan_and_records_first_commit(tmp_path):
    d = repo(tmp_path)
    commit(d, 0, adate=time.time() + 400 * DAY, cdate=time.time() + 400 * DAY)
    f, facts = ce.git_facts(d / ".git", "r")
    assert any(x["check"] == "history_forgery" for x in f)
    assert facts["first_commit_ts"] is not None


def test_repo_created_after_first_commit_warns():
    t = 1_700_000_000
    out = ce.created_vs_first_commit("o/r", "2024-06-01T00:00:00Z", t)
    assert out and out[0]["level"] == "WARN" and out[0]["check"] == "history_forgery"
    assert "recreated" in out[0]["detail"] or "imported" in out[0]["detail"]


def test_repo_created_before_first_commit_is_fine():
    assert ce.created_vs_first_commit("o/r", "2020-01-01T00:00:00Z", 1_700_000_000) == []
    assert ce.created_vs_first_commit("o/r", None, 1_700_000_000) == []
    assert ce.created_vs_first_commit("o/r", "garbage", 1_700_000_000) == []
