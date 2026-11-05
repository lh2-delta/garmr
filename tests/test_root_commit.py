"""Recorded root commit ids against the archive's roots (Squirrel L8 root commit identity)."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import batch  # noqa: E402
import check_export as ce  # noqa: E402
from conftest import OWNER, REPO, b_read, build_github_tree, make_for_check, tar_it  # noqa: E402
from garmr import forcheck  # noqa: E402

A, B = "a" * 40, "b" * 40


def test_for_check_root_keys_are_read_from_any_section():
    fc = forcheck.ForCheck("x.for-check.csv", [("acme/widgets", "meta", "root_commit", A),
                                                ("acme/widgets", "counts_before", "roots", f"{B}, {A}"),
                                                ("acme/widgets", "meta", "state", "exported")])
    assert fc.recorded_roots("acme/widgets") == {A, B}


def test_recorded_root_missing_from_archive_fails():
    out = ce.root_findings("r", {A}, {"roots": {B}})
    assert out[0]["level"] == "FAIL" and out[0]["check"] == "root_commit" and out[0]["missing"] == [A]


def test_matching_roots_are_clean_and_extra_roots_warn():
    assert ce.root_findings("r", {A}, {"roots": {A}}) == []
    w = ce.root_findings("r", {A}, {"roots": {A, B}})
    assert w[0]["level"] == "WARN" and w[0]["item"] == "extra"


def test_nothing_recorded_or_unknown_roots_is_not_judged():
    assert ce.root_findings("r", set(), {"roots": {A}}) == []
    assert ce.root_findings("r", {A}, {}) == []


def _root_of(tree):
    bare = tree / "repositories" / OWNER / f"{REPO}.git"
    return subprocess.run(["git", "-C", str(bare), "rev-list", "--max-parents=0", "--all"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _run(tmp_path, root):
    inp, out, work = tmp_path / "in", tmp_path / "out", tmp_path / "work"
    inp.mkdir()
    tree = build_github_tree(tmp_path / "t")
    archive = tar_it(tree, inp / "acme-widgets.tar.gz")
    make_for_check(archive, tree, meta={"root_commit": root or _root_of(tree)})
    batch.run(inp, out, work)
    return b_read(out / "completeness_report.csv")[0], b_read(out / "missing_items.csv")


def test_end_to_end_matching_root_is_clean(tmp_path):
    rep, miss = _run(tmp_path, None)
    assert "root_commit" not in rep["failed_checks"] and "root_commit" not in rep["warned_checks"]


def test_end_to_end_wrong_root_fails(tmp_path):
    rep, miss = _run(tmp_path, "c" * 40)
    assert rep["status"] == "INCOMPLETE" and "root_commit" in rep["failed_checks"]
    assert [m for m in miss if m["check"] == "root_commit"]
