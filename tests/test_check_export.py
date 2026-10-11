import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_export as ce  # noqa: E402
from conftest import BASE, build_github_tree, tar_it  # noqa: E402


def fails(report):
    return [f for f in report["findings"] if f["level"] == "FAIL"]


def check_names(report, level="FAIL"):
    return {f["check"] for f in report["findings"] if f["level"] == level}


def test_complete_github_tree_passes(gh_tree):
    rep = ce.check_path(gh_tree)
    assert rep["verdict"] == "COMPLETE", fails(rep)


def test_missing_pr_metadata_fails_and_names_numbers(tmp_path):
    root = build_github_tree(tmp_path / "x", prs=(1, 2, 3), pr_meta=[1, 3])
    rep = ce.check_path(root)
    assert rep["verdict"] == "INCOMPLETE"
    f = [x for x in fails(rep) if x["check"] == "pull_requests"][0]
    assert "2" in f["detail"] and "3 PR" in f["detail"] or "missing" in f["detail"]


def test_label_referenced_but_not_exported_fails(tmp_path):
    root = build_github_tree(tmp_path / "x", labels=("feature",), issue_labels=("bug",))
    assert "labels" in check_names(ce.check_path(root))


def test_no_labels_file_but_referenced_fails(tmp_path):
    root = build_github_tree(tmp_path / "x", issue_labels=("bug",))
    (root / "labels_000001.json").unlink()
    assert "labels" in check_names(ce.check_path(root))


def test_zero_byte_file_fails(tmp_path):
    root = build_github_tree(tmp_path / "x", zero_byte="milestones_000001.json")
    names = check_names(ce.check_path(root))
    assert "zero_byte" in names


def test_zero_byte_allowlisted_is_warn(tmp_path):
    root = build_github_tree(tmp_path / "x", zero_byte=".gitkeep")
    rep = ce.check_path(root)
    assert rep["verdict"] == "COMPLETE"
    assert "zero_byte" in check_names(rep, "WARN")


def test_missing_git_object_fails(tmp_path):
    root = build_github_tree(tmp_path / "x")
    bare = root / "repositories" / "acme" / "widgets.git"
    import subprocess
    subprocess.run(["git", "-C", str(bare), "repack", "-a", "-d", "-q"], check=True)
    for p in (bare / "objects" / "pack").glob("*.pack"):
        data = p.read_bytes()[:20]
        p.unlink()
        p.write_bytes(data)
    rep = ce.check_path(root)
    assert "git_integrity" in check_names(rep)


def test_tarball_roundtrip_passes(tmp_path, gh_tree):
    tgz = tar_it(gh_tree, tmp_path / "migration.tar.gz")
    rep = ce.check_path(tgz, workdir=tmp_path / "w")
    assert rep["verdict"] == "COMPLETE", fails(rep)


def test_truncated_tarball_fails(tmp_path, gh_tree):
    tgz = tar_it(gh_tree, tmp_path / "migration.tar.gz")
    tgz.write_bytes(tgz.read_bytes()[: tgz.stat().st_size // 2])
    rep = ce.check_path(tgz, workdir=tmp_path / "w")
    assert rep["verdict"] == "INCOMPLETE"
    assert "archive_integrity" in check_names(rep)


def test_zip_of_tarballs_like_download_all(tmp_path, gh_tree):
    tgz = tar_it(gh_tree, tmp_path / "acme-widgets.tar.gz")
    z = tmp_path / "run.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
        zf.write(tgz, tgz.name)
    rep = ce.check_path(z, workdir=tmp_path / "w")
    assert rep["verdict"] == "COMPLETE", fails(rep)
    assert len(rep["units"]) == 1


def test_live_counts_mismatch_fails(gh_tree):
    answers = {"prs": 5, "issues": 1, "labels": 2, "milestones": 0, "releases": 0}
    rep = ce.check_path(gh_tree, live=lambda owner, repo: answers)
    f = [x for x in fails(rep) if x["check"] == "live_counts"]
    assert f and f[0]["item"] == "prs" and f[0]["missing_count"] == 2


def test_live_counts_match_passes(gh_tree):
    answers = {"prs": 3, "issues": 1, "labels": 2, "milestones": 0, "releases": 0}
    rep = ce.check_path(gh_tree, live=lambda owner, repo: answers)
    assert rep["verdict"] == "COMPLETE", fails(rep)


def test_cli_exit_codes(tmp_path, gh_tree):
    assert ce.main([str(gh_tree)]) == 0
    root = build_github_tree(tmp_path / "bad", prs=(1, 2), pr_meta=[1])
    assert ce.main([str(root)]) == 1
    assert ce.main([str(tmp_path / "nope")]) == 2


def test_json_report_written(tmp_path, gh_tree):
    out = tmp_path / "r.json"
    ce.main([str(gh_tree), "--json", str(out)])
    assert out.exists() and '"verdict"' in out.read_text()


def test_plain_git_dir_only_git_checks(tmp_path):
    from conftest import make_bare
    bare = make_bare(tmp_path / "plain.git", (1,))
    rep = ce.check_path(bare)
    assert rep["units"][0]["layout"] == "plain-git"
    assert rep["verdict"] == "COMPLETE", fails(rep)


def _gitlab_tree(root, mrs_git=(1, 2), mrs_meta=(1, 2), labels=("bug",), used=("bug",)):
    import json
    import subprocess
    from conftest import make_bare
    bare = make_bare(root.parent / "gl_src.git", ())
    for n in mrs_git:
        subprocess.run(["git", "-C", str(bare), "update-ref", f"refs/merge-requests/{n}/head", "HEAD"], check=True)
    (root / "tree" / "project").mkdir(parents=True)
    (root / "project.json").write_text("{}")
    (root / "VERSION").write_text("0.2.4")
    subprocess.run(["git", "-C", str(bare), "bundle", "create", str(root / "project.bundle"), "--all"],
                   check=True, capture_output=True)
    nd = root / "tree" / "project"
    (nd / "merge_requests.ndjson").write_text("\n".join(json.dumps({
        "iid": n, "title": f"mr {n}", "state": "merged", "source_branch": "a", "target_branch": "main",
        "created_at": "2025-01-01", "label_links": [{"label": {"title": t}} for t in used]}) for n in mrs_meta) + "\n")
    (nd / "labels.ndjson").write_text("\n".join(json.dumps({"title": t, "color": "#fff"}) for t in labels) + "\n")
    return root


def test_gitlab_complete(tmp_path):
    rep = ce.check_path(_gitlab_tree(tmp_path / "gl"))
    assert rep["units"][0]["layout"] == "gitlab-export"
    assert rep["verdict"] == "COMPLETE", fails(rep)


def test_gitlab_missing_mr_and_label(tmp_path):
    rep = ce.check_path(_gitlab_tree(tmp_path / "gl", mrs_meta=(1,), labels=("feature",)))
    assert {"merge_requests", "labels"} <= check_names(rep)


def _embed_labels(root, names):
    import json
    p = root / "repositories_000001.json"
    repo = json.loads(p.read_text())
    repo[0]["labels"] = [{"url": f"{BASE}/labels/{n}", "name": n} for n in names]
    p.write_text(json.dumps(repo))
    (root / "labels_000001.json").unlink(missing_ok=True)


def test_labels_embedded_in_repository_record_count_as_exported(tmp_path):
    root = build_github_tree(tmp_path / "x", issue_labels=("bug",))
    _embed_labels(root, ["bug", "feature"])
    assert ce.check_path(root)["verdict"] == "COMPLETE"


def test_embedded_labels_still_flag_missing(tmp_path):
    root = build_github_tree(tmp_path / "x", issue_labels=("bug",))
    _embed_labels(root, ["feature"])
    assert "labels" in check_names(ce.check_path(root))


def test_issue_number_gap_is_warned(tmp_path):
    # PRs 1,2,3 + issue #10 => numbers 4..9 unaccounted for
    rep = ce.check_path(build_github_tree(tmp_path / "x"))
    w = [f for f in rep["findings"] if f["check"] == "number_gaps"]
    assert w and w[0]["level"] == "WARN" and "6" in w[0]["detail"]


def test_event_pointing_at_missing_pr_fails(tmp_path):
    import json
    root = build_github_tree(tmp_path / "x")
    (root / "issue_events_000001.json").write_text(json.dumps(
        [{"type": "issue_event", "url": f"{BASE}/pull/99#event-1", "pull_request": f"{BASE}/pull/99"}]))
    assert "events_parent" in check_names(ce.check_path(root))
