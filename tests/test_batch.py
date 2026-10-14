import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import batch  # noqa: E402
import check_export as ce  # noqa: E402
from conftest import build_github_tree, tar_it  # noqa: E402

REPORT_COLS = ["repo", "provider", "archive", "status", "errors", "warnings", "warned_checks", "failed_checks", "gaps", "for_check", "commits_head",
               "commits_all", "measured_branch", "measured_by", "commits_measured", "prs_git", "prs_meta", "prs_merged", "prs_unmerged", "visibility", "extra_metrics", "history_tags", "labels", "issues",
               "branches", "tags", "files", "loc",
               "org_csv_row", "org_commits", "org_loc", "org_prs", "org_branches", "org_tags", "org_files", "flags"]
MISSING_COLS = ["repo", "provider", "archive", "check", "item", "expected", "found", "missing_count",
                "missing_items", "detail"]


def setup(tmp_path, prs=(1, 2, 3), pr_meta=None, org_rows=None, name="acme-widgets"):
    inp, out, work = tmp_path / "in", tmp_path / "out", tmp_path / "work"
    inp.mkdir()
    tree = build_github_tree(tmp_path / f"t_{name}", prs=prs, pr_meta=pr_meta)
    tar_it(tree, inp / f"{name}.tar.gz")
    if org_rows is not None:
        with open(inp / "org_report.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(org_rows[0]))
            w.writeheader()
            w.writerows(org_rows)
    return inp, out, work


def read(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def run(tmp_path, **kw):
    inp, out, work = setup(tmp_path, **kw)
    batch.run(inp, out, work)
    return read(out / "completeness_report.csv"), read(out / "missing_items.csv"), out


def test_headers_and_complete_row(tmp_path):
    rep, miss, out = run(tmp_path)
    assert list(rep[0]) == REPORT_COLS
    assert open(out / "missing_items.csv").readline().strip().split(",") == MISSING_COLS
    assert len(rep) == 1 and rep[0]["status"] == "COMPLETE" and rep[0]["provider"] == "github"
    assert rep[0]["repo"] == "acme/widgets" and rep[0]["prs_git"] == "3" and rep[0]["prs_meta"] == "3"
    assert rep[0]["org_csv_row"] == "n/a" and miss == []


def test_missing_pr_goes_to_missing_csv(tmp_path):
    rep, miss, _ = run(tmp_path, pr_meta=[1, 3])
    assert rep[0]["status"] == "INCOMPLETE" and "pull_requests" in rep[0]["failed_checks"]
    row = [m for m in miss if m["check"] == "pull_requests"][0]
    assert row["repo"] == "acme/widgets" and row["missing_items"] == "2" and row["missing_count"] == "1"
    assert row["expected"] == "3" and row["found"] == "2"


def test_truncated_archive_listed_as_incomplete(tmp_path):
    inp, out, work = setup(tmp_path)
    a = inp / "acme-widgets.tar.gz"
    a.write_bytes(a.read_bytes()[: a.stat().st_size // 2])
    batch.run(inp, out, work)
    rep, miss = read(out / "completeness_report.csv"), read(out / "missing_items.csv")
    assert rep[0]["status"] == "INCOMPLETE" and rep[0]["repo"] == "acme-widgets"
    assert miss[0]["check"] == "archive_integrity"


def test_org_csv_greater_than_archive_is_error(tmp_path):
    rows = [{"org": "acme", "repo": "widgets", "total_commits": "999", "loc": "", "merged_prs": "3"}]
    rep, miss, _ = run(tmp_path, org_rows=rows)
    assert rep[0]["org_csv_row"] == "yes" and rep[0]["org_commits"] == "999"
    assert rep[0]["status"] == "INCOMPLETE"
    m = [x for x in miss if x["check"] == "org_report_mismatch"][0]
    assert m["item"] == "commits" and m["expected"] == "999" and m["found"] == "1" and m["missing_count"] == "998"


def test_org_csv_lower_than_archive_is_only_flagged(tmp_path):
    rows = [{"org": "acme", "repo": "widgets", "total_commits": "0", "loc": "", "merged_prs": "3"}]
    rep, miss, _ = run(tmp_path, org_rows=rows)
    assert rep[0]["status"] == "COMPLETE" and "ORG_LOWER:commits" in rep[0]["flags"]
    assert miss == []


def test_org_csv_equal_numbers_clean(tmp_path):
    rows = [{"org": "acme", "repo": "widgets", "total_commits": "1", "loc": "", "merged_prs": "3"}]
    rep, miss, _ = run(tmp_path, org_rows=rows)
    flags = rep[0]["flags"].split(";")                         # no org-CSV flags; for-check / KIND_ABSENT ones only
    assert "NO_FOR_CHECK" in flags and not [f for f in flags if "ORG" in f or f == "NOT_IN_ORG_CSV"] and miss == []


def test_org_loc_compared(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "measure_loc", lambda bare, ref="HEAD": 100)
    rows = [{"org": "acme", "repo": "widgets", "total_commits": "", "loc": "500", "merged_prs": ""}]
    rep, miss, _ = run(tmp_path, org_rows=rows)
    assert rep[0]["loc"] == "100"
    assert [x for x in miss if x["item"] == "loc"]


def test_org_loc_lower_is_flag_only(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "measure_loc", lambda bare, ref="HEAD": 100)
    rows = [{"org": "acme", "repo": "widgets", "total_commits": "", "loc": "50", "merged_prs": ""}]
    rep, miss, _ = run(tmp_path, org_rows=rows)
    assert "ORG_LOWER:loc" in rep[0]["flags"] and not [x for x in miss if x["item"] == "loc"]


def test_org_repo_without_archive(tmp_path):
    rows = [{"org": "acme", "repo": "widgets", "total_commits": "1", "loc": "", "merged_prs": "3"},
            {"org": "acme", "repo": "ghost", "total_commits": "5", "loc": "", "merged_prs": ""}]
    rep, miss, _ = run(tmp_path, org_rows=rows)
    ghost = [r for r in rep if r["repo"] == "acme/ghost"][0]
    assert ghost["status"] == "NO_ARCHIVE" and ghost["org_csv_row"] == "yes"
    assert [m for m in miss if m["repo"] == "acme/ghost" and m["check"] == "archive_missing"]


def test_archive_not_in_org_csv_is_flagged(tmp_path):
    rows = [{"org": "acme", "repo": "other", "total_commits": "1", "loc": "", "merged_prs": ""}]
    rep, _, _ = run(tmp_path, org_rows=rows)
    r = [x for x in rep if x["repo"] == "acme/widgets"][0]
    assert r["org_csv_row"] == "no" and "NOT_IN_ORG_CSV" in r["flags"]


def test_non_metric_csv_is_not_treated_as_org_report(tmp_path):
    inp, out, work = setup(tmp_path)
    (inp / "notes.csv").write_text("a,b\n1,2\n")
    batch.run(inp, out, work)
    assert read(out / "completeness_report.csv")[0]["org_csv_row"] == "n/a"


def test_main_uses_env_folders(tmp_path, monkeypatch):
    inp, out, work = setup(tmp_path)
    monkeypatch.setenv("EXPORT_CHECK_INPUT", str(inp))
    monkeypatch.setenv("EXPORT_CHECK_OUTPUT", str(out))
    monkeypatch.setenv("EXPORT_CHECK_WORK", str(work))
    assert batch.main([]) == 0
    assert (out / "completeness_report.csv").exists()


def test_main_exit_one_when_any_incomplete(tmp_path, monkeypatch):
    inp, out, work = setup(tmp_path, pr_meta=[1])
    monkeypatch.setenv("EXPORT_CHECK_INPUT", str(inp))
    monkeypatch.setenv("EXPORT_CHECK_OUTPUT", str(out))
    monkeypatch.setenv("EXPORT_CHECK_WORK", str(work))
    assert batch.main([]) == 1


def test_missing_input_folder_exit_two(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPORT_CHECK_INPUT", str(tmp_path / "nope"))
    monkeypatch.setenv("EXPORT_CHECK_OUTPUT", str(tmp_path / "o"))
    monkeypatch.setenv("EXPORT_CHECK_WORK", str(tmp_path / "w"))
    assert batch.main([]) == 2


def test_main_refuses_to_run_without_scc(tmp_path, monkeypatch, capsys):
    # a run without scc would silently skip the LOC floor and look more complete than it is
    inp, out, work = setup(tmp_path)
    monkeypatch.setenv("EXPORT_CHECK_INPUT", str(inp))
    monkeypatch.setenv("EXPORT_CHECK_OUTPUT", str(out))
    monkeypatch.setenv("EXPORT_CHECK_WORK", str(work))
    monkeypatch.setattr(batch.shutil, "which", lambda name: None if name == "scc" else "/usr/bin/" + name)
    assert batch.main([]) == 2
    assert "scc is not installed" in capsys.readouterr().err
    assert not (out / "completeness_report.csv").exists()


# ---------------------------------------------------------------- gaps column

def run_tree(tmp_path, mutate=None, prs=(1, 2, 3), pr_meta=None):
    inp, out, work = tmp_path / "in", tmp_path / "out", tmp_path / "work"
    inp.mkdir()
    tree = build_github_tree(tmp_path / "t_gaps", prs=prs, pr_meta=pr_meta)
    if mutate:
        mutate(tree)
    tar_it(tree, inp / "acme-widgets.tar.gz")
    batch.run(inp, out, work)
    return read(out / "completeness_report.csv"), read(out / "missing_items.csv")


def lost_merge_commits(n):
    """PR records marked merged whose merge_commit_sha is not in the archive's git: n distinct lost commits."""
    def mutate(tree):
        import json
        p = tree / "pull_requests_000001.json"
        recs = json.loads(p.read_text())
        for i, r in enumerate(recs[:n]):
            r["merged_at"] = "2025-01-02T00:00:00Z"
            r["merge_commit_sha"] = f"{i + 1:x}" * 40
        p.write_text(json.dumps(recs))
    return mutate


def test_gaps_missing_pr(tmp_path):
    rep, miss = run_tree(tmp_path, pr_meta=[1, 3])
    row = [m for m in miss if m["check"] == "pull_requests"][0]
    assert rep[0]["gaps"] == f"pull_requests={row['missing_count']}/{row['expected']}" == "pull_requests=1/3"


def test_gaps_missing_commit(tmp_path):
    rep, miss = run_tree(tmp_path, mutate=lost_merge_commits(1))
    row = [m for m in miss if m["check"] == "commits"][0]
    assert rep[0]["gaps"] == f"commits={row['missing_count']}/{row['expected']}" == "commits=1/1"
    assert rep[0]["status"] == "INCOMPLETE"


def test_gaps_both(tmp_path):
    rep, miss = run_tree(tmp_path, mutate=lost_merge_commits(2), prs=(1, 2, 3, 4), pr_meta=[1, 2, 3])
    assert rep[0]["gaps"] == "commits=2/2;pull_requests=1/4"
    for chk in ("commits", "pull_requests"):
        row = [m for m in miss if m["check"] == chk][0]
        assert f"{chk}={row['missing_count']}/{row['expected']}" in rep[0]["gaps"].split(";")


def test_gaps_blank_when_complete(tmp_path):
    rep, _ = run_tree(tmp_path)
    assert rep[0]["status"] == "COMPLETE" and rep[0]["gaps"] == ""


def test_gaps_blank_for_non_count_failure(tmp_path):
    inp, out, work = setup(tmp_path)
    a = inp / "acme-widgets.tar.gz"
    a.write_bytes(a.read_bytes()[: a.stat().st_size // 2])
    batch.run(inp, out, work)
    rep = read(out / "completeness_report.csv")
    assert "gaps" in rep[0] and rep[0]["failed_checks"] == "archive_integrity" and rep[0]["gaps"] == ""


def test_gaps_blank_when_unlisted_check_fails(tmp_path):
    rows = [{"org": "acme", "repo": "widgets", "total_commits": "999", "loc": "", "merged_prs": "3"}]
    rep, _, _ = run(tmp_path, org_rows=rows)
    assert rep[0]["failed_checks"] == "org_report_mismatch" and rep[0]["gaps"] == ""


def test_gaps_column_on_no_archive_row(tmp_path):
    rows = [{"org": "acme", "repo": "ghost", "total_commits": "5", "loc": "", "merged_prs": ""}]
    rep, _, _ = run(tmp_path, org_rows=rows)
    ghost = [r for r in rep if r["repo"] == "acme/ghost"][0]
    assert ghost["status"] == "NO_ARCHIVE" and ghost["gaps"] == ""
