"""An org-CSV row whose `error` column is filled was not measured (the profiler failed on that repo, e.g. a UTF-8 decode
error part-way through); its numbers are leftovers, so they are never compared. Ported from Squirrel's summary handling."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_batch import run  # noqa: E402


def test_row_with_error_is_not_compared(tmp_path):
    rows = [{"org": "acme", "repo": "widgets", "total_commits": "999", "loc": "0", "merged_prs": "0",
             "error": "'utf-8' codec can't decode byte 0xc3"}]
    rep, miss, _ = run(tmp_path, org_rows=rows)
    r = rep[0]
    assert r["status"] == "COMPLETE" and miss == []
    assert "ORG_ROW_ERROR" in r["flags"].split(";")
    assert r["org_commits"] == "" and r["org_prs"] == ""
    assert not [f for f in r["flags"].split(";") if f.startswith("ORG_LOWER")]


def test_blank_error_column_is_compared_as_before(tmp_path):
    rows = [{"org": "acme", "repo": "widgets", "total_commits": "999", "loc": "", "merged_prs": "3", "error": ""}]
    rep, miss, _ = run(tmp_path, org_rows=rows)
    assert rep[0]["status"] == "INCOMPLETE" and "ORG_ROW_ERROR" not in rep[0]["flags"]
    assert [m for m in miss if m["check"] == "org_report_mismatch"]


def test_errored_row_does_not_hide_a_good_row_for_the_same_repo(tmp_path):
    rows = [{"org": "acme", "repo": "widgets", "total_commits": "5", "loc": "", "merged_prs": "3", "error": "boom"},
            {"org": "acme", "repo": "widgets", "total_commits": "999", "loc": "", "merged_prs": "3", "error": ""}]
    rep, miss, _ = run(tmp_path, org_rows=rows)
    assert rep[0]["org_commits"] == "999" and rep[0]["status"] == "INCOMPLETE"
    assert "ORG_ROW_ERROR" in rep[0]["flags"]
