"""The reference count is looked for in git history and in metadata separately; more than the reference is an
"Extra ... detected" warning and the `extra_metrics` column. `history_tags` names what is odd about the history."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import batch  # noqa: E402
import check_export as ce  # noqa: E402
from test_batch import run  # noqa: E402
from test_review_fixes import _org  # noqa: E402


def cmp(ref, metrics, metric="prs", tol=0):
    extras = []
    errs, flags, _ = batch.compare_metrics(_org([], metric), {metric: str(ref)}, metrics, tol, extras)
    return errs, flags, extras


# ---------------------------------------------------------------- the reference is looked for on both sides

def test_both_sides_reach_the_reference_and_nothing_is_extra():
    errs, flags, extras = cmp(5, {"prs_git": 5, "prs_merged": 5})
    assert errs == [] and extras == [] and not [f for f in flags if f.startswith("ORG")]


def test_more_than_the_reference_is_an_extra_on_each_side_never_a_failure():
    errs, _, extras = cmp(5, {"prs_git": 8, "prs_merged": 6})
    assert errs == [] and extras == [("prs_git", 3, 5), ("prs_metadata", 1, 5)]


def test_git_short_while_metadata_has_enough_fails_naming_git_history_only():
    errs, _, extras = cmp(5, {"prs_git": 3, "prs_merged": 7})
    assert len(errs) == 1 and errs[0]["item"] == "prs" and errs[0]["missing_count"] == 2
    assert "git history holds 3" in errs[0]["detail"] and "metadata" not in errs[0]["detail"]
    assert extras == [("prs_metadata", 2, 5)]


def test_both_short_is_one_finding_so_a_pr_is_never_counted_twice():
    errs, _, _ = cmp(10, {"prs_git": 4, "prs_merged": 7})
    assert len(errs) == 1 and errs[0]["found"] == 4 and errs[0]["missing_count"] == 6
    assert "git history holds 4" in errs[0]["detail"] and "metadata holds 7" in errs[0]["detail"]


def test_an_unknown_side_is_flagged_not_judged_and_metadata_falls_back_to_all_records():
    errs, flags, extras = cmp(5, {"prs_git": 5})
    assert errs == [] and "ORG_UNCOMPARED:prs_metadata" in flags
    errs, flags, extras = cmp(5, {"prs_git": 5, "prs_meta": 9})    # no merged/closed info in the export: all records bound it
    assert errs == [] and extras == [("prs_metadata", 4, 5)] and not [f for f in flags if f.startswith("ORG_UNC")]
    assert cmp(5, {})[1] == ["ORG_UNCOMPARED:prs"]


def test_other_metrics_report_extras_too_and_keep_the_old_flag():
    errs, flags, extras = cmp(10, {"commits_all": 14, "commits_head": 14}, metric="commits")
    assert errs == [] and extras == [("commits", 4, 10)] and "ORG_LOWER:commits" in flags
    errs, flags, extras = cmp(10, {"commits_all": 30, "commits_head": 10}, metric="commits")  # extra only on other branches
    assert extras == [] and "ORG_LOWER:commits" not in flags


def test_loc_tolerance_still_applies_before_anything_is_extra():
    assert cmp(100, {"loc": 101}, metric="loc", tol=2.0)[2] == []
    assert cmp(100, {"loc": 140}, metric="loc", tol=2.0)[2] == [("loc", 40, 100)]


def test_warning_text_and_column_format():
    extras = [("prs_git", 3, 5), ("commits", 12, 90)]
    warns = batch.extra_findings(extras)
    assert [w["level"] for w in warns] == ["WARN", "WARN"] and warns[0]["check"] == "extra_metrics"
    assert warns[0]["detail"] == "Extra PRs detected: 3 more PRs in git history than the reference's 5"
    assert warns[1]["detail"].startswith("Extra commits detected: 12 more commits")
    assert batch.extra_column(extras) == "prs_git:+3;commits:+12" and batch.extra_column([]) == ""


def test_end_to_end_extras_warn_fill_the_column_and_leave_the_status_alone(tmp_path):
    rows = [{"org": "acme", "repo": "widgets", "total_commits": "", "loc": "", "merged_prs": "1"}]   # the archive has 3 PRs
    rep, miss, _ = run(tmp_path, org_rows=rows)
    r = rep[0]
    assert r["status"] == "COMPLETE" and miss == []
    assert r["extra_metrics"] == "prs_git:+2;prs_metadata:+2" and "extra_metrics" in r["warned_checks"].split(";")


def test_end_to_end_no_extras_leaves_the_column_empty(tmp_path):
    rows = [{"org": "acme", "repo": "widgets", "total_commits": "", "loc": "", "merged_prs": "3"}]
    rep, _, _ = run(tmp_path, org_rows=rows)
    assert rep[0]["extra_metrics"] == "" and "extra_metrics" not in rep[0]["warned_checks"]


# ---------------------------------------------------------------- history tags

def F(level, check, item=None):
    return {"level": level, "check": check, "item": item}


def test_forgery_signals_map_to_tags():
    cases = {"created_after_history": "imported", "future_dated": "suspected_forged", "author_after_committer": "suspected_forged",
             "non_monotonic": "suspected_forged", "bulk_timestamp": "suspected_forged", "single_committer": "suspected_forged",
             "bad_signature": "suspected_forged", "multi_root": "flattened", "squashed": "flattened",
             "unreachable": "rewritten", "reflog_rewrite": "rewritten"}
    for item, tag in cases.items():
        assert ce.history_tags([F("WARN", "history_forgery", item)], {}) == [tag], item


def test_metric_facts_map_to_tags_and_order_is_stable():
    tags = ce.history_tags([F("FAIL", "git_integrity"), F("WARN", "history_forgery", "squashed")],
                           {"fork": True, "imported_prs": True, "base_force_pushes": 3})
    assert tags == ["imported", "forked", "flattened", "rewritten", "faulty"]


def test_nothing_odd_means_no_tags_and_unrelated_findings_add_none():
    assert ce.history_tags([F("FAIL", "pull_requests"), F("WARN", "history_forgery", "git_error"),
                            F("WARN", "git_integrity")], {"base_force_pushes": 0}) == []   # an empty repo (WARN) is not faulty


def test_faulty_needs_a_failed_integrity_check():
    for check in ("git_integrity", "repo_git", "archive_integrity"):
        assert ce.history_tags([F("FAIL", check)], {}) == ["faulty"], check


def test_end_to_end_a_repo_created_long_after_its_history_is_tagged_imported(tmp_path):
    from conftest import build_github_tree
    from test_visibility_prs import run_report, set_repo
    tree = build_github_tree(tmp_path / "tree")
    set_repo(tree, created_at="2999-01-01T00:00:00Z")
    row = run_report(tmp_path, tree)
    assert row["history_tags"].split(";") == ["imported"] and "history_forgery" in row["warned_checks"].split(";")


def test_tags_change_no_verdict_end_to_end(tmp_path):
    rep, miss, _ = run(tmp_path)
    assert rep[0]["status"] == "COMPLETE" and rep[0]["history_tags"] == "" and miss == []
