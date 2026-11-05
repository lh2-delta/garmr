"""Merged PR records with no trace in git history (Squirrel L5 squashing). A WARN, never a FAIL."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_export as ce  # noqa: E402

BASE = "https://github.com/acme/widgets"


def pr(n, merged=True):
    return {"url": f"{BASE}/pull/{n}", "merged_at": "2025-01-02T00:00:00Z" if merged else None}


def facts(merge=(), squash=(), known=True):
    return {"merge_prs": set(merge), "squash_prs": set(squash), "refs_known": known}


def test_every_merged_pr_traced_is_clean():
    by_num = {1: pr(1), 2: pr(2), 3: pr(3, merged=False)}
    assert ce.squash_findings("acme/widgets", by_num, facts(merge=[1], squash=[2])) == []


def test_merged_prs_without_trace_warn_with_count():
    by_num = {n: pr(n) for n in (1, 2, 3, 4)}
    out = ce.squash_findings("acme/widgets", by_num, facts(merge=[1]))
    assert len(out) == 1
    w = out[0]
    assert w["level"] == "WARN" and w["check"] == "squashed_prs"
    assert w["expected"] == 4 and w["found"] == 1
    assert "3 of 4" in w["detail"]


def test_unmerged_prs_are_not_expected_in_history():
    by_num = {1: pr(1, merged=False), 2: pr(2, merged=False)}
    assert ce.squash_findings("acme/widgets", by_num, facts()) == []


def test_unknown_refs_are_not_judged():
    assert ce.squash_findings("acme/widgets", {1: pr(1)}, facts(known=False)) == []
