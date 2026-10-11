"""Hardening of check_export.py: silent passes, crashes, environment failures and false positives."""
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_export as ce  # noqa: E402
from conftest import BASE, COUNTS_OK, OWNER, build_github_tree, full_pr, make_bare, tar_it  # noqa: E402
from garmr import forcheck  # noqa: E402

ALICE = "https://github.com/alice"


def names(rep, level="FAIL"):
    return {f["check"] for f in rep["findings"] if f["level"] == level}


def pick(rep, check, level=None, item=None):
    return [f for f in rep["findings"] if f["check"] == check and (level is None or f["level"] == level)
            and (item is None or f.get("item") == item)]


def edit_json(path, fn):
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data))


def bare_of(root, owner=OWNER, name="widgets"):
    return root / "repositories" / owner / f"{name}.git"


def commit_bytes(bare, message: bytes):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": "/usr/bin:/bin"}
    tree = subprocess.run(["git", "-C", str(bare), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True).stdout.strip()
    parent = subprocess.run(["git", "-C", str(bare), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    sha = subprocess.run(["git", "-C", str(bare), "commit-tree", tree, "-p", parent], input=message,
                         capture_output=True, env=env, check=True).stdout.decode().strip()
    subprocess.run(["git", "-C", str(bare), "update-ref", "refs/heads/main", sha], check=True, env=env)
    return sha


def a_fc(label="acme/widgets", **over):
    """In-memory ForCheck with identical before/after counts (no file needed)."""
    counts = dict(COUNTS_OK, **over)
    rows = [(label, f"counts_{t}", k, v) for t in ("before", "after") for k, v in counts.items()]
    return forcheck.ForCheck(Path("x.for-check.csv"), rows)


def analyse(root, fc=None, live=None, tmp=None):
    root = Path(root)
    return ce.analyse_unit(root.name, root, "github-migration", [], tmp or root.parent, live, True, False, fc)


def all_findings(u):
    return list(u["unit_findings"]) + [x for r in u["repos"] for x in r["findings"]]


def add_repo(root, name, prs=()):
    """Second repository, listed in repositories_000001.json."""
    edit_json(root / "repositories_000001.json", lambda d: d.append(
        {"type": "repository", "url": f"https://github.com/{OWNER}/{name}", "name": name,
         "owner": f"https://github.com/{OWNER}"}))
    make_bare(bare_of(root, name=name), prs)


# ---------------------------------------------------------------- G1

def test_g1_valid_zlib_wrong_content_blob_fails_fsck_full(tmp_path):
    root = build_github_tree(tmp_path / "x")
    bare = bare_of(root)
    sha = subprocess.run(["git", "-C", str(bare), "rev-parse", "HEAD:a.txt"], capture_output=True, text=True).stdout.strip()
    obj = bare / "objects" / sha[:2] / sha[2:]
    assert obj.exists()
    obj.parent.chmod(0o755)
    obj.unlink()
    obj.write_bytes(zlib.compress(b"blob 5\0WRONG"))
    rep = ce.check_path(root)
    f = pick(rep, "git_integrity", "FAIL")
    assert f and any("corrupt" in x["detail"] for x in f)
    assert rep["verdict"] == "INCOMPLETE"


def test_g1_missing_object_reported_as_missing_not_corrupt(tmp_path):
    root = build_github_tree(tmp_path / "x")
    bare = bare_of(root)
    sha = subprocess.run(["git", "-C", str(bare), "rev-parse", "HEAD:a.txt"], capture_output=True, text=True).stdout.strip()
    obj = bare / "objects" / sha[:2] / sha[2:]
    obj.parent.chmod(0o755)
    obj.unlink()
    f = pick(ce.check_path(root), "git_integrity", "FAIL")
    assert any(x.get("item") == "missing" for x in f)


# ---------------------------------------------------------------- G2

def test_g2_zeroed_packed_refs_fails_when_pr_metadata_exists(tmp_path):
    root = build_github_tree(tmp_path / "x")
    (bare_of(root) / "packed-refs").write_text("")
    rep = ce.check_path(root)
    z = [x for x in pick(rep, "zero_byte") if "packed-refs" in x["detail"]]
    assert z and all(x["level"] == "FAIL" for x in z)


def test_g2_zero_packed_refs_without_any_metadata_is_still_a_warn(tmp_path):
    bare = make_bare(tmp_path / "plain.git", ())
    (bare / "packed-refs").write_text("")
    rep = ce.check_path(bare)
    assert [x["level"] for x in pick(rep, "zero_byte")] == ["WARN"]


def test_g2_prs_in_metadata_but_no_pull_refs_in_git_fails(tmp_path):
    root = build_github_tree(tmp_path / "x", prs=(), pr_meta=[1, 2, 3])
    f = pick(ce.check_path(root), "git_refs", "FAIL")
    assert f and f[0]["missing"] == [1, 2, 3]


def test_g2_default_branch_ref_required_even_without_other_branches(tmp_path):
    root = build_github_tree(tmp_path / "x")
    bare = bare_of(root)
    subprocess.run(["git", "-C", str(bare), "update-ref", "-d", "refs/heads/main"], check=True)
    subprocess.run(["git", "-C", str(bare), "update-ref", "refs/tags/v1", "refs/pull/1/head"], check=True)
    edit_json(root / "repositories_000001.json", lambda d: d[0].update(default_branch="main"))
    assert "default_branch" in names(ce.check_path(root))


# ---------------------------------------------------------------- G3

def test_g3_unlisted_repo_fails_and_gets_git_checks(tmp_path):
    root = build_github_tree(tmp_path / "x")
    make_bare(bare_of(root, name="other"), ())
    u = analyse(root)
    assert any(x["check"] == "repo_unlisted" and x["level"] == "FAIL" for x in all_findings(u))
    other = [r for r in u["repos"] if r["repo"] == "acme/other"][0]
    assert other["metrics"].get("commits_all") == 1


def test_g3_unlisted_corrupt_repo_is_checked(tmp_path):
    root = build_github_tree(tmp_path / "x")
    shutil.copytree(bare_of(root), bare_of(root, name="other"))
    o = bare_of(root, name="other")
    sha = subprocess.run(["git", "-C", str(o), "rev-parse", "HEAD:a.txt"], capture_output=True, text=True).stdout.strip()
    (o / "objects" / sha[:2]).chmod(0o755)
    (o / "objects" / sha[:2] / sha[2:]).unlink()
    u = analyse(root)
    other = [r for r in u["repos"] if r["repo"] == "acme/other"][0]
    assert any(x["check"] == "git_integrity" and x["level"] == "FAIL" for x in other["findings"])


def test_g3_empty_wiki_fails_and_good_wiki_passes(tmp_path):
    root = build_github_tree(tmp_path / "x")
    wiki = bare_of(root, name="widgets.wiki")
    subprocess.run(["git", "init", "-q", "--bare", str(wiki)], check=True)
    f = pick(ce.check_path(root), "git_integrity", "FAIL")
    assert f and "wiki" in f[0]["detail"]
    shutil.rmtree(wiki)
    make_bare(wiki, ())
    assert ce.check_path(root)["verdict"] == "COMPLETE"


# ---------------------------------------------------------------- G4

def test_g4_empty_kinds_are_info_kind_absent(tmp_path):
    rep = ce.check_path(build_github_tree(tmp_path / "x"))
    absent = {f["item"] for f in pick(rep, "kind_absent", "INFO")}
    assert {"releases", "milestones", "issue_comments", "commit_comments"} <= absent
    assert "labels" not in absent and "issues" not in absent
    assert rep["verdict"] == "COMPLETE"


def test_g4_issues_kind_missing_when_git_closes_an_issue(tmp_path):
    root = build_github_tree(tmp_path / "x")
    (root / "issues_000001.json").unlink()
    commit_bytes(bare_of(root), b"fix it, closes #10\n")
    rep = ce.check_path(root)
    assert pick(rep, "kind_missing", "FAIL", "issues")
    assert not pick(rep, "kind_absent", item="issues")


def test_g4_issues_kind_missing_when_comments_reference_them(tmp_path):
    root = build_github_tree(tmp_path / "x")
    (root / "issues_000001.json").unlink()
    (root / "issue_comments_000001.json").write_text(json.dumps([{
        "type": "issue_comment", "url": f"{BASE}/issues/10#issuecomment-1", "issue": f"{BASE}/issues/10",
        "user": ALICE, "created_at": "2025-01-01T00:00:00Z"}]))
    assert pick(ce.check_path(root), "kind_missing", "FAIL", "issues")


def test_g4_kind_missing_when_for_check_counts_it(tmp_path):
    root = build_github_tree(tmp_path / "x")
    u = analyse(root, fc=a_fc(releases=4))
    assert any(x["check"] == "kind_missing" and x.get("item") == "releases" and x["level"] == "FAIL"
               for x in all_findings(u))


def test_g4_huge_pr_number_does_not_blow_up_number_gaps(tmp_path):
    root = build_github_tree(tmp_path / "x", prs=(1, 200_000_000))
    t = time.time()
    rep = ce.check_path(root)
    assert time.time() - t < 20
    w = pick(rep, "number_gaps", "WARN")
    assert w and "199999987" in w[0]["detail"] or w and "unaccounted" in w[0]["detail"]


def test_g4_first_gaps_counts_without_enumerating():
    total, gaps = ce.first_gaps({1, 5, 200_000_000})
    assert total == 200_000_000 - 3 and gaps == [2, 3, 4, 6, 7, 8, 9, 10, 11, 12]


# ---------------------------------------------------------------- G5 / G6

def test_g5_typeless_pr_record_still_gets_core_field_checks(tmp_path):
    root = build_github_tree(tmp_path / "x")

    def strip(d):
        d[0].pop("type")
        d[0].pop("title")
    edit_json(root / "pull_requests_000001.json", strip)
    f = pick(ce.check_path(root), "record_incomplete", "FAIL")
    assert any(any("title" in m for m in x["missing"]) for x in f)


def test_g6_field_in_majority_must_be_in_all(tmp_path):
    root = build_github_tree(tmp_path / "x", prs=(1, 2, 3, 4))

    def add(d):
        for x in d[:3]:
            x["mergeable"] = True
    edit_json(root / "pull_requests_000001.json", add)
    f = pick(ce.check_path(root), "record_incomplete", "FAIL")
    assert any("pull/4:mergeable" in m for x in f for m in x["missing"])


def test_g6_field_in_half_of_records_is_not_flagged(tmp_path):
    root = build_github_tree(tmp_path / "x", prs=(1, 2, 3, 4))

    def add(d):
        for x in d[:2]:
            x["mergeable"] = True
    edit_json(root / "pull_requests_000001.json", add)
    assert ce.check_path(root)["verdict"] == "COMPLETE"


def test_g6_null_value_is_not_a_missing_key(tmp_path):
    root = build_github_tree(tmp_path / "x", prs=(1, 2, 3, 4))

    def add(d):
        for i, x in enumerate(d):
            x["mergeable"] = None if i == 3 else True
    edit_json(root / "pull_requests_000001.json", add)
    assert ce.check_path(root)["verdict"] == "COMPLETE"


def test_g6_nested_dict_field_peer_gap(tmp_path):
    root = build_github_tree(tmp_path / "x", prs=(1, 2, 3, 4))

    def add(d):
        for x in d[:3]:
            x["head"]["user"] = ALICE
    edit_json(root / "pull_requests_000001.json", add)
    f = pick(ce.check_path(root), "record_incomplete", "FAIL")
    assert any("pull/4:head.user" in m for x in f for m in x["missing"])


# ---------------------------------------------------------------- G7

def test_g7_orphan_comment_in_multi_repo_archive_fails(tmp_path):
    root = build_github_tree(tmp_path / "x")
    add_repo(root, "gadgets")
    g = "https://github.com/acme/gadgets"
    (root / "issue_comments_000001.json").write_text(json.dumps([{
        "type": "issue_comment", "url": f"{g}/issues/999#issuecomment-1", "issue": f"{g}/issues/999",
        "user": ALICE, "created_at": "2025-01-01T00:00:00Z"}]))
    f = pick(ce.check_path(root), "comments_parent", "FAIL")
    assert f and 999 in f[0]["missing"]


def test_g7_multi_repo_comment_for_exported_issue_is_fine(tmp_path):
    root = build_github_tree(tmp_path / "x")
    add_repo(root, "gadgets")
    (root / "issue_comments_000001.json").write_text(json.dumps([{
        "type": "issue_comment", "url": f"{BASE}/issues/10#issuecomment-1", "issue": f"{BASE}/issues/10",
        "user": ALICE, "created_at": "2025-01-01T00:00:00Z"}]))
    assert not pick(ce.check_path(root), "comments_parent")


# ---------------------------------------------------------------- G8

def test_g8_for_check_label_not_matching_any_of_two_repos_fails(tmp_path):
    root = build_github_tree(tmp_path / "x")
    add_repo(root, "gadgets")
    u = analyse(root, fc=a_fc(label="renamed/thing"))
    f = [x for x in all_findings(u) if x["check"] == "for_check_unmatched"]
    assert len(f) >= 2 and all(x["level"] == "FAIL" for x in f)
    assert any("renamed/thing" in x["detail"] for x in f)


def test_g8_for_check_section_without_unit_fails(tmp_path):
    root = build_github_tree(tmp_path / "x")
    fc = a_fc()
    fc2 = a_fc(label="other/gone")
    for repo, section, key, value in [(r, s, k, v) for t in ("before", "after") for r in ("other/gone",)
                                      for s, k, v in [(f"counts_{t}", "prs", 1)]]:
        fc._add(repo, section, key, value)
    assert fc2 and fc
    f = [x for x in all_findings(analyse(root, fc=fc)) if x["check"] == "for_check_unmatched"]
    assert f and "other/gone" in f[0]["detail"] and f[0]["level"] == "FAIL"


# ---------------------------------------------------------------- G9

def test_g9_live_failure_is_fail_not_warn(tmp_path):
    root = build_github_tree(tmp_path / "x")

    def boom(owner, repo):
        raise RuntimeError("401 bad credentials")
    rep = ce.check_path(root, live=boom)
    f = pick(rep, "live_check_failed", "FAIL")
    assert f and "401" in f[0]["detail"] and rep["verdict"] == "INCOMPLETE"


# ---------------------------------------------------------------- G10

def test_g10_dict_form_user_reference_is_checked(tmp_path):
    root = build_github_tree(tmp_path / "x")
    edit_json(root / "pull_requests_000001.json", lambda d: d[0].update(user={"url": "https://github.com/mallory"}))
    f = pick(ce.check_path(root), "dangling_reference", "FAIL")
    assert f and "mallory" in f[0]["missing"][0]


def test_g10_dict_form_known_user_is_fine(tmp_path):
    root = build_github_tree(tmp_path / "x")
    edit_json(root / "pull_requests_000001.json", lambda d: d[0].update(user={"url": ALICE}))
    assert ce.check_path(root)["verdict"] == "COMPLETE"


def test_g10_review_with_null_pull_request_fails(tmp_path):
    root = build_github_tree(tmp_path / "x")
    (root / "pull_request_reviews_000001.json").write_text(json.dumps([{
        "type": "pull_request_review", "url": f"{BASE}/pull/1#review-1", "user": ALICE, "pull_request": None}]))
    f = pick(ce.check_path(root), "record_incomplete", "FAIL")
    assert any("pull_request" in m for x in f for m in x["missing"])


def test_g10_tarball_path_traversal_is_a_failure(tmp_path):
    root = build_github_tree(tmp_path / "x")
    (tmp_path / "secret.txt").write_text("host file")
    edit_json(root / "issues_000001.json",
              lambda d: d[0].update(attachments=[{"asset_url": "tarball://root/../secret.txt"}]))
    f = pick(ce.check_path(root), "file_refs", "FAIL")
    assert f and "../secret.txt" in f[0]["missing"]


def test_g10_percent_encoded_attachment_name_resolves(tmp_path):
    root = build_github_tree(tmp_path / "x")
    (root / "attachments" / "1").mkdir(parents=True)
    (root / "attachments" / "1" / "my file.png").write_bytes(b"png")
    edit_json(root / "issues_000001.json",
              lambda d: d[0].update(attachments=[{"asset_url": "tarball://root/attachments/1/my%20file.png"}]))
    assert ce.check_path(root)["verdict"] == "COMPLETE"


# ---------------------------------------------------------------- C1 / C2

def test_c1_invalid_utf8_commit_message_does_not_crash(tmp_path):
    root = build_github_tree(tmp_path / "x")
    commit_bytes(bare_of(root), b"caf\xe9 \xff\xfe broken encoding\n")
    rep = ce.check_path(root)
    assert rep["verdict"] == "COMPLETE", [f for f in rep["findings"] if f["level"] == "FAIL"]


def test_c1_invalid_utf8_json_is_metadata_json_fail(tmp_path):
    root = build_github_tree(tmp_path / "x")
    (root / "labels_000001.json").write_bytes(b'[{"name": "caf\xe9"}]')
    f = pick(ce.check_path(root), "metadata_json", "FAIL")
    assert f and "labels_000001.json" in f[0]["detail"]


@pytest.mark.parametrize("content", ["null", '"a string"', "42", "[1, 2, 3]", '[{"type": "label"}, null]'])
def test_c2_malformed_page_content_is_metadata_json_not_crash(tmp_path, content):
    root = build_github_tree(tmp_path / "x")
    (root / "milestones_000001.json").write_text(content)
    u = analyse(root)
    assert any(x["check"] == "metadata_json" and x["level"] == "FAIL" and "milestones_000001.json" in x["detail"]
               for x in all_findings(u))
    assert not any(x["check"] == "analysis_error" for x in all_findings(u))


def test_c2_null_repository_url_does_not_crash(tmp_path):
    root = build_github_tree(tmp_path / "x")
    edit_json(root / "repositories_000001.json", lambda d: d[0].update(url=None))
    u = analyse(root)
    assert not any(x["check"] == "analysis_error" for x in all_findings(u))
    assert u["repos"]


def test_c2_gitlab_ndjson_non_dict_lines_and_null_label(tmp_path):
    from test_check_export import _gitlab_tree
    root = _gitlab_tree(tmp_path / "gl")
    nd = root / "tree" / "project"
    (nd / "issues.ndjson").write_text('[1, 2]\n"str"\n' + json.dumps(
        {"iid": 1, "title": "t", "state": "opened", "created_at": "x", "label_links": [{"label": None}, None]}) + "\n")
    u = ce.analyse_unit("gl", root, "gitlab-export", [], tmp_path, None, True, False, None)
    fs = all_findings(u)
    assert any(x["check"] == "metadata_json" for x in fs)
    assert not any(x["check"] == "analysis_error" for x in fs)


# ---------------------------------------------------------------- C3 / contract 3, 4

def _fake_slow_git(tmp_path, monkeypatch):
    d = tmp_path / "fakebin"
    d.mkdir()
    g = d / "git"
    g.write_text("#!/bin/sh\nexec sleep 5\n")
    g.chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}:{os.environ['PATH']}")


def test_c3_git_timeout_is_a_fail_finding(tmp_path, monkeypatch):
    root = build_github_tree(tmp_path / "x")
    _fake_slow_git(tmp_path, monkeypatch)
    monkeypatch.setenv("GARMR_GIT_TIMEOUT", "0.3")
    monkeypatch.setenv("GARMR_FSCK_TIMEOUT", "0.3")
    t = time.time()
    u = analyse(root)
    assert time.time() - t < 30
    f = [x for x in all_findings(u) if x["check"] == "timeout"]
    assert f and f[0]["level"] == "FAIL" and f[0]["item"]
    assert any(r["findings"] for r in u["repos"])


def test_c3_fsck_has_its_own_timeout(tmp_path, monkeypatch):
    root = build_github_tree(tmp_path / "x")
    monkeypatch.setenv("GARMR_FSCK_TIMEOUT", "0.0001")
    f = pick(ce.check_path(root), "timeout", "FAIL")
    assert f and "fsck" in f[0]["item"]


def test_c3_commit_messages_stream(tmp_path):
    bare = make_bare(tmp_path / "s.git", ())
    commit_bytes(bare, b"second\n\nbody line\n")
    msgs = list(ce.commit_messages(bare))
    assert msgs == ["second\n\nbody line", "init"]


def test_c3_records_are_indexed_once_per_repo(tmp_path):
    kinds = {"issue_comments": [{"url": f"{BASE}/issues/1#c{i}", "issue": f"{BASE}/issues/1"} for i in range(5)],
             "issues": [{"url": "https://github.com/acme/other/issues/1"}]}
    idx = ce.index_by_repo(kinds)
    assert len(idx["https://github.com/acme/widgets"]["issue_comments"]) == 5
    assert len(idx["https://github.com/acme/other"]["issues"]) == 1


def test_contract_scratch_env_is_used_for_temp_dirs(tmp_path, monkeypatch):
    seen = []
    real = ce.tempfile.mkdtemp
    monkeypatch.setattr(ce.tempfile, "mkdtemp", lambda *a, **k: seen.append(k.get("dir")) or real(*a, **k))
    monkeypatch.setenv("GARMR_SCRATCH", str(tmp_path / "scr"))
    ce.check_path(build_github_tree(tmp_path / "x"))
    assert seen and all(d == str(tmp_path / "scr") for d in seen)


def test_contract_analyse_unit_never_raises_and_keeps_a_repo_row(tmp_path, monkeypatch):
    root = build_github_tree(tmp_path / "x")

    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(ce, "check_github", boom)
    u = analyse(root)
    assert u["repos"] and any(x["check"] == "analysis_error" and x["level"] == "FAIL" for x in u["unit_findings"])
    assert u["repos"][0]["provider"] == "github" and u["repos"][0].get("placeholder")


def test_contract_github_layout_without_repositories_json_gets_placeholder(tmp_path):
    root = tmp_path / "x"
    root.mkdir()
    (root / "schema.json").write_text("{}")
    u = analyse(root)
    assert len(u["repos"]) == 1 and u["repos"][0]["placeholder"] is True
    assert any(x["check"] == "required_file" for x in u["unit_findings"])


def test_contract_environment_problem_propagates_from_scratch(tmp_path, monkeypatch):
    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(ce.tempfile, "mkdtemp", full)
    with pytest.raises(ce.EnvironmentProblem) as ei:
        ce.check_path(build_github_tree(tmp_path / "x"))
    assert ei.value.kind == "disk_full"


# ---------------------------------------------------------------- C4 / C5

def _tar_with_unsafe(path):
    with tarfile.open(path, "w:gz") as t:
        good = tarfile.TarInfo("ok/file.txt")
        data = b"hello"
        good.size = len(data)
        t.addfile(good, io.BytesIO(data))
        evil = tarfile.TarInfo("../evil.txt")
        evil.size = 3
        t.addfile(evil, io.BytesIO(b"bad"))
        link = tarfile.TarInfo("ok/abs")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/hostname"
        t.addfile(link)
        hard = tarfile.TarInfo("ok/hard")
        hard.type = tarfile.LNKTYPE
        hard.linkname = "../../outside"
        t.addfile(hard)
        dev = tarfile.TarInfo("ok/null")
        dev.type = tarfile.CHRTYPE
        t.addfile(dev)
    return path


def test_c4_unsafe_members_are_skipped_and_reported_not_corrupt(tmp_path):
    arc = _tar_with_unsafe(tmp_path / "a" / "unsafe.tar.gz") if (tmp_path / "a").mkdir() is None else None
    dest = tmp_path / "out" / "d"
    skipped = []
    assert ce.extract(arc, dest, skipped) is None
    assert (dest / "ok" / "file.txt").read_text() == "hello"
    assert not (tmp_path / "out" / "evil.txt").exists() and not (dest.parent / "evil.txt").exists()
    assert not (dest / "ok" / "abs").exists()
    assert set(skipped) == {"../evil.txt", "ok/abs", "ok/hard", "ok/null"}


def test_c4_unsafe_member_warn_through_check_path(tmp_path):
    (tmp_path / "in").mkdir()
    arc = _tar_with_unsafe(tmp_path / "in" / "unsafe.tar.gz")
    rep = ce.check_path(arc, workdir=tmp_path / "w")
    w = pick(rep, "unsafe_member", "WARN")
    assert w and w[0]["found"] == 4 and "../evil.txt" in w[0]["missing"]
    assert "archive_integrity" not in names(rep)
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "w" / "evil.txt").exists()


@pytest.mark.parametrize("code,kind", [(errno.ENOSPC, "disk_full"), (errno.EDQUOT, "disk_full"),
                                       (errno.EFBIG, "disk_full"), (errno.EACCES, "permission"),
                                       (errno.EROFS, "permission"), (errno.EIO, "io")])
def test_c4_host_errors_raise_environment_problem(tmp_path, monkeypatch, code, kind):
    (tmp_path / "a").mkdir()
    arc = _tar_with_unsafe(tmp_path / "a" / "x.tar.gz")

    def fail(self, *a, **k):
        raise OSError(code, "boom")
    monkeypatch.setattr(tarfile.TarFile, "extract", fail)
    with pytest.raises(ce.EnvironmentProblem) as ei:
        ce.extract(arc, tmp_path / "d")
    assert ei.value.kind == kind


def test_c4_truncated_archive_is_still_archive_integrity(tmp_path, gh_tree):
    tgz = tar_it(gh_tree, tmp_path / "m.tar.gz")
    tgz.write_bytes(tgz.read_bytes()[: tgz.stat().st_size // 2])
    assert isinstance(ce.extract(tgz, tmp_path / "d"), str)


@pytest.mark.parametrize("exc,word", [(RuntimeError("File 'a' is encrypted, password required for extraction"), "encrypted"),
                                      (NotImplementedError("compression type 99 (unknown)"), "compression"),
                                      (zipfile.LargeZipFile("too big"), "too large")])
def test_c5_extra_zip_exceptions_become_archive_errors(tmp_path, monkeypatch, exc, word):
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("x.txt", "x")

    def bad(self, *a, **k):
        raise exc
    monkeypatch.setattr(zipfile.ZipFile, "extractall", bad)
    err = ce.extract(z, tmp_path / "d")
    assert isinstance(err, str) and word in err


# ---------------------------------------------------------------- P1 (GitLab)

def test_p1_project_json_under_tree_and_bundle_one_level_down(tmp_path):
    from test_check_export import _gitlab_tree
    root = _gitlab_tree(tmp_path / "gl")
    (root / "tree" / "project.json").write_text("{}")
    (root / "project.json").unlink()
    (root / "bundles").mkdir()
    (root / "project.bundle").rename(root / "bundles" / "project.bundle")
    rep = ce.check_path(root)
    assert rep["units"][0]["layout"] == "gitlab-export"
    assert rep["verdict"] == "COMPLETE", [f for f in rep["findings"] if f["level"] == "FAIL"]


def test_p1_group_label_missing_from_project_labels_only_warns(tmp_path):
    from test_check_export import _gitlab_tree
    root = _gitlab_tree(tmp_path / "gl", labels=("bug",), used=("bug",))
    nd = root / "tree" / "project" / "merge_requests.ndjson"
    rows = [json.loads(x) for x in nd.read_text().splitlines()]
    rows[0]["label_links"].append({"label": {"title": "company-wide", "group_id": 7, "project_id": None}})
    nd.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = ce.check_path(root)
    assert rep["verdict"] == "COMPLETE" and pick(rep, "labels", "WARN")


def test_p1_mr_refs_fetch_failure_is_a_warn(tmp_path, monkeypatch):
    from test_check_export import _gitlab_tree
    root = _gitlab_tree(tmp_path / "gl")
    real = ce.run_cmd

    def fake(cmd, what, timeout=None, **kw):
        if what == "git fetch":
            return subprocess.CompletedProcess(cmd, 1, "", "fatal: nope")
        return real(cmd, what, timeout, **kw)
    monkeypatch.setattr(ce, "run_cmd", fake)
    w = pick(ce.check_path(root), "git_refs", "WARN")
    assert w and "nope" in w[0]["detail"]


def test_p1_gitlab_closes_reference_without_record_fails(tmp_path):
    from test_check_export import _gitlab_tree
    src = tmp_path / "gl_src.git"
    root = _gitlab_tree(tmp_path / "gl")
    clone = tmp_path / "c.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(root / "project.bundle"), str(clone)], check=True, capture_output=True)
    commit_bytes(clone, b"fix, closes #55\n")
    (root / "project.bundle").unlink()
    subprocess.run(["git", "-C", str(clone), "bundle", "create", str(root / "project.bundle"), "--all"],
                   check=True, capture_output=True)
    assert src
    f = pick(ce.check_path(root), "number_refs", "FAIL")
    assert f and 55 in f[0]["missing"]


# ---------------------------------------------------------------- P2 / P3

def test_p2_unmerged_pr_merge_commit_sha_missing_only_warns(tmp_path):
    root = build_github_tree(tmp_path / "x")
    edit_json(root / "pull_requests_000001.json", lambda d: d[0].update(merge_commit_sha="a" * 40))
    rep = ce.check_path(root)
    assert rep["verdict"] == "COMPLETE" and pick(rep, "commits", "WARN")


def test_p2_other_sha_keys_missing_only_warn_even_on_merged_pr(tmp_path):
    root = build_github_tree(tmp_path / "x")
    edit_json(root / "pull_requests_000001.json",
              lambda d: d[0].update(merged_at="2025-01-02T00:00:00Z", commit_id="c" * 40,
                                    before_commit_oid="d" * 40, after_commit_oid="e" * 40))
    rep = ce.check_path(root)
    assert rep["verdict"] == "COMPLETE" and pick(rep, "commits", "WARN")


def test_p2_sha256_hex_is_recognised():
    assert ce.HEX_SHA.match("a" * 64) and ce.HEX_SHA.match("a" * 40) and not ce.HEX_SHA.match("a" * 50)


def test_p2_sha256_repo_checked(tmp_path):
    root = build_github_tree(tmp_path / "x")
    shutil.rmtree(bare_of(root))
    w = tmp_path / "w256"
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    subprocess.run(["git", "init", "-q", "-b", "main", "--object-format=sha256", str(w)], check=True, env=env)
    (w / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(w), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(w), "commit", "-q", "-m", "i"], check=True, env=env)
    for n in (1, 2, 3):
        subprocess.run(["git", "-C", str(w), "update-ref", f"refs/pull/{n}/head", "HEAD"], check=True, env=env)
    subprocess.run(["git", "clone", "-q", "--bare", str(w), str(bare_of(root))], check=True, env=env)
    for n in (1, 2, 3):
        subprocess.run(["git", "-C", str(bare_of(root)), "update-ref", f"refs/pull/{n}/head", "HEAD"], check=True, env=env)
    edit_json(root / "pull_requests_000001.json", lambda d: d[0].update(merged_at="2025-01-02T00:00:00Z",
                                                                          merge_commit_sha="f" * 64))
    f = pick(ce.check_path(root), "commits", "FAIL")
    assert f and "f" * 64 in f[0]["missing"]


@pytest.mark.parametrize("who", ["dependabot%5Bbot%5D", "github-actions[bot]", "ghost"])
def test_p3_bots_and_ghost_only_warn(tmp_path, who):
    root = build_github_tree(tmp_path / "x")
    edit_json(root / "pull_requests_000001.json", lambda d: d[0].update(user=f"https://github.com/{who}"))
    rep = ce.check_path(root)
    assert rep["verdict"] == "COMPLETE" and pick(rep, "dangling_reference", "WARN", "bots")


# ---------------------------------------------------------------- P4

def test_p4_cross_repo_closes_is_ignored(tmp_path):
    root = build_github_tree(tmp_path / "x")
    commit_bytes(bare_of(root), b"closes other-org/other-repo#77\n")
    rep = ce.check_path(root)
    assert rep["verdict"] == "COMPLETE" and not pick(rep, "number_refs", "FAIL")


def test_p4_closes_list_is_parsed(tmp_path):
    root = build_github_tree(tmp_path / "x")
    commit_bytes(bare_of(root), b"closes #10, #11 and #12\n")
    f = pick(ce.check_path(root), "number_refs", "FAIL")
    assert f and f[0]["missing"] == [11, 12]


def test_p4_same_repo_slug_counts(tmp_path):
    root = build_github_tree(tmp_path / "x")
    commit_bytes(bare_of(root), b"fixes acme/widgets#42\n")
    assert 42 in pick(ce.check_path(root), "number_refs", "FAIL")[0]["missing"]


def test_p4_bare_mention_of_unknown_number_only_warns(tmp_path):
    root = build_github_tree(tmp_path / "x")
    commit_bytes(bare_of(root), b"see #999 for context\n")
    rep = ce.check_path(root)
    assert rep["verdict"] == "COMPLETE"
    w = pick(rep, "number_refs", "WARN", "mentions")
    assert w and 999 in w[0]["missing"]


# ---------------------------------------------------------------- P5

def test_p5_collaborators_key_absent_is_skipped_with_info(tmp_path):
    root = build_github_tree(tmp_path / "x")
    u = analyse(root, fc=a_fc(collaborators=5))
    fs = all_findings(u)
    assert not [x for x in fs if x["check"] == "live_counts"]
    assert any(x["check"] == "for_check_unknown" and x["item"] == "collaborators" and x["level"] == "INFO" for x in fs)


def test_p5_collaborator_shortfall_is_only_a_warn(tmp_path):
    root = build_github_tree(tmp_path / "x")
    edit_json(root / "repositories_000001.json", lambda d: d[0].update(collaborators=[]))
    fs = all_findings(analyse(root, fc=a_fc(collaborators=3)))
    c = [x for x in fs if x["check"] == "live_counts" and x["item"] == "collaborators"]
    assert c and c[0]["level"] == "WARN"
    assert not [x for x in fs if x["level"] == "FAIL"]


# ---------------------------------------------------------------- P6

def _empty_repo_tree(tmp_path, **repo_fields):
    root = build_github_tree(tmp_path / "x", prs=(), pr_meta=[])
    shutil.rmtree(bare_of(root))
    subprocess.run(["git", "init", "-q", "--bare", str(bare_of(root))], check=True)
    edit_json(root / "repositories_000001.json", lambda d: d[0].update(**repo_fields))
    return root


def test_p6_no_refs_fails_by_default(tmp_path):
    rep = ce.check_path(_empty_repo_tree(tmp_path))
    assert [f for f in pick(rep, "git_integrity") if "no refs" in f["detail"]][0]["level"] == "FAIL"


def test_p6_no_refs_is_warn_when_record_says_empty(tmp_path):
    rep = ce.check_path(_empty_repo_tree(tmp_path, size=0))
    assert [f for f in pick(rep, "git_integrity") if "no refs" in f["detail"]][0]["level"] == "WARN"


def test_p6_no_refs_is_warn_when_for_check_says_zero_commits(tmp_path):
    root = _empty_repo_tree(tmp_path)
    fs = all_findings(analyse(root, fc=a_fc(prs=0, issues=1, labels=2, branches=0, commits=0)))
    assert [f for f in fs if f["check"] == "git_integrity" and "no refs" in f["detail"]][0]["level"] == "WARN"


# ---------------------------------------------------------------- P7

def _paged_prs(root, sizes):
    for p in root.glob("pull_requests_*.json"):
        p.unlink()
    n = 0
    for i, size in enumerate(sizes, 1):
        (root / f"pull_requests_{i:06d}.json").write_text(json.dumps([full_pr(n + k + 1) for k in range(size)]))
        n += size
    return n


def test_p7_flags_only_pages_below_median_of_non_final(tmp_path):
    total = sum([3, 3, 2, 3, 1])
    root = build_github_tree(tmp_path / "x", prs=range(1, total + 1))
    _paged_prs(root, [3, 3, 2, 3, 1])
    f = pick(ce.check_path(root), "page_partial", "FAIL")
    assert f and f[0]["missing"] == ["pull_requests_000003.json"]


def test_p7_larger_non_final_page_does_not_flag_the_normal_ones(tmp_path):
    total = sum([3, 3, 3, 5, 2])
    root = build_github_tree(tmp_path / "x", prs=range(1, total + 1))
    _paged_prs(root, [3, 3, 3, 5, 2])
    assert not pick(ce.check_path(root), "page_partial")


# ---------------------------------------------------------------- P8 / P9

def test_p8_case_difference_between_url_and_directory_is_fine(tmp_path):
    root = build_github_tree(tmp_path / "x")
    (root / "repositories" / "acme").rename(root / "repositories" / "Acme")
    (root / "repositories" / "Acme" / "widgets.git").rename(root / "repositories" / "Acme" / "Widgets.git")
    rep = ce.check_path(root)
    assert rep["verdict"] == "COMPLETE", [f for f in rep["findings"] if f["level"] == "FAIL"]


def test_p8_records_with_other_case_url_still_attributed(tmp_path):
    root = build_github_tree(tmp_path / "x")
    edit_json(root / "pull_requests_000001.json",
              lambda d: [x.update(repository=BASE.replace("acme", "ACME")) for x in d])
    rep = ce.check_path(root)
    assert rep["verdict"] == "COMPLETE", [f for f in rep["findings"] if f["level"] == "FAIL"]


def test_p9_dangling_symlinks_do_not_crash(tmp_path):
    root = build_github_tree(tmp_path / "x")
    os.symlink(tmp_path / "nowhere", root / "labels_000009.json")
    (root / "attachments").mkdir()
    os.symlink(tmp_path / "nowhere", root / "attachments" / "dangling")
    edit_json(root / "issues_000001.json",
              lambda d: d[0].update(attachments=[{"asset_url": "tarball://root/attachments/dangling"}]))
    u = analyse(root)
    fs = all_findings(u)
    assert not any(x["check"] == "analysis_error" for x in fs)
    assert any(x["check"] == "metadata_json" for x in fs) and any(x["check"] == "file_refs" for x in fs)
