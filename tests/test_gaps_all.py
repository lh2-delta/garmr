"""`gaps` for every failed count-vs-reference check and the `excess` column (see README, 'Gaps column')."""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import batch  # noqa: E402
from conftest import COUNTS_OK, build_github_tree, make_for_check, tar_it  # noqa: E402
from test_batch import lost_merge_commits, read, run, run_tree  # noqa: E402


def run_fc(tmp_path, after=None, tree_kw=None, mutate=None):
    """One archive plus a for-check whose counts are COUNTS_OK overridden by `after` (before == after)."""
    inp, out, work = tmp_path / "in", tmp_path / "out", tmp_path / "work"
    inp.mkdir()
    tree = build_github_tree(tmp_path / "t_fc", **(tree_kw or {}))
    if mutate:
        mutate(tree)
    arch = tar_it(tree, inp / "acme-widgets.tar.gz")
    counts = dict(COUNTS_OK, **(after or {}))
    make_for_check(arch, tree, before=counts, after=counts)
    batch.run(inp, out, work)
    return read(out / "completeness_report.csv"), read(out / "missing_items.csv")


def sub(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def miss_of(miss, check, item=None):
    return [m for m in miss if m["check"] == check and (item is None or m["item"] == item)][0]


def zero_byte(tree):
    (tree / "milestones_000001.json").write_text("")      # a structural failure: no count reference


def test_gaps_for_check_issues_and_labels(tmp_path):
    rep, miss = run_fc(tmp_path, after={"issues": 5, "labels": 4})
    assert rep[0]["gaps"] == "live_counts:issues=4/5;live_counts:labels=2/4"
    for item in ("issues", "labels"):
        row = miss_of(miss, "live_counts", item)
        assert f"live_counts:{item}={row['missing_count']}/{row['expected']}" in rep[0]["gaps"].split(";")


def test_gaps_for_check_branches_tags_milestones(tmp_path):
    rep, _ = run_fc(tmp_path, after={"branches": 3, "tags": 2, "milestones": 4})
    assert rep[0]["gaps"] == "live_counts:branches=2/3;live_counts:milestones=4/4;live_counts:tags=2/2"


def test_gaps_labels_referenced_but_not_exported(tmp_path):
    rep, miss = run_fc(tmp_path, after={"labels": 1}, tree_kw={"labels": ("bug",), "issue_labels": ("bug", "feature")})
    row = miss_of(miss, "labels")
    assert rep[0]["gaps"] == f"labels={row['missing_count']}/{row['expected']}" == "labels=1/2"


def test_gaps_org_mismatch_items(tmp_path):
    rows = [{"org": "acme", "repo": "widgets", "total_commits": "999", "loc": "", "merged_prs": "9"}]
    rep, miss, _ = run(tmp_path, org_rows=rows)
    c, p = miss_of(miss, "org_report_mismatch", "commits"), miss_of(miss, "org_report_mismatch", "prs")
    assert rep[0]["gaps"] == (f"org_report_mismatch:commits={c['missing_count']}/{c['expected']};"
                              f"org_report_mismatch:prs={p['missing_count']}/{p['expected']}")
    assert c["expected"] == "999" and rep[0]["gaps"].endswith("org_report_mismatch:prs=6/9")


def test_gaps_mixed_existing_first_then_sorted(tmp_path):
    rep, _ = run_fc(tmp_path, after={"labels": 4, "issues": 5}, mutate=lost_merge_commits(2))
    assert rep[0]["gaps"] == "commits=2/2;live_counts:issues=4/5;live_counts:labels=2/4"


def test_gaps_blank_for_structural_failure(tmp_path):
    rep, _ = run_fc(tmp_path, mutate=zero_byte)
    assert "zero_byte" in rep[0]["failed_checks"] and rep[0]["gaps"] == ""


def test_gaps_structural_not_listed_next_to_count(tmp_path):
    rep, _ = run_fc(tmp_path, after={"issues": 5}, mutate=zero_byte)
    assert "zero_byte" in rep[0]["failed_checks"] and rep[0]["gaps"] == "live_counts:issues=4/5"


def test_gaps_filter_rules_unit():
    def row(check, item, exp, miss, repo="r"):
        return {"repo": repo, "check": check, "item": item, "expected": exp, "missing_count": miss}
    rows = [row("page_sequence", "issues", 10, 2), row("git_integrity", "git_integrity", 10, 2),
            row("repo_not_exported", "requested", 10, 2), row("labels", "labels", 0, 0),
            row("labels", "labels", 4, ""), row("refs", "refs", 8, 1), row("file_refs", "uploads", 5, 5),
            row("commits", "commits", 3, 1, repo="*")]
    assert batch.gaps_column(rows) == "file_refs:uploads=5/5;refs=1/8"


def test_excess_for_check(tmp_path):
    rep, _ = run_fc(tmp_path, after={"prs": 1})
    assert rep[0]["extra_metrics"] == "prs_metadata:+2" and rep[0]["excess"] == "prs_metadata=+2/1"


def test_excess_org(tmp_path):
    rows = [{"org": "acme", "repo": "widgets", "total_commits": "", "loc": "", "merged_prs": "1"}]
    rep, _, _ = run(sub(tmp_path, "o"), org_rows=rows)
    assert rep[0]["excess"] == "prs_git=+2/1;prs_metadata=+2/1" and rep[0]["status"] == "COMPLETE"


def test_excess_unit():
    assert batch.excess_column([]) == "" and batch.excess_column([("commits", 3, 0)]) == ""
    assert batch.excess_column([("commits", 12, 90), ("prs_git", 3, 5)]) == "commits=+12/90;prs_git=+3/5"


def test_excess_blank_when_nothing_extra(tmp_path):
    rep, _ = run_tree(tmp_path)
    assert "excess" in rep[0] and rep[0]["excess"] == ""


def test_columns_on_no_archive_row(tmp_path):
    rows = [{"org": "acme", "repo": "ghost", "total_commits": "5", "loc": "", "merged_prs": ""}]
    rep, _, _ = run(sub(tmp_path, "g"), org_rows=rows)
    ghost = [r for r in rep if r["repo"] == "acme/ghost"][0]
    assert ghost["status"] == "NO_ARCHIVE" and ghost["gaps"] == "" and ghost["excess"] == ""
