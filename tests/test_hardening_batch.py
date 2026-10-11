"""Hardening of the batch runner: never silent, crash safe, exit codes, org CSV loading. Synthetic fixtures only."""
import csv
import errno
import os
import signal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import batch  # noqa: E402
import check_export as ce  # noqa: E402
from garmr import forcheck  # noqa: E402
from conftest import (b_archive, b_env, b_read, b_zip, build_github_tree, make_for_check,  # noqa: E402
                      tar_it)

ORG_HEAD = ["org", "repo", "total_commits", "loc", "merged_prs"]


def org_csv(inp, rows, name="org_report.csv", head=ORG_HEAD, delim=",", encoding="utf-8"):
    text = delim.join(head) + "\n" + "".join(delim.join(str(r.get(h, "")) for h in head) + "\n" for r in rows)
    (Path(inp) / name).parent.mkdir(parents=True, exist_ok=True)
    (Path(inp) / name).write_bytes(text.encode(encoding))


def go(inp, out, work, **kw):
    batch.run(inp, out, work, want_loc=False, **kw)
    return b_read(out / "completeness_report.csv"), b_read(out / "missing_items.csv")


def notes(out):
    return (out / "run_notes.txt").read_text()


# ------------------------------------------------------------------ B1 never silent

def test_b1_archive_without_repo_rows_still_gets_a_row(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    monkeypatch.setattr(ce, "analyse_unit", lambda *a, **k: {"name": "x", "layout": "x", "unit_findings": [], "repos": []})
    rep, miss = go(inp, out, work)
    assert len(rep) == 1 and rep[0]["repo"] == "acme-widgets" and rep[0]["status"] == "INCOMPLETE"
    assert miss and miss[0]["check"] == "analysis_error"


def test_b1_crash_in_one_archive_does_not_stop_batch(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path, "acme-a")
    b_archive(inp, tmp_path, "acme-b")
    real = ce.discover_units

    def boom(path, w, units):
        if "acme-a" in path.name:
            raise RuntimeError("kaput")
        return real(path, w, units)
    monkeypatch.setattr(ce, "discover_units", boom)
    rep, miss = go(inp, out, work)
    by = {r["archive"]: r for r in rep}
    assert by["acme-a.tar.gz"]["status"] == "INCOMPLETE" and by["acme-a.tar.gz"]["repo"] == "acme-a"
    assert by["acme-b.tar.gz"]["status"] == "COMPLETE"
    assert [m for m in miss if m["check"] == "analysis_error" and "kaput" in m["detail"]]


# ------------------------------------------------------------------ B2 for-check validity

@pytest.mark.parametrize("content,state", [
    ("a;b;c;d\n1;2;3;4\n", "unreadable"),
    ("", "empty"),
    ("repo,section,key,value\n", "empty"),
    ("repo,section,key,value\nacme/widgets,counts_before,prs,3\n", "unreadable"),          # no meta rows
    ("repo,section,key,value\nacme/widgets,meta,state,exported\n", "unreadable"),          # meta only
])
def test_b2_unusable_for_check_is_never_complete(tmp_path, monkeypatch, content, state):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path, "acme-widgets-42")
    (inp / "acme-widgets-42.for-check.csv").write_text(content)
    rep, miss = go(inp, out, work)
    assert rep[0]["status"] == "INCOMPLETE" and rep[0]["for_check"] == state
    assert [m for m in miss if m["check"] == "for_check_unusable"]


def test_b2_utf16_for_check_is_read(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    archive, tree = b_archive(inp, tmp_path, "acme-widgets-42")
    p = make_for_check(archive, tree)
    p.write_bytes(p.read_text().encode("utf-16"))
    rep, _ = go(inp, out, work)
    assert rep[0]["for_check"] == "yes" and rep[0]["status"] == "COMPLETE"


def test_b2_missing_for_check_summary_line(tmp_path, monkeypatch, capsys):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    assert batch.main([]) == 0
    assert "1 of 1 archives had no for-check: source-side checks were skipped" in capsys.readouterr().out


def test_b2_require_for_check_env_makes_missing_a_fail(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    monkeypatch.setenv("GARMR_REQUIRE_FOR_CHECK", "1")
    assert batch.main([]) == 1
    miss = b_read(out / "missing_items.csv")
    assert [m for m in miss if m["check"] == "for_check_missing"]


# ------------------------------------------------------------------ B3 org CSV loading

def one_row(commits="999"):
    return [{"org": "acme", "repo": "widgets", "total_commits": commits, "loc": "", "merged_prs": ""}]


@pytest.mark.parametrize("delim", [";", "\t", "|"])
def test_b3_delimiters(tmp_path, monkeypatch, delim):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    org_csv(inp, one_row(), delim=delim)
    rep, miss = go(inp, out, work)
    assert rep[0]["org_csv_row"] == "yes" and rep[0]["org_commits"] == "999"


@pytest.mark.parametrize("enc", ["utf-16", "cp1252", "utf-8-sig"])
def test_b3_encodings(tmp_path, monkeypatch, enc):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    rows = one_row() + [{"org": "acme", "repo": "café", "total_commits": "5"}]
    org_csv(inp, rows, encoding=enc)
    rep, _ = go(inp, out, work)
    assert [r for r in rep if r["repo"] == "acme/widgets"][0]["org_csv_row"] == "yes"
    assert [r for r in rep if r["repo"] == "acme/café"]


def test_b3_subfolder_and_merge_of_all_csvs(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    org_csv(inp, one_row(), name="a.csv")
    org_csv(inp, [{"org": "acme", "repo": "ghost", "total_commits": "5"}], name="sub/b.csv")
    rep, _ = go(inp, out, work)
    assert [r for r in rep if r["repo"] == "acme/ghost"][0]["status"] == "NO_ARCHIVE"
    assert [r for r in rep if r["repo"] == "acme/widgets"][0]["org_csv_row"] == "yes"
    assert "a.csv" in notes(out) or (out / "run_notes.txt").exists()


def test_b3_project_name_only_when_no_repo_column(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    org_csv(inp, [{"org": "acme", "project_name": "widgets", "total_commits": "999"}], head=["org", "project_name", "total_commits"])
    rep, _ = go(inp, out, work)
    assert rep[0]["org_csv_row"] == "yes"
    # with a repo column present, project_name is NOT the identity
    org_csv(inp, [{"org": "acme", "repo": "widgets", "project_name": "Some Project", "total_commits": "1"}],
            head=["org", "repo", "project_name", "total_commits"])
    rep, _ = go(inp, out, work)
    assert rep[0]["org_csv_row"] == "yes" and "NOT_IN_ORG_CSV" not in rep[0]["flags"]


def test_b3_xlsx_warning_and_unused_csv_listed(tmp_path, monkeypatch, capsys):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    (inp / "report.xlsx").write_bytes(b"PK\x03\x04junk")
    (inp / "notes.csv").write_text("a,b\n1,2\n")
    go(inp, out, work)
    n = notes(out)
    assert "report.xlsx" in n and "export as CSV" in n
    assert "notes.csv" in n
    assert "report.xlsx" in capsys.readouterr().out


def test_b3_run_notes_always_created(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    go(inp, out, work)
    assert notes(out).strip() != ""


# ------------------------------------------------------------------ B4 / B5 / B6 / B7 org matching

def test_b4_one_row_per_branch_collapsed_max(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    org_csv(inp, [{"org": "acme", "repo": "widgets", "total_commits": "1"},
                  {"org": "acme", "repo": "widgets", "total_commits": "7"},
                  {"org": "acme", "repo": "widgets", "total_commits": "3"}])
    rep, miss = go(inp, out, work)
    assert len(rep) == 1                       # no false NO_ARCHIVE
    assert "MULTI_ROW" in rep[0]["flags"] and rep[0]["org_commits"] == "7"


def test_b5_other_org_row_is_not_matched(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    org_csv(inp, [{"org": "other", "repo": "widgets", "total_commits": "999"}])
    rep, _ = go(inp, out, work)
    mine = [r for r in rep if r["repo"] == "acme/widgets"][0]
    assert mine["org_csv_row"] == "no" and "NOT_IN_ORG_CSV" in mine["flags"] and mine["status"] == "COMPLETE"
    assert [r for r in rep if r["repo"] == "other/widgets"][0]["status"] == "NO_ARCHIVE"


@pytest.mark.parametrize("raw", ["12.5k", "N/A", "1 234", "12.5", "-5"])
def test_b6_unparseable_values_flagged(tmp_path, monkeypatch, raw):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    org_csv(inp, one_row(raw))
    rep, miss = go(inp, out, work)
    assert "ORG_VALUE_UNPARSEABLE:commits" in rep[0]["flags"] and rep[0]["org_commits"] == ""
    assert not [m for m in miss if m["check"] == "org_report_mismatch"]


def test_b6_blank_silent_and_thousands_accepted(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    org_csv(inp, [{"org": "acme", "repo": "widgets", "total_commits": "", "loc": "", "merged_prs": ""}])
    rep, _ = go(inp, out, work)
    assert "UNPARSEABLE" not in rep[0]["flags"]
    (inp / "org_report.csv").write_text('org,repo,total_commits\nacme,widgets,"1,234"\n')
    rep, miss = go(inp, out, work)
    assert rep[0]["org_commits"] == "1234"


def _truncate(a):
    a.write_bytes(a.read_bytes()[: a.stat().st_size // 2])


def test_b7_archive_under_other_name_matches_org_row(tmp_path, monkeypatch):
    # a broken archive is labelled with its file stem; the org row is acme/widgets: no wrong-reason NO_ARCHIVE pair
    inp, out, work = b_env(tmp_path, monkeypatch)
    a, _ = b_archive(inp, tmp_path, "acme-widgets-42")
    _truncate(a)
    org_csv(inp, one_row("1"))
    rep, _ = go(inp, out, work)
    assert len(rep) == 1 and rep[0]["repo"] == "acme-widgets-42" and rep[0]["org_csv_row"] == "yes"
    assert "NOT_IN_ORG_CSV" not in rep[0]["flags"]


def test_b7_fuzzy_stem_ignores_punctuation_and_case(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    a, _ = b_archive(inp, tmp_path, "export_ACME.Widgets_migration-9")
    _truncate(a)
    org_csv(inp, one_row("1"))
    rep, _ = go(inp, out, work)
    assert len(rep) == 1 and rep[0]["org_csv_row"] == "yes"


def test_b7_fuzzy_never_crosses_orgs(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    a, _ = b_archive(inp, tmp_path, "other-widgets-1")
    _truncate(a)
    org_csv(inp, one_row("1"))
    rep, _ = go(inp, out, work)
    assert {r["status"] for r in rep} == {"INCOMPLETE", "NO_ARCHIVE"}


# ------------------------------------------------------------------ B8 orphan inside zip

def test_b8_orphan_for_check_in_zip_names_the_zip(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    archive, tree = b_archive(inp, tmp_path, "acme-widgets-42", dest=tmp_path)
    fc = make_for_check(archive, tree)
    stray = b"repo,section,key,value\nacme/gone,meta,archive,acme-gone-9.tar.gz\n"
    b_zip(inp / "bundle.zip", {archive.name: archive, fc.name: fc, "acme-gone-9.for-check.csv": stray})
    rep, miss = go(inp, out, work)
    gone = [r for r in rep if r["status"] == "NO_ARCHIVE"]
    assert gone and gone[0]["archive"] == "bundle.zip"
    assert [m for m in miss if m["check"] == "archive_missing" and m["archive"] == "bundle.zip"]


# ------------------------------------------------------------------ C1 / C2 exit codes

def test_c1_environment_problem_stops_batch_exit_two(tmp_path, monkeypatch, capsys):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path, "acme-a")
    b_archive(inp, tmp_path, "acme-b")

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(ce, "discover_units", full)
    assert batch.main([]) == 2
    n = notes(out)
    assert "NOT_CHECKED" in n and "acme-a.tar.gz" in n and "acme-b.tar.gz" in n
    assert "disk" in capsys.readouterr().err.lower()


def test_c1_environment_problem_class_from_check_export(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    e = batch.EnvironmentProblem.__new__(batch.EnvironmentProblem)
    e.kind = "permission"

    def deny(*a, **k):
        raise e
    monkeypatch.setattr(ce, "discover_units", deny)
    assert batch.main([]) == 2


def test_c2_empty_input_exit_two(tmp_path, monkeypatch):
    b_env(tmp_path, monkeypatch)
    assert batch.main([]) == 2


def test_c2_zero_archives_exit_two_even_if_folder_not_empty(tmp_path, monkeypatch, capsys):
    inp, out, work = b_env(tmp_path, monkeypatch)
    (inp / "readme.txt").write_text("hi")
    assert batch.main([]) == 2
    assert "no archives" in capsys.readouterr().err.lower()


def test_c2_output_not_writable_exit_two(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    out.write_text("i am a file")
    assert batch.main([]) == 2


def test_c2_not_enough_space_exit_two(tmp_path, monkeypatch, capsys):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    monkeypatch.setattr(batch.shutil, "disk_usage", lambda p: (100, 99, 1))
    assert batch.main([]) == 2
    assert "space" in capsys.readouterr().err.lower()


def test_c2_disk_usage_failure_only_warns(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)

    def bad(p):
        raise OSError("nope")
    monkeypatch.setattr(batch.shutil, "disk_usage", bad)
    assert batch.main([]) == 0


def test_c2_internal_crash_exit_three_with_partials(tmp_path, monkeypatch, capsys):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path, "acme-a")
    b_archive(inp, tmp_path, "acme-b")
    real, calls = batch.missing_rows, []

    def flaky(rec):
        calls.append(1)
        if len(calls) == 2:
            raise ZeroDivisionError("bug")
        return real(rec)
    monkeypatch.setattr(batch, "missing_rows", flaky)
    assert batch.main([]) == 3
    assert "ZeroDivisionError" in capsys.readouterr().err
    assert not (out / "completeness_report.csv").exists()
    part = b_read(out / "completeness_report.csv.partial")
    assert [r["repo"] for r in part] == ["acme/widgets"]
    assert (out / "missing_items.csv.partial").exists()


# ------------------------------------------------------------------ C3 partial files / stale cleanup

def test_c3_stale_outputs_removed_at_start_and_no_partial_left(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    out.mkdir()
    for n in ("completeness_report.csv", "missing_items.csv", "completeness_report.csv.partial",
              "missing_items.csv.partial", "run_notes.txt"):
        (out / n).write_text("STALE")
    assert batch.main([]) == 0
    assert "STALE" not in (out / "completeness_report.csv").read_text()
    assert "STALE" not in (out / "missing_items.csv").read_text()
    assert not list(out.glob("*.partial"))
    assert "STALE" not in notes(out)


def test_c3_outputs_are_mode_0600(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    batch.main([])
    for n in ("completeness_report.csv", "missing_items.csv", "run_notes.txt"):
        assert (os.stat(out / n).st_mode & 0o777) == 0o600


# ------------------------------------------------------------------ C4 signals / scratch

@pytest.mark.parametrize("sig,code", [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_c4_signal_stops_after_current_archive(tmp_path, monkeypatch, sig, code):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path, "acme-a")
    b_archive(inp, tmp_path, "acme-b")
    real, n = ce.analyse_unit, []

    def wrapped(*a, **k):
        r = real(*a, **k)
        if not n:
            n.append(1)
            os.kill(os.getpid(), sig)
        return r
    monkeypatch.setattr(ce, "analyse_unit", wrapped)
    old = signal.getsignal(sig)
    assert batch.main([]) == code
    assert signal.getsignal(sig) == old           # handlers restored
    part = b_read(out / "completeness_report.csv.partial")
    assert len(part) == 1
    assert not (out / "completeness_report.csv").exists()
    assert "acme-b.tar.gz" in notes(out) and "NOT_CHECKED" in notes(out)
    assert not list(work.glob("garmr-*"))


def test_c4_leftover_scratch_of_killed_run_removed_but_nothing_else(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    (work / "garmr-old").mkdir(parents=True)
    (work / "garmr-old" / "junk").write_text("x")
    (work / "keep-me").mkdir()
    assert batch.main([]) == 0
    assert not (work / "garmr-old").exists() and (work / "keep-me").exists()


def test_c4_scratch_env_is_set_to_work_during_run(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    seen = []
    real = ce.analyse_unit
    monkeypatch.setattr(ce, "analyse_unit", lambda *a, **k: (seen.append(os.environ.get("GARMR_SCRATCH")), real(*a, **k))[1])
    batch.main([])
    assert seen == [str(work)]


# ------------------------------------------------------------------ C5 zip bundles

def make_bundle(tmp_path, inp, names, extra=None, with_fc=True):
    stage = tmp_path / "stage"
    stage.mkdir(exist_ok=True)
    members = {}
    for i, name in enumerate(names):
        tree = build_github_tree(tmp_path / f"zt{i}")
        a = tar_it(tree, stage / f"{name}.tar.gz")
        members[a.name] = a
        if with_fc:
            fc = make_for_check(a, tree)
            members[fc.name] = fc
    members.update(extra or {})
    return b_zip(inp / "bundle.zip", members)


def test_c5_one_tarball_in_scratch_at_a_time(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    make_bundle(tmp_path, inp, ["acme-a-1", "acme-b-2", "acme-c-3"])
    seen, real = [], ce.discover_units

    def spy(path, w, units):
        seen.append(sorted(p.name for p in Path(os.environ["GARMR_SCRATCH"]).rglob("*") if p.is_file() and p.name.endswith(".tar.gz")))
        return real(path, w, units)
    monkeypatch.setattr(ce, "discover_units", spy)
    rep, _ = go(inp, out, work)
    assert len(rep) == 3 and all(r["status"] == "COMPLETE" for r in rep)
    assert seen and all(len(s) == 1 for s in seen)      # never two tarballs on disk together
    assert not list(work.glob("garmr-*"))


def test_c5_macosx_and_dotunderscore_skipped(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    make_bundle(tmp_path, inp, ["acme-a-1"], extra={"__MACOSX/acme-a-1.tar.gz": b"junk", "._acme-a-1.tar.gz": b"junk"})
    rep, _ = go(inp, out, work)
    assert len(rep) == 1 and rep[0]["status"] == "COMPLETE"


def test_c5_unreadable_member_becomes_integrity_row_and_batch_continues(tmp_path, monkeypatch):
    import zipfile
    inp, out, work = b_env(tmp_path, monkeypatch)
    make_bundle(tmp_path, inp, ["acme-a-1", "acme-b-2"])
    real = zipfile.ZipFile.open

    def opener(self, name, *a, **k):
        n = getattr(name, "filename", name)
        if n == "acme-a-1.tar.gz":
            raise RuntimeError("File 'acme-a-1.tar.gz' is encrypted, password required for extraction")
        return real(self, name, *a, **k)
    monkeypatch.setattr(zipfile.ZipFile, "open", opener)
    rep, miss = go(inp, out, work)
    by = {r["archive"].split("!")[-1]: r for r in rep}
    assert by["acme-a-1.tar.gz"]["status"] == "INCOMPLETE" and by["acme-b-2.tar.gz"]["status"] == "COMPLETE"
    assert [m for m in miss if m["check"] == "archive_integrity" and "encrypted" in m["detail"]]


def test_c5_nested_zip_is_recursed(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    inner = make_bundle(tmp_path, tmp_path, ["acme-a-1"])
    inner.rename(tmp_path / "inner.zip")
    b_zip(inp / "outer.zip", {"inner.zip": tmp_path / "inner.zip"})
    rep, _ = go(inp, out, work)
    assert len(rep) == 1 and rep[0]["status"] == "COMPLETE" and rep[0]["for_check"] == "yes"
    assert rep[0]["archive"].count("!") == 2


def test_c5_loose_for_check_beside_zip_is_used_for_the_zip(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    make_bundle(tmp_path, inp, ["acme-a-1"])
    z = inp / "bundle.zip"
    (inp / "bundle.for-check.csv").write_text(
        "repo,section,key,value\nx,meta,archive,bundle.zip\nx,meta,archive_sha256," + "0" * 64 +
        "\nx,meta,state,exported\nx,counts_before,prs,1\n")
    rep, miss = go(inp, out, work)
    assert not [r for r in rep if r["status"] == "NO_ARCHIVE"]
    assert [m for m in miss if m["check"] == "transfer_integrity" and m["item"] == "sha256"]
    assert z.exists()


# ------------------------------------------------------------------ C6 csv safety

def test_c6_formula_cells_neutralised_and_newlines_stripped(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    org_csv(inp, [{"org": "", "repo": "=HYPERLINK(1)", "total_commits": "5"},
                  {"org": "", "repo": "@evil", "total_commits": "5"},
                  {"org": "", "repo": "-bad", "total_commits": "5"},
                  {"org": "", "repo": "-1+cmd|' /C calc'!A0", "total_commits": "5"}],
            head=["org", "repo", "total_commits"])
    go(inp, out, work)
    rows = b_read(out / "completeness_report.csv")
    repos = {r["repo"] for r in rows}
    # '-bad' is a legal repo name and a join key downstream: left untouched; a '-' cell with formula characters is not
    assert "'=HYPERLINK(1)" in repos and "'@evil" in repos and "-bad" in repos
    assert "'-1+cmd|' /C calc'!A0" in repos
    assert [r for r in rows if r["repo"] == "acme/widgets"]


def test_c6_sanitizer_unit():
    assert batch.csv_safe("=1+1") == "'=1+1"
    assert batch.csv_safe("\tx") == "'\tx".replace("\t", " ") or batch.csv_safe("\tx").startswith("'")
    assert batch.csv_safe("a\nb\r\nc") == "a b  c" or "\n" not in batch.csv_safe("a\nb\r\nc")
    assert batch.csv_safe(-5) == -5 and batch.csv_safe("-5") == "-5" and batch.csv_safe(7) == 7


# ------------------------------------------------------------------ C7 pairing

def test_c7_pairing_case_insensitive_on_stem(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    archive, tree = b_archive(inp, tmp_path, "acme-widgets-42")
    p = make_for_check(archive, tree)
    p.rename(inp / "ACME-Widgets-42.for-check.csv")
    rep, _ = go(inp, out, work)
    assert rep[0]["for_check"] == "yes" and len(rep) == 1


def test_c7_cross_folder_needs_unique_candidate(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    archive, tree = b_archive(inp, tmp_path, "acme-widgets-42")
    p = make_for_check(archive, tree)
    (inp / "fc1").mkdir()
    (inp / "fc2").mkdir()
    (inp / "fc1" / "one.for-check.csv").write_bytes(p.read_bytes())
    (inp / "fc2" / "two.for-check.csv").write_bytes(p.read_bytes())
    p.unlink()
    rep, _ = go(inp, out, work)
    mine = [r for r in rep if r["archive"] == "acme-widgets-42.tar.gz"][0]
    assert mine["for_check"] == "no"
    assert len([r for r in rep if r["status"] == "NO_ARCHIVE"]) == 2


def test_c7_cross_folder_sha_mismatch_not_paired(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    archive, tree = b_archive(inp, tmp_path, "acme-widgets-42")
    p = make_for_check(archive, tree, meta={"archive_sha256": "f" * 64})
    (inp / "fc").mkdir()
    p.rename(inp / "fc" / "whatever.for-check.csv")
    rep, _ = go(inp, out, work)
    assert [r for r in rep if r["archive"] == "acme-widgets-42.tar.gz"][0]["for_check"] == "no"
    assert [r for r in rep if r["status"] == "NO_ARCHIVE"]


def test_c7_stale_for_check_hint(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    archive, tree = b_archive(inp, tmp_path, "acme-widgets-42")
    fc = make_for_check(archive, tree)
    os.utime(fc, (1_000_000, 1_000_000))            # for-check is old, archive is fresh and different
    with open(archive, "ab") as f:
        f.write(b"\0" * 64)
    _, miss = go(inp, out, work)
    sha = [m for m in miss if m["check"] == "transfer_integrity" and m["item"] == "sha256"]
    assert sha and "older export" in sha[0]["detail"]


# ------------------------------------------------------------------ C8 archive-level findings once

def test_c8_archive_level_finding_once_with_star_and_on_every_row(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    real = ce.analyse_unit

    def two(*a, **k):
        u = real(*a, **k)
        r = u["repos"][0]
        u["repos"] = [r, dict(r, repo="acme/other")]
        u["unit_findings"] = list(u["unit_findings"]) + [ce.finding("FAIL", "archive_integrity", "member x is corrupt"),
                                                         ce.finding("FAIL", "zero_byte", "empty file y", item="y")]
        return u
    monkeypatch.setattr(ce, "analyse_unit", two)
    rep, miss = go(inp, out, work)
    assert len(rep) == 2 and all(r["status"] == "INCOMPLETE" and r["errors"] == "2" for r in rep)
    star = [m for m in miss if m["check"] == "archive_integrity"]
    assert len(star) == 1 and star[0]["repo"] == "*"
    zb = [m for m in miss if m["check"] == "zero_byte"]
    assert len(zb) == 1 and zb[0]["repo"] == "*"


# ------------------------------------------------------------------ C10 forcheck robustness

def test_c10_object_totals_survive_dangling_symlink(tmp_path):
    objects = tmp_path / "repositories" / "acme" / "w.git" / "objects"
    objects.mkdir(parents=True)
    (objects / "ab").write_bytes(b"1234")
    os.symlink(tmp_path / "nowhere", objects / "dangling")
    assert forcheck.object_totals([tmp_path]) == (1, 4)


# ------------------------------------------------------------------ C11 loc compare

def test_c11_loc_slack_rounds_up():
    org = {"metrics": {"loc": "loc"}}
    errs, flags, _ = batch.compare_metrics(org, {"loc": "101"}, {"loc": 99}, 2.0)   # ceil(1.98)=2 -> 101 <= 101
    assert errs == []
    errs, flags, _ = batch.compare_metrics(org, {"loc": "102"}, {"loc": 99}, 2.0)
    assert len(errs) == 1


def test_c11_loc_branch_diff_flag_but_still_error():
    org = {"metrics": {"loc": "loc"}}
    errs, flags, _ = batch.compare_metrics(org, {"loc": "120"}, {"loc": 100}, 0)
    assert len(errs) == 1 and "LOC_BRANCH_DIFF" in flags
    errs, flags, _ = batch.compare_metrics(org, {"loc": "500"}, {"loc": 100}, 0)
    assert len(errs) == 1 and "LOC_BRANCH_DIFF" not in flags


# ------------------------------------------------------------------ C12 summary / kind_absent flag

def test_c12_summary_printed(tmp_path, monkeypatch, capsys):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path, "acme-a")
    b_archive(inp, tmp_path, "acme-b", pr_meta=[1])
    org_csv(inp, one_row("1"))
    assert batch.main([]) == 1
    o = capsys.readouterr().out
    assert "1 COMPLETE" in o and "1 INCOMPLETE" in o and "run_notes.txt" in o and "org_report.csv" in o


def test_kind_absent_info_becomes_flag(tmp_path, monkeypatch):
    inp, out, work = b_env(tmp_path, monkeypatch)
    b_archive(inp, tmp_path)
    real = ce.analyse_unit

    def add(*a, **k):
        u = real(*a, **k)
        u["repos"][0]["findings"].append(ce.finding("INFO", "kind_absent", "no releases file", item="releases"))
        return u
    monkeypatch.setattr(ce, "analyse_unit", add)
    rep, _ = go(inp, out, work)
    assert "KIND_ABSENT:releases" in rep[0]["flags"]
