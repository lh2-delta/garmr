"""Type-aware peer comparison, imported-history PR state, and real missing counts."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_export as ce  # noqa: E402
from conftest import BASE, build_github_tree  # noqa: E402
from test_hardening_logic import bare_of, commit_bytes, edit_json, pick  # noqa: E402

U = "https://github.com/alice"


def ev(n, event, **extra):
    return {"type": "issue_event", "url": f"{BASE}/issues/1/events/{n}", "actor": U, "event": event,
            "created_at": "2025-01-01T00:00:00Z", **extra}


def attach(n, parent, **extra):
    return {"type": "attachment", "url": f"{BASE}/attachments/{n}", "asset_url": "x", parent: f"{BASE}/{parent}/{n}", **extra}


def gaps_of(recs, kind):
    return ce.incomplete_records(recs, ce.CORE_FIELDS.get(kind, []), ce.PEER_GROUP.get(kind))


# ---------------------------------------------------------------- 1 type-aware peers

def test_event_types_with_different_fields_are_not_incomplete():
    recs = [ev(i, "referenced", commit_repository=BASE) for i in range(5)] + [ev(10 + i, "closed") for i in range(5)]
    assert gaps_of(recs, "issue_event") == []
    # without grouping, the same data is flagged once more than half the peers carry the field
    many = [ev(i, "referenced", commit_repository=BASE) for i in range(8)] + [ev(10 + i, "closed") for i in range(3)]
    assert ce.incomplete_records(many, [], None)
    assert gaps_of(many, "issue_event") == []


def test_same_event_type_missing_a_field_still_fails():
    recs = [ev(i, "referenced", commit_repository=BASE) for i in range(5)] + [ev(10, "referenced")]
    assert gaps_of(recs, "issue_event") == [f"{BASE}/issues/1/events/10:commit_repository"]


def test_attachments_with_different_parent_keys_are_not_incomplete():
    recs = ([attach(i, "issue_comment") for i in range(4)] + [attach(10 + i, "pull_request") for i in range(4)]
            + [attach(20 + i, "pull_request_review_comment") for i in range(4)])
    assert gaps_of(recs, "attachment") == []


def test_attachment_missing_a_field_its_siblings_have_fails():
    recs = [attach(i, "issue_comment", asset_name="a") for i in range(5)]
    recs.append(attach(9, "issue_comment"))
    assert gaps_of(recs, "attachment") == [f"{BASE}/attachments/9:asset_name"]


def test_end_to_end_heterogeneous_events_pass_and_broken_one_fails(tmp_path):
    root = build_github_tree(tmp_path / "x")
    events = [ev(i, "referenced", commit_repository=BASE) for i in range(5)] + [ev(10 + i, "closed") for i in range(5)]
    (root / "issue_events_000001.json").write_text(json.dumps(events))
    assert not pick(ce.check_path(root), "record_incomplete", "FAIL")
    del events[0]["commit_repository"]
    (root / "issue_events_000001.json").write_text(json.dumps(events))
    assert pick(ce.check_path(root), "record_incomplete", "FAIL", "issue_event")


# ---------------------------------------------------------------- 3 real counts

def test_missing_count_is_the_real_total_not_the_example_cap(tmp_path):
    root = build_github_tree(tmp_path / "x")
    events = [ev(i, "referenced", commit_repository=BASE) for i in range(600)]
    for e in events[:250]:
        del e["commit_repository"]
    events += [ev(2000 + i, "closed") for i in range(5)]
    (root / "issue_events_000001.json").write_text(json.dumps(events))
    f = pick(ce.check_path(root), "record_incomplete", "FAIL", "issue_event")[0]
    assert len(f["missing"]) == 200 and f["missing_count"] == 250
    assert "250 field(s) missing from 250 record(s)" in f["detail"]


def test_finding_without_cap_has_no_count():
    assert "missing_count" not in ce.finding("FAIL", "x", "d", missing=ce.capped(["a", "b"]))


# ---------------------------------------------------------------- 2 imported history

def _tree(tmp_path, message, created_at=None, prs=(1, 2, 3)):
    root = build_github_tree(tmp_path / "t", prs=prs)
    commit_bytes(bare_of(root), message)
    if created_at:
        edit_json(root / "repositories_000001.json", lambda d: d[0].update(created_at=created_at))
    return root


def test_merge_commit_naming_another_head_branch_only_warns(tmp_path):
    rep = ce.check_path(_tree(tmp_path, b"Merge pull request #2 from Old-Org/bugFix/smallFixes\n"))
    assert pick(rep, "pr_state", "WARN") and not pick(rep, "pr_state", "FAIL")


def test_merge_commit_older_than_the_repo_only_warns(tmp_path):
    rep = ce.check_path(_tree(tmp_path, b"Merge pull request #2 from a/b2\n", created_at="2099-01-01T00:00:00Z"))
    assert pick(rep, "pr_state", "WARN") and not pick(rep, "pr_state", "FAIL")


def test_genuine_unmerged_pr_in_a_normal_repo_still_fails(tmp_path):
    rep = ce.check_path(_tree(tmp_path, b"Merge pull request #2 from a/b2\n"))
    assert pick(rep, "pr_state", "FAIL") and not pick(rep, "pr_state", "WARN")


def test_mixed_imported_and_genuine_keeps_the_genuine_fail(tmp_path):
    rep = ce.check_path(_tree(tmp_path, b"Merge pull request #2 from a/b2\n\nMerge pull request #3 from Old/x\n"))
    assert pick(rep, "pr_state", "FAIL")[0]["missing"] == ["#2"]
    assert "[3]" in pick(rep, "pr_state", "WARN")[0]["detail"]


def test_pull_requests_kind_missing_warns_for_imported_history(tmp_path):
    root = _tree(tmp_path, b"Merge pull request #7 from x/y\n", created_at="2099-01-01T00:00:00Z", prs=())
    rep = ce.check_path(root)
    assert pick(rep, "kind_missing", "WARN", "pull_requests") and not pick(rep, "kind_missing", "FAIL", "pull_requests")


def test_pull_requests_kind_missing_still_fails_in_a_normal_repo(tmp_path):
    rep = ce.check_path(_tree(tmp_path, b"Merge pull request #7 from x/y\n", prs=()))
    assert pick(rep, "kind_missing", "FAIL", "pull_requests")
