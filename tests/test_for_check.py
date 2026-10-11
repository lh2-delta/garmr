"""Garmr uses <archive>.for-check.csv (written by migration-downloader) for source-side ground truth."""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import batch  # noqa: E402
from conftest import COUNTS_OK, build_github_tree, make_for_check, tar_it  # noqa: E402


def read(p):
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def setup(tmp_path, fc=True, **fc_kwargs):
    inp, out, work = tmp_path / "in", tmp_path / "out", tmp_path / "work"
    inp.mkdir()
    tree = build_github_tree(tmp_path / "tree")
    archive = tar_it(tree, inp / "acme-widgets-42.tar.gz")
    if fc:
        make_for_check(archive, tree, **fc_kwargs)
    return inp, out, work, archive, tree


def run(tmp_path, **kw):
    inp, out, work, *_ = setup(tmp_path, **kw)
    batch.run(inp, out, work, want_loc=False)
    return read(out / "completeness_report.csv"), read(out / "missing_items.csv")


def fail_rows(miss, check, item=None):
    return [m for m in miss if m["check"] == check and (item is None or m["item"] == item)]


def test_matching_for_check_is_complete(tmp_path):
    rep, miss = run(tmp_path)
    assert rep[0]["status"] == "COMPLETE", miss
    assert rep[0]["for_check"] == "yes" and "NO_FOR_CHECK" not in rep[0]["flags"]


def test_no_for_check_flagged_not_failed(tmp_path):
    rep, miss = run(tmp_path, fc=False)
    assert rep[0]["status"] == "COMPLETE" and rep[0]["for_check"] == "no"
    assert "NO_FOR_CHECK" in rep[0]["flags"] and miss == []


def test_archive_sha_mismatch_fails(tmp_path):
    inp, out, work, archive, tree = setup(tmp_path)
    with open(archive, "ab") as f:
        f.write(b"\0" * 512)                       # changed after download (e.g. while copying to S3)
    batch.run(inp, out, work, want_loc=False)
    miss = read(out / "missing_items.csv")
    assert fail_rows(miss, "transfer_integrity", "sha256") and fail_rows(miss, "transfer_integrity", "bytes")


def test_download_shorter_than_server_content_length_fails(tmp_path):
    rep, miss = run(tmp_path, meta={"content_length": 10_000_000})
    f = fail_rows(miss, "transfer_integrity", "content_length")[0]
    assert f["expected"] == "10000000"


def test_listed_file_absent_from_archive_fails(tmp_path):
    inp, out, work, archive, tree = setup(tmp_path, files={"schema.json": 20, "labels_000001.json": 999,
                                                           "milestones_000001.json": 5})
    batch.run(inp, out, work, want_loc=False)
    miss = read(out / "missing_items.csv")
    f = fail_rows(miss, "file_listing", "missing")[0]
    assert "milestones_000001.json" in f["missing_items"]
    assert "labels_000001.json" in fail_rows(miss, "file_listing", "size")[0]["missing_items"]


def test_git_objects_fewer_than_source_listed_fails(tmp_path):
    inp, out, work, archive, tree = setup(tmp_path)
    make_for_check(archive, tree, files={}, objects=(10_000, 5_000_000))
    batch.run(inp, out, work, want_loc=False)
    miss = read(out / "missing_items.csv")
    assert fail_rows(miss, "file_listing", "objects")


def test_requested_repo_not_exported_fails(tmp_path):
    rep, miss = run(tmp_path, requested=("acme/widgets", "acme/gadgets"))
    assert fail_rows(miss, "repo_not_exported")[0]["missing_items"] == "acme/gadgets"


def test_exported_repo_not_in_archive_fails(tmp_path):
    rep, miss = run(tmp_path, exported=("acme/widgets", "acme/other"))
    assert "acme/other" in fail_rows(miss, "repo_not_exported")[0]["missing_items"]


def test_counts_below_source_fail_with_how_many(tmp_path):
    c = dict(COUNTS_OK, prs=5, labels=9)
    rep, miss = run(tmp_path, before=c, after=c)
    prs = fail_rows(miss, "live_counts", "prs")[0]
    assert (prs["expected"], prs["found"], prs["missing_count"]) == ("5", "3", "2")
    assert fail_rows(miss, "live_counts", "labels")[0]["missing_count"] == "7"
    assert rep[0]["status"] == "INCOMPLETE"


def test_count_deleted_during_export_window_is_ok(tmp_path):
    rep, miss = run(tmp_path, before=dict(COUNTS_OK, prs=5), after=dict(COUNTS_OK, prs=3))   # 2 PRs deleted mid-export
    assert not fail_rows(miss, "live_counts")


def test_archive_above_both_snapshots_only_warns(tmp_path):
    rep, miss = run(tmp_path, before=dict(COUNTS_OK, prs=2), after=dict(COUNTS_OK, prs=2))
    assert rep[0]["status"] == "COMPLETE" and not fail_rows(miss, "live_counts")


def test_unknown_source_count_is_skipped_and_flagged(tmp_path):
    c = dict(COUNTS_OK, collaborators="")
    rep, miss = run(tmp_path, before=c, after=c)
    assert rep[0]["status"] == "COMPLETE" and "FOR_CHECK_UNKNOWN:collaborators" in rep[0]["flags"]


def test_ref_on_source_throughout_but_missing_in_archive_fails(tmp_path):
    inp, out, work, archive, tree = setup(tmp_path)
    refs = {"refs/heads/main": "a" * 40, "refs/heads/dev": "b" * 40}
    make_for_check(archive, tree, refs_before=refs, refs_after=refs)
    batch.run(inp, out, work, want_loc=False)
    miss = read(out / "missing_items.csv")
    f = fail_rows(miss, "refs")[0]
    assert f["missing_items"] == "refs/heads/dev"


def test_ref_deleted_during_export_is_ok(tmp_path):
    inp, out, work, archive, tree = setup(tmp_path)
    import subprocess
    bare = tree / "repositories" / "acme" / "widgets.git"
    real = dict(line.split(" ") for line in subprocess.run(
        ["git", "-C", str(bare), "for-each-ref", "--format=%(refname) %(objectname)"],
        capture_output=True, text=True).stdout.splitlines())
    make_for_check(archive, tree, refs_before=dict(real, **{"refs/heads/gone": "c" * 40}), refs_after=real)
    batch.run(inp, out, work, want_loc=False)
    assert not fail_rows(read(out / "missing_items.csv"), "refs")


def test_ref_with_different_sha_only_warns(tmp_path):
    inp, out, work, archive, tree = setup(tmp_path)
    make_for_check(archive, tree, refs_before={"refs/heads/main": "d" * 40}, refs_after={"refs/heads/main": "d" * 40})
    batch.run(inp, out, work, want_loc=False)
    assert read(out / "completeness_report.csv")[0]["status"] == "COMPLETE"


def test_failed_export_state_fails(tmp_path):
    rep, miss = run(tmp_path, meta={"state": "failed"})
    assert fail_rows(miss, "export_state")


def test_for_check_without_archive_is_reported(tmp_path):
    inp, out, work, archive, tree = setup(tmp_path)
    make_for_check(archive, tree).rename(inp / "acme-gone-7.for-check.csv")
    batch.run(inp, out, work, want_loc=False)
    rep = read(out / "completeness_report.csv")
    gone = [r for r in rep if r["status"] == "NO_ARCHIVE"]
    assert gone and gone[0]["for_check"] == "yes"
    assert fail_rows(read(out / "missing_items.csv"), "archive_missing")


def test_for_check_csv_is_not_mistaken_for_org_csv(tmp_path):
    inp, out, work, archive, tree = setup(tmp_path)
    assert batch.load_org_csv(inp) is None


def test_for_check_unreadable_is_flagged_not_crash(tmp_path):
    inp, out, work, archive, tree = setup(tmp_path)
    (inp / "acme-widgets-42.for-check.csv").write_text("not,a,valid\nfile")
    batch.run(inp, out, work, want_loc=False)
    rep = read(out / "completeness_report.csv")
    assert rep[0]["for_check"] == "unreadable" and "FOR_CHECK_UNREADABLE" in rep[0]["flags"]


# ---------------------------------------------------------------- extra coverage

def test_for_check_label_matches_case_insensitively(tmp_path):
    c = dict(COUNTS_OK, prs=5)
    rep, miss = run(tmp_path, before=c, after=c, label="ACME/Widgets")
    assert fail_rows(miss, "live_counts", "prs")


def test_single_repo_for_check_matches_single_unit_whatever_the_label(tmp_path):
    c = dict(COUNTS_OK, prs=5)
    rep, miss = run(tmp_path, before=c, after=c, label="renamed/elsewhere")
    assert fail_rows(miss, "live_counts", "prs")


def test_for_check_in_other_folder_matched_by_meta_archive_name(tmp_path):
    inp, out, work, archive, tree = setup(tmp_path)
    (inp / "meta").mkdir()
    make_for_check(archive, tree).rename(inp / "meta" / "whatever.for-check.csv")
    batch.run(inp, out, work, want_loc=False)
    rep = read(out / "completeness_report.csv")
    assert [r["for_check"] for r in rep] == ["yes"] and rep[0]["status"] == "COMPLETE"


def test_for_check_wins_over_live_api(tmp_path):
    inp, out, work, archive, tree = setup(tmp_path)
    live = lambda o, r: dict(COUNTS_OK, prs=9, commits=1)       # noqa: E731  (API disagrees with the snapshots)
    batch.run(inp, out, work, live=live, want_loc=False)
    assert not fail_rows(read(out / "missing_items.csv"), "live_counts", "prs")


def test_source_default_branch_differs_only_warns(tmp_path):
    import json
    inp, out, work, archive, tree = setup(tmp_path)
    p = tree / "repositories_000001.json"
    data = json.loads(p.read_text())
    data[0]["default_branch"] = "main"
    p.write_text(json.dumps(data))
    archive = tar_it(tree, inp / "acme-widgets-42.tar.gz")
    c = dict(COUNTS_OK, default_branch="develop")
    make_for_check(archive, tree, before=c, after=c)
    batch.run(inp, out, work, want_loc=False)
    assert read(out / "completeness_report.csv")[0]["status"] == "COMPLETE"
    import check_export as ce
    from garmr import forcheck
    u = ce.check_path(tree)   # no for-check: no warning
    assert not [f for f in u["findings"] if f["check"] == "default_branch"]
    fc = forcheck.load(inp / "acme-widgets-42.for-check.csv")
    r = ce.analyse_unit("x", tree, "github-migration", [], tmp_path, fc=fc)["repos"][0]
    w = [f for f in r["findings"] if f["check"] == "default_branch"]
    assert w and w[0]["level"] == "WARN"


def test_file_listing_tolerates_single_wrapper_folder(tmp_path):
    inp, out, work = tmp_path / "in", tmp_path / "out", tmp_path / "work"
    inp.mkdir()
    tree = build_github_tree(tmp_path / "tree")
    import tarfile
    archive = inp / "wrapped-1.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        t.add(tree, arcname="export")
    make_for_check(archive, tree, files={"schema.json": 20}, objects=(0, 0))
    batch.run(inp, out, work, want_loc=False)
    assert not fail_rows(read(out / "missing_items.csv"), "file_listing")


def _gitlab_fc(tmp_path, rows):
    from garmr import forcheck
    return forcheck.ForCheck(tmp_path / "x.for-check.csv", [("g/p", *r) for r in rows])


def test_gitlab_counts_and_refs_use_for_check(tmp_path):
    import check_export as ce
    from test_check_export import _gitlab_tree
    root = _gitlab_tree(tmp_path / "gl")
    fc = _gitlab_fc(tmp_path, [("counts_before", "prs", "5"), ("counts_after", "prs", "5"),
                               ("counts_before", "labels", "1"), ("counts_after", "labels", "1"),
                               ("counts_before", "issues", ""), ("counts_after", "issues", ""),
                               ("ref_before", "refs/heads/dev", "a" * 40), ("ref_after", "refs/heads/dev", "a" * 40),
                               ("ref_before", "refs/merge-requests/9/head", "b" * 40),
                               ("ref_after", "refs/merge-requests/9/head", "b" * 40)])
    r = ce.analyse_unit("gl", root, "gitlab-export", [], tmp_path, fc=fc)["repos"][0]
    fails = {(f["check"], f.get("item")): f for f in r["findings"] if f["level"] == "FAIL"}
    assert fails[("live_counts", "prs")]["missing_count"] == 3
    assert fails[("refs", None)]["missing"] == ["refs/heads/dev"]        # MR refs are not compared on GitLab
    assert ("live_counts", "labels") not in fails
    unknown = {f["item"] for f in r["findings"] if f["check"] == "for_check_unknown"}
    assert "issues" in unknown and "prs" not in unknown                     # blank or absent -> skipped + flagged


# ---------------------------------------------------------------- "Download all (.zip)": tarballs + their for-check.csv inside one zip

def _bundle(tmp_path, names=("acme-widgets-42",), with_fc=(True,), tamper=(False,)):
    """input/bundle.zip holding <name>.tar.gz and (optionally) <name>.for-check.csv, as migration-downloader zips a run."""
    import zipfile
    inp, out, work = tmp_path / "in", tmp_path / "out", tmp_path / "work"
    inp.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir()
    for i, name in enumerate(names):
        tree = build_github_tree(tmp_path / f"tree{i}")
        archive = tar_it(tree, stage / f"{name}.tar.gz")
        if with_fc[i]:
            make_for_check(archive, tree)
        if tamper[i]:
            with open(archive, "ab") as f:
                f.write(b"\0" * 512)
    with zipfile.ZipFile(inp / "bundle.zip", "w", zipfile.ZIP_STORED) as z:
        for p in sorted(stage.iterdir()):
            z.write(p, p.name)
    return inp, out, work


def test_zip_bundle_uses_for_check_inside_the_zip(tmp_path):
    inp, out, work = _bundle(tmp_path)
    batch.run(inp, out, work, want_loc=False)
    rep = read(out / "completeness_report.csv")
    assert len(rep) == 1 and rep[0]["status"] == "COMPLETE", read(out / "missing_items.csv")
    assert rep[0]["for_check"] == "yes" and "NO_FOR_CHECK" not in rep[0]["flags"]
    assert rep[0]["archive"].startswith("bundle.zip") and "acme-widgets-42.tar.gz" in rep[0]["archive"]


def test_zip_bundle_inner_tarball_changed_after_download_fails(tmp_path):
    inp, out, work = _bundle(tmp_path, tamper=(True,))
    batch.run(inp, out, work, want_loc=False)
    assert fail_rows(read(out / "missing_items.csv"), "transfer_integrity", "sha256")


def test_zip_bundle_each_tarball_pairs_with_its_own_for_check(tmp_path):
    inp, out, work = _bundle(tmp_path, names=("acme-a-1", "acme-b-2"), with_fc=(True, False), tamper=(False, False))
    batch.run(inp, out, work, want_loc=False)
    rep = {r["archive"].split("!")[-1]: r for r in read(out / "completeness_report.csv")}
    assert rep["acme-a-1.tar.gz"]["for_check"] == "yes"
    assert rep["acme-b-2.tar.gz"]["for_check"] == "no" and "NO_FOR_CHECK" in rep["acme-b-2.tar.gz"]["flags"]


def test_zip_bundle_for_check_without_its_tarball_is_no_archive(tmp_path):
    import zipfile
    inp, out, work = _bundle(tmp_path)
    stray = tmp_path / "acme-gone-9.for-check.csv"
    stray.write_text("repo,section,key,value\nacme/gone,meta,archive,acme-gone-9.tar.gz\n")
    with zipfile.ZipFile(inp / "bundle.zip", "a") as z:
        z.write(stray, stray.name)
    batch.run(inp, out, work, want_loc=False)
    rep = read(out / "completeness_report.csv")
    assert [r["status"] for r in rep if r["status"] == "NO_ARCHIVE"] == ["NO_ARCHIVE"]
    assert fail_rows(read(out / "missing_items.csv"), "archive_missing")


def test_zip_of_tarballs_without_any_for_check_still_checked(tmp_path):
    inp, out, work = _bundle(tmp_path, with_fc=(False,))
    batch.run(inp, out, work, want_loc=False)
    rep = read(out / "completeness_report.csv")
    assert rep[0]["status"] == "COMPLETE" and "NO_FOR_CHECK" in rep[0]["flags"]
