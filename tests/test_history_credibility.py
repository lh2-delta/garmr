"""History credibility signals ported from Squirrel's L10.3: commits dated before git existed and placeholder identities.
Synthetic repos only; both are WARNs (heuristics, not proof)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_export as ce  # noqa: E402
from test_history_forgery import DAY, commit, repo, scan  # noqa: E402


def items(findings):
    return {f.get("item"): f for f in findings if f["check"] == "history_forgery"}


def test_commit_dated_before_git_existed_warns(tmp_path):
    d = repo(tmp_path)
    old = 1000000000  # 2001-09-09, before git's first release (2005-04-07)
    commit(d, 0, adate=old, cdate=old)
    commit(d, 1, adate=old + DAY, cdate=old + DAY)
    f = items(scan(d)[0])
    assert "pre_git_date" in f
    assert f["pre_git_date"]["level"] == "WARN"
    assert f["pre_git_date"]["found"] == 2
    assert ce.HISTORY_TAG_OF_SIGNAL["pre_git_date"] == "suspected_forged"


def test_placeholder_identities_warn(tmp_path):
    d = repo(tmp_path)
    t = time.time() - 30 * DAY
    commit(d, 0, author=("root", "root@localhost"), adate=t, cdate=t)
    commit(d, 1, author=("unknown", "unknown@x"), adate=t + DAY, cdate=t + DAY)
    commit(d, 2, author=("alice", "alice@example.com"), adate=t + 2 * DAY, cdate=t + 2 * DAY)
    f = items(scan(d)[0])
    assert "placeholder_identity" in f
    assert f["placeholder_identity"]["found"] == 2
    assert f["placeholder_identity"]["level"] == "WARN"


def test_normal_history_has_neither_signal(tmp_path):
    d = repo(tmp_path)
    t = time.time() - 30 * DAY
    for i in range(3):
        commit(d, i, author=(f"dev{i}", f"dev{i}@example.com"), adate=t + i * DAY, cdate=t + i * DAY)
    f = items(scan(d)[0])
    assert "pre_git_date" not in f and "placeholder_identity" not in f
