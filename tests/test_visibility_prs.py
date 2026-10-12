"""completeness_report.csv columns `visibility` and `prs_merged` / `prs_unmerged` / `prs_open`."""
import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import batch  # noqa: E402
import check_export as ce  # noqa: E402
from conftest import build_github_tree, tar_it  # noqa: E402
from garmr import forcheck  # noqa: E402
from test_check_export import _gitlab_tree  # noqa: E402
from test_hardening_logic import edit_json, pick  # noqa: E402


def repo_of(root, layout="github-migration", tmp=None, **kw):
    return ce.analyse_unit("x", root, layout, [], tmp or root.parent, **kw)["repos"][0]


def set_repo(root, **fields):
    edit_json(root / "repositories_000001.json", lambda d: d[0].update(fields))


def set_prs(root, per_pr):
    edit_json(root / "pull_requests_000001.json", lambda d: [p.update(per_pr[i]) for i, p in enumerate(d)])


def run_report(tmp_path, tree, name="acme-widgets-42.tar.gz"):
    inp, out, work = tmp_path / "in", tmp_path / "out", tmp_path / "work"
    inp.mkdir()
    tar_it(tree, inp / name)
    batch.run(inp, out, work, want_loc=False)
    with open(out / "completeness_report.csv", newline="") as f:
        return list(csv.DictReader(f))[0]


# ---------------------------------------------------------------- visibility

@pytest.mark.parametrize("fields,want", [({"private": False}, "public"), ({"private": True}, "private"),
                                         ({"visibility": "internal"}, "internal"), ({}, "unknown"),
                                         ({"private": "yes"}, "unknown")])
def test_github_visibility(tmp_path, fields, want):
    root = build_github_tree(tmp_path / "t")
    set_repo(root, **fields)
    assert repo_of(root)["metrics"]["visibility"] == want


@pytest.mark.parametrize("pj,want", [({"visibility": "private"}, "private"), ({"visibility_level": 20}, "public"),
                                     ({"visibility_level": 10}, "internal"), ({"visibility_level": 0}, "private"),
                                     ({"visibility_level": 7}, "unknown"), ({"is_private": True}, "private"),
                                     ({}, "unknown")])
def test_gitlab_and_bitbucket_style_visibility(tmp_path, pj, want):
    root = _gitlab_tree(tmp_path / "gl")
    (root / "project.json").write_text(json.dumps(pj))
    assert repo_of(root, "gitlab-export")["metrics"]["visibility"] == want


def test_plain_git_is_unknown_and_pr_columns_blank(tmp_path):
    tree = build_github_tree(tmp_path / "tree")
    row = run_report(tmp_path, tree / "repositories" / "acme", "plain.tar.gz")     # bare repo only: no metadata
    assert row["visibility"] == "unknown"
    assert row["prs_merged"] == row["prs_unmerged"] == row["prs_open"] == ""


def test_visibility_mismatch_with_live_api_and_for_check(tmp_path):
    root = build_github_tree(tmp_path / "t")
    set_repo(root, private=True)
    w = pick(repo_of(root, live=lambda o, r: {"visibility": "public"}), "visibility_mismatch", "WARN")
    assert len(w) == 1 and "private" in w[0]["detail"] and "public" in w[0]["detail"]
    assert not pick(repo_of(root, live=lambda o, r: {"visibility": "private"}), "visibility_mismatch")
    assert not pick(repo_of(root, live=lambda o, r: {"visibility": None}), "visibility_mismatch")
    assert not pick(repo_of(root, live=lambda o, r: {}), "visibility_mismatch")
    assert not pick(repo_of(root), "visibility_mismatch")              # no other source: skipped silently
    rows = [("acme/widgets", "counts_before", "visibility", "public"),
            ("acme/widgets", "counts_after", "visibility", "public")]
    fc = forcheck.ForCheck(tmp_path / "x.for-check.csv", rows)
    w = pick(repo_of(root, fc=fc), "visibility_mismatch", "WARN")
    assert w and w[0]["item"] == "for-check"


def test_unknown_archive_visibility_never_mismatches(tmp_path):
    root = build_github_tree(tmp_path / "t")
    assert not pick(repo_of(root, live=lambda o, r: {"visibility": "public"}), "visibility_mismatch")


def test_gitlab_visibility_mismatch_with_for_check(tmp_path):
    root = _gitlab_tree(tmp_path / "gl")
    (root / "project.json").write_text(json.dumps({"visibility": "private"}))
    fc = forcheck.ForCheck(tmp_path / "x.for-check.csv", [("g/p", "counts_after", "visibility", "public")])
    assert pick(repo_of(root, "gitlab-export", fc=fc), "visibility_mismatch", "WARN")


# ---------------------------------------------------------------- PR categories

def prs_of(rec):
    m = rec["metrics"]
    return tuple(m.get(k) for k in ("prs_merged", "prs_unmerged", "prs_open"))


def test_github_pr_categories_from_dates(tmp_path):
    root = build_github_tree(tmp_path / "t")
    set_prs(root, [{"merged_at": "2025-02-01T00:00:00Z", "closed_at": "2025-02-01T00:00:00Z"},
                   {"merged_at": None, "closed_at": "2025-02-02T00:00:00Z"},
                   {"merged_at": None, "closed_at": None}])
    rec = repo_of(root)
    assert prs_of(rec) == (1, 1, 1)
    assert rec["metrics"]["prs_meta"] == 3 and rec["metrics"]["prs_git"] == 3


def test_empty_strings_are_not_dates_and_state_is_fallback(tmp_path):
    root = build_github_tree(tmp_path / "t")
    set_prs(root, [{"merged_at": "", "closed_at": "", "state": "closed"},
                   {"merged_at": "", "closed_at": ""},
                   {"state": "open"}])
    assert prs_of(repo_of(root)) == (0, 1, 2)


def test_duplicate_pr_records_count_once_at_most_advanced_status(tmp_path):
    root = build_github_tree(tmp_path / "t", prs=(1, 2), pr_meta=[1, 1, 2])
    set_prs(root, [{"merged_at": None, "closed_at": None}, {"merged_at": "2025-02-01T00:00:00Z"},
                   {"merged_at": None, "closed_at": None}])
    rec = repo_of(root)
    assert prs_of(rec) == (1, 0, 1) and rec["metrics"]["prs_meta"] == 2


def test_no_pr_records_leaves_blank_not_zero(tmp_path):
    root = build_github_tree(tmp_path / "t", prs=(), pr_meta=[])
    (root / "pull_requests_000001.json").unlink()
    assert prs_of(repo_of(root)) == (None, None, None)


def test_record_with_no_status_fields_is_unknown_not_open(tmp_path):
    root = build_github_tree(tmp_path / "t")          # fixture PRs carry none of merged_at/closed_at/state
    assert prs_of(repo_of(root)) == (None, None, None)


def test_gitlab_mr_categories(tmp_path):
    root = _gitlab_tree(tmp_path / "gl", mrs_git=(1, 2, 3, 4), mrs_meta=(1, 2, 3, 4))
    f = root / "tree" / "project" / "merge_requests.ndjson"
    recs = [json.loads(x) for x in f.read_text().splitlines()]
    recs[0].update(state="merged", merged_at="2025-01-02")
    recs[1].update(state="closed", closed_at="2025-01-02")
    recs[2].update(state="opened")
    recs[3].update(state="closed", closed_at=None)
    f.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    assert prs_of(repo_of(root, "gitlab-export")) == (1, 2, 1)


def test_report_csv_carries_new_columns(tmp_path):
    tree = build_github_tree(tmp_path / "tree")
    set_repo(tree, private=False)
    set_prs(tree, [{"merged_at": "2025-02-01T00:00:00Z"}, {"closed_at": "2025-02-01T00:00:00Z", "merged_at": None},
                   {"merged_at": None, "closed_at": None}])
    row = run_report(tmp_path, tree)
    assert (row["visibility"], row["prs_merged"], row["prs_unmerged"], row["prs_open"]) == ("public", "1", "1", "1")
    assert row["prs_git"] == "3" and row["prs_meta"] == "3"
