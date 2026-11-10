"""Beyond PRs/labels: anything the archive refers to must exist, files must be complete, counts must agree."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_export as ce  # noqa: E402
from conftest import BASE, OWNER, build_github_tree, full_pr  # noqa: E402


def names(rep, level="FAIL"):
    return {f["check"] for f in rep["findings"] if f["level"] == level}


def one(rep, check, level="FAIL"):
    return [f for f in rep["findings"] if f["check"] == check and f["level"] == level][0]


def edit_json(path, fn):
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data))


def test_dangling_user_reference_fails(tmp_path):
    root = build_github_tree(tmp_path / "x")
    edit_json(root / "pull_requests_000001.json", lambda d: d[0].update(user="https://github.com/mallory"))
    f = one(ce.check_path(root), "dangling_reference")
    assert "mallory" in f["missing"][0]


def test_known_user_reference_ok(tmp_path):
    root = build_github_tree(tmp_path / "x")
    edit_json(root / "pull_requests_000001.json", lambda d: d[0].update(user="https://github.com/alice"))
    assert ce.check_path(root)["verdict"] == "COMPLETE"


def test_dangling_repo_reference_only_warns(tmp_path):
    root = build_github_tree(tmp_path / "x")
    edit_json(root / "pull_requests_000001.json",
              lambda d: d[0].update(head={"ref": "x", "sha": "1" * 40, "repo": "https://github.com/forker/widgets"}))
    rep = ce.check_path(root)
    assert rep["verdict"] == "COMPLETE" and "dangling_reference" in names(rep, "WARN")


def test_merge_commit_sha_missing_from_git_fails(tmp_path):
    root = build_github_tree(tmp_path / "x")
    edit_json(root / "pull_requests_000001.json",
              lambda d: d[0].update(merge_commit_sha="a" * 40, merged_at="2025-01-02T00:00:00Z"))
    assert "a" * 40 in one(ce.check_path(root), "commits")["missing"]


def test_existing_commit_sha_ok(tmp_path):
    import subprocess
    root = build_github_tree(tmp_path / "x")
    bare = root / "repositories" / OWNER / "widgets.git"
    sha = subprocess.run(["git", "-C", str(bare), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    edit_json(root / "pull_requests_000001.json", lambda d: d[0].update(merge_commit_sha=sha))
    assert ce.check_path(root)["verdict"] == "COMPLETE"


def test_head_base_sha_missing_only_warns(tmp_path):
    root = build_github_tree(tmp_path / "x")
    edit_json(root / "pull_requests_000001.json", lambda d: d[0].update(head={"ref": "x", "sha": "b" * 40, "repo": BASE}))
    rep = ce.check_path(root)
    assert rep["verdict"] == "COMPLETE" and "commits" in names(rep, "WARN")


def test_tarball_file_reference_missing_fails(tmp_path):
    root = build_github_tree(tmp_path / "x")
    edit_json(root / "issues_000001.json",
              lambda d: d[0].update(attachments=[{"asset_url": "tarball://root/attachments/1/a.png"}]))
    assert "attachments/1/a.png" in one(ce.check_path(root), "file_refs")["missing"]


def test_tarball_file_reference_present_ok(tmp_path):
    root = build_github_tree(tmp_path / "x")
    (root / "attachments" / "1").mkdir(parents=True)
    (root / "attachments" / "1" / "a.png").write_bytes(b"png")
    edit_json(root / "issues_000001.json",
              lambda d: d[0].update(attachments=[{"asset_url": "tarball://root/attachments/1/a.png"}]))
    assert ce.check_path(root)["verdict"] == "COMPLETE"


def _prs(nums):
    return [full_pr(n) for n in nums]


def test_missing_page_file_in_sequence_fails(tmp_path):
    root = build_github_tree(tmp_path / "x", prs=(1, 2, 3, 4))
    edit_json(root / "pull_requests_000001.json", lambda d: d.__delitem__(slice(None)) or d.extend(_prs([1, 2])))
    (root / "pull_requests_000003.json").write_text(json.dumps(_prs([3, 4])))
    f = one(ce.check_path(root), "page_sequence")
    assert f["missing"] == ["pull_requests_000002.json"]


def test_short_non_last_page_fails(tmp_path):
    root = build_github_tree(tmp_path / "x", prs=(1, 2, 3))
    edit_json(root / "pull_requests_000001.json", lambda d: d.__delitem__(slice(None)) or d.extend(_prs([1])))
    (root / "pull_requests_000002.json").write_text(json.dumps(_prs([2, 3])))
    assert "page_partial" in names(ce.check_path(root))


def test_last_page_may_be_short(tmp_path):
    root = build_github_tree(tmp_path / "x", prs=(1, 2, 3))
    edit_json(root / "pull_requests_000001.json", lambda d: d.__delitem__(slice(None)) or d.extend(_prs([1, 2])))
    (root / "pull_requests_000002.json").write_text(json.dumps(_prs([3])))
    assert ce.check_path(root)["verdict"] == "COMPLETE"


def test_required_file_missing_fails(tmp_path):
    root = build_github_tree(tmp_path / "x")
    (root / "users_000001.json").unlink()
    assert "required_file" in names(ce.check_path(root))


LIVE_OK = {"prs": 3, "issues": 1, "labels": 2, "milestones": 0, "releases": 0, "issue_comments": 0,
           "review_comments": 0, "commit_comments": 0, "branches": 1, "tags": 0, "collaborators": 0, "commits": 1}


def test_live_each_entity_checked_and_reported_separately(tmp_path):
    root = build_github_tree(tmp_path / "x")
    api = dict(LIVE_OK, issue_comments=7, branches=4)
    rep = ce.check_path(root, live=lambda o, r: api)
    live = [f for f in rep["findings"] if f["check"] == "live_counts" and f["level"] == "FAIL"]
    assert {f["item"]: f["missing_count"] for f in live} == {"issue_comments": 7, "branches": 3}


def test_live_all_match_ok(tmp_path):
    root = build_github_tree(tmp_path / "x")
    assert ce.check_path(root, live=lambda o, r: LIVE_OK)["verdict"] == "COMPLETE"


def test_live_archive_has_more_than_api_only_warns(tmp_path):
    root = build_github_tree(tmp_path / "x")
    rep = ce.check_path(root, live=lambda o, r: dict(LIVE_OK, labels=1))
    assert rep["verdict"] == "COMPLETE" and "live_counts" in names(rep, "WARN")


def test_live_commit_count_compared_with_git(tmp_path):
    root = build_github_tree(tmp_path / "x")
    rep = ce.check_path(root, live=lambda o, r: dict(LIVE_OK, commits=50))
    f = [x for x in rep["findings"] if x["check"] == "live_counts" and x.get("item") == "commits"][0]
    assert f["missing_count"] == 49


def test_gitlab_uploads_referenced_but_missing(tmp_path):
    from test_check_export import _gitlab_tree
    root = _gitlab_tree(tmp_path / "gl")
    nd = root / "tree" / "project" / "issues.ndjson"
    nd.write_text(json.dumps({"iid": 1, "description": "see /uploads/" + "ab" * 16 + "/shot.png"}) + "\n")
    f = one(ce.check_path(root), "file_refs")
    assert "uploads/" + "ab" * 16 + "/shot.png" in f["missing"]


def test_gitlab_lfs_objects_listed_but_not_present(tmp_path):
    from test_check_export import _gitlab_tree
    root = _gitlab_tree(tmp_path / "gl")
    (root / "tree" / "project" / "lfs_objects.ndjson").write_text(json.dumps({"oid": "c" * 64}) + "\n")
    assert "file_refs" in names(ce.check_path(root))


# ---------------------------------------------------------------- git is the ground truth

def _commit(root, message):
    import subprocess
    bare = root / "repositories" / OWNER / "widgets.git"
    tree = subprocess.run(["git", "-C", str(bare), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True).stdout.strip()
    parent = subprocess.run(["git", "-C", str(bare), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": "/usr/bin:/bin"}
    sha = subprocess.run(["git", "-C", str(bare), "commit-tree", tree, "-p", parent, "-m", message],
                         capture_output=True, text=True, env=env, check=True).stdout.strip()
    subprocess.run(["git", "-C", str(bare), "update-ref", "refs/heads/main", sha], check=True, env=env)
    return sha


def test_closes_reference_in_git_without_record_fails(tmp_path):
    root = build_github_tree(tmp_path / "x")
    _commit(root, "fix login bug (closes #42)")
    assert 42 in one(ce.check_path(root), "number_refs")["missing"]


def test_closes_reference_with_issue_record_ok(tmp_path):
    root = build_github_tree(tmp_path / "x")
    _commit(root, "fix thing, closes #10")  # issue #10 exists in the fixture
    assert ce.check_path(root)["verdict"] == "COMPLETE"


def test_plain_hash_mention_is_ignored(tmp_path):
    root = build_github_tree(tmp_path / "x")
    _commit(root, "see discussion in #999 for context")
    assert ce.check_path(root)["verdict"] == "COMPLETE"


def test_merge_commit_in_git_but_pr_not_marked_merged_fails(tmp_path):
    root = build_github_tree(tmp_path / "x")
    _commit(root, "Merge pull request #2 from a/b2")
    f = one(ce.check_path(root), "pr_state")
    assert f["missing"] == ["#2"]


def test_merge_commit_in_git_and_pr_merged_ok(tmp_path):
    root = build_github_tree(tmp_path / "x")
    _commit(root, "Merge pull request #2 from a/b2")
    edit_json(root / "pull_requests_000001.json",
              lambda d: [x.update(merged_at="2025-01-01T00:00:00Z") for x in d if x["url"].endswith("/pull/2")])
    assert ce.check_path(root)["verdict"] == "COMPLETE"


def test_pr_in_metadata_but_ref_missing_in_git_fails(tmp_path):
    root = build_github_tree(tmp_path / "x", prs=(1, 2), pr_meta=[1, 2, 3])
    f = one(ce.check_path(root), "git_refs")
    assert f["missing"] == [3]


def test_release_tag_missing_from_git_fails(tmp_path):
    root = build_github_tree(tmp_path / "x")
    (root / "releases_000001.json").write_text(json.dumps(
        [{"type": "release", "url": f"{BASE}/releases/1", "repository": BASE, "tag_name": "v9.9"}]))
    assert one(ce.check_path(root), "release_tag")["missing"] == ["v9.9"]


def test_default_branch_missing_from_git_fails(tmp_path):
    root = build_github_tree(tmp_path / "x")
    edit_json(root / "repositories_000001.json", lambda d: d[0].update(default_branch="trunk"))
    assert "default_branch" in names(ce.check_path(root))


def test_pr_record_missing_core_fields_fails(tmp_path):
    root = build_github_tree(tmp_path / "x")
    def fill(d):
        d[1].pop("title")
        d[1]["head"] = {"ref": "x", "repo": BASE}
    edit_json(root / "pull_requests_000001.json", fill)
    f = one(ce.check_path(root), "record_incomplete")
    assert any("title" in m for m in f["missing"]) and any("head.sha" in m for m in f["missing"])


def test_record_missing_field_all_peers_have_is_flagged(tmp_path):
    root = build_github_tree(tmp_path / "x", prs=(1, 2, 3, 4))
    def fill(d):
        for x in d:
            x["merged_at"] = None
        d[2].pop("merged_at")
    edit_json(root / "pull_requests_000001.json", fill)
    f = one(ce.check_path(root), "record_incomplete")
    assert any("merged_at" in m and "pull/3" in m for m in f["missing"])


def test_records_with_same_shape_are_fine(tmp_path):
    root = build_github_tree(tmp_path / "x", prs=(1, 2, 3, 4))
    assert ce.check_path(root)["verdict"] == "COMPLETE"
