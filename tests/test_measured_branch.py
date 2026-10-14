"""Garmr measures LOC / files / commits_measured on the branch with the newest commit (the profiler's rule); commits_head stays on
the default branch because the for-check's commit count is the platform's default-branch history."""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_export as ce  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args, date=None):
    env = dict(ENV)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    subprocess.run(["git", "-c", "init.defaultBranch=master", *args], cwd=cwd, env=env, check=True, capture_output=True)


def commit(work, name, content, date):
    (work / name).write_text(content)
    git(work, "add", name)
    git(work, "commit", "-q", "-m", f"add {name}", date=date)


def make_bare(tmp, feature_date="2024-06-01T12:00:00Z", master_date="2020-01-01T12:00:00Z"):
    """master: 1 commit (old); feature: master's commit + 2 newer ones. Returns the bare repo."""
    work = tmp / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "master")
    commit(work, "a.txt", "a\n", master_date)
    git(work, "checkout", "-q", "-b", "feature")
    commit(work, "b.txt", "b\n", feature_date)
    commit(work, "c.txt", "c\n", feature_date)
    bare = tmp / "bare.git"
    git(tmp, "clone", "-q", "--bare", str(work), str(bare))
    return bare


def facts(bare, default="master"):
    return ce.git_facts(bare, "acme/x", False, False, default, False, "x")[1]["metrics"]


def test_the_newest_branch_is_chosen_not_the_default(tmp_path):
    assert ce.latest_branch(make_bare(tmp_path), "master") == "feature"


def test_a_tie_goes_to_the_default_branch(tmp_path):
    same = "2022-01-01T00:00:00Z"
    assert ce.latest_branch(make_bare(tmp_path, feature_date=same, master_date=same), "master") == "master"


def test_use_default_branch_switch_and_no_branch(tmp_path, monkeypatch):
    bare = make_bare(tmp_path)
    monkeypatch.setenv("GARMR_USE_DEFAULT_BRANCH", "1")
    assert ce.latest_branch(bare, "master") == ""
    empty = tmp_path / "empty.git"
    subprocess.run(["git", "init", "-q", "--bare", str(empty)], check=True, capture_output=True)
    monkeypatch.delenv("GARMR_USE_DEFAULT_BRANCH")
    assert ce.latest_branch(empty, "master") == ""


def test_metrics_follow_the_measured_branch_but_commits_head_stays_on_the_default(tmp_path):
    m = facts(make_bare(tmp_path))
    assert m["measured_branch"] == "feature"
    assert m["commits_measured"] == 3          # master's commit + the two newer ones
    assert m["commits_head"] == 1              # the default branch: what the for-check compares with
    assert m["files"] == 3                     # a, b and c exist on the measured branch only
    assert m["commits_all"] == 3


def test_old_behaviour_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMR_USE_DEFAULT_BRANCH", "1")
    m = facts(make_bare(tmp_path))
    assert m["measured_branch"] == "master" and m["commits_measured"] == m["commits_head"] == 1 and m["files"] == 1


def test_a_repo_with_one_branch_measures_it(tmp_path):
    from conftest import build_github_tree
    from test_visibility_prs import repo_of
    m = repo_of(build_github_tree(tmp_path / "t"))["metrics"]
    assert m["measured_branch"] and m["commits_measured"] == m["commits_head"]


def test_the_report_has_both_columns(tmp_path):
    from conftest import build_github_tree
    from test_visibility_prs import run_report
    row = run_report(tmp_path, build_github_tree(tmp_path / "tree"))
    assert row["measured_branch"] and row["commits_measured"] == row["commits_head"]
