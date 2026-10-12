"""Regression tests for the review-fix pass over check_export.py (extraction caps, git failures, live gaps, LOC...)."""
import io
import json
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_export as ce  # noqa: E402
from conftest import build_github_tree, make_bare, tar_it  # noqa: E402
from test_hardening_logic import bare_of, commit_bytes, edit_json, names, pick  # noqa: E402


def _tgz(path: Path, members: dict):
    with tarfile.open(path, "w:gz") as t:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return path


# ---------------------------------------------------------------- 5 / 15 / 20b extraction

def test_extract_member_cap_is_an_archive_error_not_an_abort(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMR_MAX_EXTRACT_MEMBERS", "3")
    arc = _tgz(tmp_path / "many.tar.gz", {f"f{i}": b"x" for i in range(10)})
    err = ce.extract(arc, tmp_path / "d")
    assert isinstance(err, str) and "GARMR_MAX_EXTRACT_MEMBERS" in err
    rep = ce.check_path(arc, workdir=tmp_path / "w")
    assert pick(rep, "archive_integrity", "FAIL")


def test_extract_byte_cap_checked_before_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMR_MAX_EXTRACT_BYTES", "100")
    arc = _tgz(tmp_path / "big.tar.gz", {"a": b"x" * 500})
    err = ce.extract(arc, tmp_path / "d")
    assert "GARMR_MAX_EXTRACT_BYTES" in err and not (tmp_path / "d" / "a").exists()


def test_extract_zip_declared_size_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMR_MAX_EXTRACT_BYTES", "100")
    z = tmp_path / "b.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.txt", "x" * 500)
    assert "GARMR_MAX_EXTRACT_BYTES" in ce.extract(z, tmp_path / "d")


def test_extract_ratio_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "RATIO_MIN_BYTES", 10)
    monkeypatch.setenv("GARMR_MAX_EXTRACT_RATIO", "2")
    arc = _tgz(tmp_path / "bomb.tar.gz", {"a": b"\0" * 100_000})
    assert "GARMR_MAX_EXTRACT_RATIO" in ce.extract(arc, tmp_path / "d")


def test_extract_refuses_when_declared_size_exceeds_free_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(1000, 900, 50))
    arc = _tgz(tmp_path / "a.tar.gz", {"a": b"x" * 500})
    err = ce.extract(arc, tmp_path / "d")
    assert "free" in err


def test_extract_deadline(tmp_path, monkeypatch):
    clock = iter(range(0, 10_000_000, 4000))
    monkeypatch.setenv("GARMR_EXTRACT_TIMEOUT", "3600")
    monkeypatch.setattr(ce.time, "monotonic", lambda: next(clock))
    arc = _tgz(tmp_path / "a.tar.gz", {"a": b"x", "b": b"y"})
    assert "time limit" in ce.extract(arc, tmp_path / "d")


def test_zip_escaping_names_are_skipped_and_warned(tmp_path):
    z = tmp_path / "in" / "evil.zip"
    z.parent.mkdir()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("ok/file.txt", "hi")
        zf.writestr("../evil.txt", "x")
        zf.writestr("/abs.txt", "x")
    skipped = []
    assert ce.extract(z, tmp_path / "d", skipped) is None
    assert set(skipped) == {"../evil.txt", "/abs.txt"}
    assert (tmp_path / "d" / "ok" / "file.txt").exists()
    assert not (tmp_path / "d" / "abs.txt").exists() and not (tmp_path / "evil.txt").exists()


def test_same_named_nested_archives_do_not_merge(tmp_path):
    inp = tmp_path / "in"
    for sub, n in (("a", (1,)), ("b", (1, 2))):
        tree = build_github_tree(tmp_path / f"t_{sub}", prs=n)
        (inp / sub).mkdir(parents=True)
        tar_it(tree, inp / sub / "export.tar.gz")
    units: list = []
    ce.discover_units(inp, tmp_path / "w", units)
    roots = [u[1] for u in units]
    assert len(units) == 2 and len(set(roots)) == 2
    sizes = sorted(len(list(r.glob("pull_requests_*.json"))) for r in roots)
    assert sizes == [1, 1]
    assert all((r / "repositories").is_dir() for r in roots)


def test_nested_archive_depth_cap(tmp_path, monkeypatch):
    inner = _tgz(tmp_path / "x.tar.gz", {"readme": b"hi"})
    cur = inner
    for i in range(ce.MAX_NEST_DEPTH + 2):
        nxt = tmp_path / f"wrap{i}.tar.gz"
        with tarfile.open(nxt, "w:gz") as t:
            t.add(cur, arcname=cur.name)
        cur = nxt
    units: list = []
    ce.discover_units(cur, tmp_path / "w", units)
    assert any("nested more than" in x["detail"] for u in units for x in u[3])


# ---------------------------------------------------------------- 14 git failures are not 'no facts'

def test_git_lines_nonzero_exit_raises(tmp_path):
    with pytest.raises(ce.GitError):
        list(ce.git_lines(tmp_path / "not-a-repo", "log"))


def test_git_lines_ok_exit_codes(gh_tree):
    bare = bare_of(gh_tree)
    assert list(ce.git_lines(bare, "grep", "-l", "nothing-like-this-xyz", "HEAD", "--", ok=(0, 1))) == []


def test_git_failure_after_fsck_is_a_fail_not_a_pass(gh_tree, monkeypatch):
    def boom(bare, *a):
        raise ce.GitError("git rev-list", 128, "fatal: bad object")
    monkeypatch.setattr(ce, "git_int", boom)
    rep = ce.check_path(gh_tree)
    f = pick(rep, "git_integrity", "FAIL")
    assert f and "not verified" in f[0]["detail"] and rep["verdict"] == "INCOMPLETE"


# ---------------------------------------------------------------- 3 unknown branch/tag counts are skipped

def test_unknown_branch_tag_counts_do_not_fail_live_counts(gh_tree, monkeypatch):
    monkeypatch.setattr(ce, "git_facts", lambda *a, **k: ([], ce.empty_facts()))

    def live(owner, name):
        return {"branches": 5, "tags": 4}
    rep = ce.check_path(gh_tree, live=live)
    assert not [f for f in pick(rep, "live_counts") if f.get("item") in ("branches", "tags")]


def test_git_facts_reports_zero_branches_for_known_empty_refs(tmp_path):
    bare = tmp_path / "e.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    f, facts = ce.git_facts(bare, "x", empty_ok=True)
    assert facts["metrics"]["branches"] == 0 and facts["metrics"]["tags"] == 0


# ---------------------------------------------------------------- 4 live gaps

def test_live_entity_that_cannot_be_counted_is_a_finding(gh_tree):
    def live(owner, name):
        return {"tags": None, "collaborators": None, "labels": 2,
                "_errors": {"tags": "HTTPError: 403 rate limit", "collaborators": "HTTPError: 403"}}
    rep = ce.check_path(gh_tree, live=live)
    tags = pick(rep, "live_check_failed", "FAIL", item="tags")
    assert tags and "403" in tags[0]["detail"] and rep["verdict"] == "INCOMPLETE"
    assert pick(rep, "live_check_failed", "WARN", item="collaborators")


def test_github_live_records_the_reason(monkeypatch):
    import urllib.error

    def fake_open(req, timeout=0):
        url = req.full_url
        if "/tags" in url:
            raise urllib.error.HTTPError(url, 403, "rate limited", {}, None)

        class R:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                return b"[]" if "search" not in url else b'{"total_count": 0}'
        return R()
    monkeypatch.setattr(ce.urllib.request, "urlopen", fake_open)
    out = ce.github_live("tok")("o", "r")
    assert out["tags"] is None and "403" in out["_errors"]["tags"]


# ---------------------------------------------------------------- 12 PEER_EXCLUDE typo

def test_peer_exclude_has_no_dead_entries():
    # every entry must be a real key name; "merged_at_" matched nothing and merged_at itself is judged on purpose
    assert "merged_at_" not in ce.PEER_EXCLUDE
    assert not [k for k in ce.PEER_EXCLUDE if k.endswith("_")]


# ---------------------------------------------------------------- 13 commit-message markers

def _no_ref_tree(tmp_path, message, created_at=None):
    root = build_github_tree(tmp_path / "t", prs=())
    commit_bytes(bare_of(root), message)
    if created_at:
        edit_json(root / "repositories_000001.json", lambda d: d[0].update(created_at=created_at))
    return root


def test_merge_marker_without_record_still_fails_for_a_normal_repo(tmp_path):
    root = _no_ref_tree(tmp_path, b"Merge pull request #7 from x/y\n")
    assert pick(ce.check_path(root), "pull_requests", "FAIL")


def test_merge_marker_in_imported_history_only_warns(tmp_path):
    root = _no_ref_tree(tmp_path, b"Merge pull request #7 from x/y\n\ncloses #9\n", created_at="2099-01-01T00:00:00Z")
    rep = ce.check_path(root)
    assert pick(rep, "pull_requests", "WARN") and not pick(rep, "pull_requests", "FAIL")
    assert pick(rep, "number_refs", "WARN") and not pick(rep, "number_refs", "FAIL")


def test_gitlab_merge_request_line_must_name_this_project(tmp_path):
    bare = make_bare(tmp_path / "g.git", ())
    commit_bytes(bare, b"Merge\n\nSee merge request other/proj!5\n")
    commit_bytes(bare, b"Merge\n\nSee merge request me/proj!6\n")
    _, facts = ce.git_facts(bare, "me/proj", slug="me/proj")
    assert facts["merge_prs"] == {6}


# ---------------------------------------------------------------- 16 zero byte

def test_empty_working_tree_files_are_not_zero_byte_failures(tmp_path):
    repo = tmp_path / "wt"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)}
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "x"]):
        if args[0] == "add":
            (repo / ".gitignore").write_text("")
            (repo / "py.typed").write_text("")
            (repo / "a.txt").write_text("a")
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)
    rep = ce.check_path(repo)
    assert not pick(rep, "zero_byte", "FAIL"), rep["findings"]


def test_zero_byte_inside_git_dir_still_fails(tmp_path):
    repo = tmp_path / "wt"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)}
    (repo / "a.txt").write_text("a")
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)
    (repo / ".git" / "refs" / "heads" / "broken").write_text("")
    assert [f for f in ce.zero_byte_findings(repo, "plain-git") if f["level"] == "FAIL"]


# ---------------------------------------------------------------- 7 / 8 LOC

needs_scc = pytest.mark.skipif(not shutil.which("scc"), reason="scc not installed")


def _loc_repo(tmp_path):
    repo = tmp_path / "loc"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)}
    (repo / "a.py").write_text("x = 1\ny = 2\n")
    (repo / "ignored_by_export.py").write_text("a = 1\nb = 2\nc = 3\n")
    (repo / "gitignored.py").write_text("d = 1\ne = 2\n")
    (repo / ".gitattributes").write_text("ignored_by_export.py export-ignore\n")
    (repo / ".gitignore").write_text("gitignored.py\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-f", "."], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)
    return repo / ".git"


@needs_scc
def test_loc_counts_export_ignored_and_gitignored_files(tmp_path):
    assert ce.measure_loc(_loc_repo(tmp_path), "HEAD") == 7


def test_loc_failure_is_raised_not_none(tmp_path):
    if not shutil.which("scc"):
        pytest.skip("scc not installed")
    with pytest.raises(ce.LocUnmeasured):
        ce.measure_loc(_loc_repo(tmp_path), "no-such-ref")


def test_unmeasurable_loc_is_a_warn_finding(gh_tree, monkeypatch):
    def fail(bare, ref="HEAD"):
        raise ce.LocUnmeasured("scc is not installed")
    monkeypatch.setattr(ce, "measure_loc", fail)
    rep = ce.check_path(gh_tree, want_loc=True)
    w = pick(rep, "loc_unmeasured", "WARN")
    assert w and "scc" in w[0]["detail"]


# ---------------------------------------------------------------- 6 stop request

def test_run_cmd_is_killed_by_stop_request():
    ce.clear_stop()
    threading.Timer(0.3, ce.request_stop).start()
    t0 = time.monotonic()
    try:
        with pytest.raises(ce.Interrupted):
            ce.run_cmd(["sleep", "30"], "sleep", timeout=60)
    finally:
        ce.clear_stop()
    assert time.monotonic() - t0 < 5


def test_run_cmd_timeout_still_raises_cmdtimeout():
    with pytest.raises(ce.CmdTimeout):
        ce.run_cmd(["sleep", "30"], "sleep", timeout=0.5)


def test_interrupted_is_not_swallowed_by_exception_handlers(gh_tree, monkeypatch):
    def stop(*a, **k):
        raise ce.Interrupted("git")
    monkeypatch.setattr(ce, "git_facts", stop)
    with pytest.raises(ce.Interrupted):
        ce.check_path(gh_tree)


# ---------------------------------------------------------------- 17 bounded JSON

def test_oversized_metadata_json_is_a_fail_not_loaded(gh_tree, monkeypatch):
    monkeypatch.setenv("GARMR_MAX_JSON_BYTES", "50")
    rep = ce.check_path(gh_tree)
    f = pick(rep, "metadata_json", "FAIL")
    assert f and "GARMR_MAX_JSON_BYTES" in f[0]["detail"]


def test_ndjson_is_streamed_and_bad_line_reported(tmp_path):
    p = tmp_path / "x.ndjson"
    p.write_text('{"a": 1}\n\n{"a": 2}\n[1]\nnot json\n')
    fl: list = []
    out = ce.read_ndjson(p, fl)
    assert out == [{"a": 1}, {"a": 2}] and any("bad JSON" in f["detail"] for f in fl)
    assert names({"findings": fl}) == {"metadata_json"}


# ================================================================ batch.py / forcheck.py

import batch  # noqa: E402
from conftest import b_archive, b_env, b_read  # noqa: E402
from garmr import forcheck  # noqa: E402


def _org(entries, metric="commits"):
    return {"entries": entries, "index": batch.build_index(entries), "metrics": {metric: metric}, "files": []}


def _entry(label, **vals):
    return {"owner": "acme", "name": "widgets", "label": label, "rows": 1, "bad": set(), "files": ["o.csv"],
            "vals": {m: vals.get(m) for m in batch.ORG_METRICS}}


def _rec(**metrics):
    return {"repo": "acme/widgets", "provider": "github", "archive": "acme-widgets.tar.gz", "metrics": metrics,
            "findings": [], "for_check": "no"}


def _st(org):
    return {"org": org, "tol": 0.0, "matched": set(), "seen_star": set(), "require_fc": False}


# 2
def test_ambiguous_org_row_is_flagged_not_compared():
    org = _org([_entry("acme/widgets", commits=999), _entry("acme/widgets-dup", commits=5)])
    rec = _rec(commits_all=1, commits_head=1)
    (row,), miss = batch.build_rows(rec, _st(org))
    assert "AMBIGUOUS_ORG_ROW" in row["flags"] and row["status"] == "COMPLETE" and not miss
    assert row["org_commits"] == ""


# 9
def test_unknown_pr_counts_are_not_compared():
    errs, flags, _ = batch.compare_metrics(_org([], "prs"), {"prs": "5"}, {}, 0)
    assert errs == [] and "ORG_UNCOMPARED:prs" in flags


def test_known_zero_pr_counts_still_compare():
    errs, _, _ = batch.compare_metrics(_org([], "prs"), {"prs": "5"}, {"prs_git": 0, "prs_meta": 0}, 0)
    assert errs and errs[0]["item"] == "prs"


# 7
def test_unmeasured_loc_is_flagged():
    errs, flags, _ = batch.compare_metrics(_org([], "loc"), {"loc": "500"}, {"loc": None}, 0)
    assert errs == [] and "LOC_UNMEASURED" in flags


# 18
def test_identifier_cells_keep_leading_dash_but_formulas_are_escaped():
    assert batch.csv_safe("-tool", identifier=True) == "-tool"
    assert batch.csv_safe("-tool") == "'-tool"
    assert batch.csv_safe("=cmd", identifier=True) == "'=cmd"
    assert batch.csv_safe("-1+cmd|x", identifier=True) == "'-1+cmd|x"


# 6
def test_signal_handler_wakes_running_children():
    ce.clear_stop()
    try:
        batch._on_signal(15, None)
        assert ce._STOP.is_set()
    finally:
        ce.clear_stop()
        batch._STOP["sig"] = None


def test_stop_inside_an_archive_marks_it_not_checked(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path, "acme-a")

    def stop(*a, **k):
        raise ce.Interrupted("git fsck")
    monkeypatch.setattr(batch, "analyse_archive", stop)
    res = batch._run(inp, out, work, want_loc=False)
    assert res["interrupted"] and not res["finalized"] and not res["crash"]
    assert any("NOT_CHECKED: acme-a.tar.gz" in n for n in res["notes"].items)
    assert not list(work.glob("garmr-*"))
    batch._STOP["sig"] = None


# 20e
def test_bad_loc_tolerance_exits_two_with_a_message(tmp_path, monkeypatch, capsys):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    monkeypatch.setenv("LOC_TOLERANCE_PCT", "abc")
    assert batch.main([]) == 2
    assert "LOC_TOLERANCE_PCT" in capsys.readouterr().err


# 19
def test_for_check_file_paths_cannot_escape_the_extract_root(tmp_path):
    root = tmp_path / "x" / "root"
    root.mkdir(parents=True)
    (root / "schema.json").write_text("{}")
    (tmp_path / "x" / "outside.txt").write_text("hello")
    (tmp_path / "abs.txt").write_text("hello")
    rows = [("acme/widgets", "file", "../outside.txt", 5), ("acme/widgets", "file", str(tmp_path / "abs.txt"), 5),
            ("acme/widgets", "file", "schema.json", 2)]
    fc = forcheck.ForCheck(tmp_path / "x.for-check.csv", rows)
    out = forcheck.file_listing_findings(fc, Path("a.tar.gz"), root)
    miss = [f for f in out if f["item"] == "missing"]
    assert miss and set(miss[0]["missing"]) == {"../outside.txt", str(tmp_path / "abs.txt")}
