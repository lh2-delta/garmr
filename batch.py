#!/usr/bin/env python3
"""Garmr folder batch: check every migration-downloader archive in the input folder.

Folders are fixed (mounted by docker-compose), overridable by env only for tests:
  EXPORT_CHECK_INPUT   /data/input    repo .zip/.tar.gz files, their <archive>.for-check.csv, one organisation metrics .csv
  EXPORT_CHECK_OUTPUT  /data/output   completeness_report.csv, missing_items.csv
  EXPORT_CHECK_WORK    /data/work     scratch space; each archive is extracted, checked, then deleted

Outputs (all mode 0600; stale copies from a previous run are removed at start):
  completeness_report.csv  one row per repo of the run: COMPLETE / INCOMPLETE / NO_ARCHIVE + counts + flags
  missing_items.csv        only errors / things present in git history or the org report but absent from the archive.
                           Archive-level findings are written once per archive with repo `*`.
  run_notes.txt            non-fatal notices of the run (org CSVs skipped, unreadable members, interruptions...)
  *.csv.partial            written incrementally during the run; they remain only if the run died or was stopped

Exit codes: 0 all complete | 1 something incomplete / NO_ARCHIVE | 2 usage or environment (input missing/empty, no
archives, output/work unwritable, not enough space, scc missing, disk full/permission/io) | 3 internal crash |
130 SIGINT | 143 SIGTERM.

Org metrics CSV rule: org number > what the archive holds  -> error row in missing_items.csv (repo INCOMPLETE)
                      org number < what the archive holds  -> only flagged (ORG_LOWER:<metric>) in the report
"""
from __future__ import annotations

from __future__ import annotations

import codecs
import csv
import errno
import io
import math
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import traceback
import zipfile
from pathlib import Path, PurePosixPath

import check_export as ce
from garmr import forcheck

INPUT_DIR = "/data/input"
OUTPUT_DIR = "/data/output"
WORK_DIR = "/data/work"

SCRATCH_PREFIX = "garmr-"
# the extracted tree (1.2x the archive) coexists with a bare clone of a GitLab bundle and with the LOC checkout of HEAD,
# each about the archive's size, so the largest archive needs this much free work space
SPACE_FACTOR = 3.2
MAX_ZIP_DEPTH = 2
BRANCH_DIFF_PCT = 25

REPORT_COLS = ["repo", "provider", "archive", "status", "errors", "warnings", "warned_checks", "failed_checks", "for_check",
               "commits_head",
               "commits_all", "prs_git", "prs_meta", "labels", "issues", "branches", "tags", "files", "loc",
               "org_csv_row", "org_commits", "org_loc", "org_prs", "org_branches", "org_tags", "org_files", "flags"]
MISSING_COLS = ["repo", "provider", "archive", "check", "item", "expected", "found", "missing_count",
                "missing_items", "detail"]
ORG_METRICS = ("commits", "loc", "prs", "branches", "tags", "files")

ID_ALIASES = ["full_name", "repo_full_name", "repository", "repo", "repo_name", "name", "slug", "archive",
              "zip", "file", "filename"]
FALLBACK_ID_ALIASES = ["project_name"]      # only when no ID_ALIASES column exists
OWNER_ALIASES = ["org", "owner", "organization", "organisation", "company"]
METRIC_ALIASES = {
    "commits": ["commits", "commit_count", "total_commits", "num_commits", "n_commits"],
    "loc": ["loc", "lines_of_code", "code", "loc_total", "total_loc", "code_lines", "sloc"],
    "prs": ["prs", "pr_count", "pull_requests", "merged_prs", "prs_count", "num_prs"],
    "branches": ["branches", "branch_count", "total_branches"],
    "tags": ["tags", "tag_count", "total_tags"],
    "files": ["files", "file_count", "total_files"],
}

# findings that describe the whole archive: attached to every repo row, written once (repo `*`) in missing_items.csv
ARCHIVE_LEVEL = {"transfer_integrity", "export_state", "file_listing", "archive_integrity", "for_check_unusable",
                 "for_check_missing"}

try:
    EnvironmentProblem = ce.EnvironmentProblem
except AttributeError:          # check_export without the host-problem contract: keep working
    class EnvironmentProblem(Exception):
        kind = "io"


class UsageError(Exception):
    """Exit 2: the run cannot start or must stop because of the folders or the host, not because of the data."""


_STOP = {"sig": None}
_SCRATCH: set = set()


def env_kind(e):
    """'disk_full' | 'permission' | 'io' when the exception is a host problem, else None."""
    if isinstance(e, EnvironmentProblem):
        return getattr(e, "kind", None) or "io"
    if isinstance(e, OSError):
        if e.errno in (errno.ENOSPC, errno.EDQUOT):
            return "disk_full"
        if e.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            return "permission"
        if e.errno == errno.EIO:
            return "io"
    return None


ENV_WORDS = {"disk_full": "disk full", "permission": "permission denied", "io": "I/O error"}


# ---------------------------------------------------------------- small helpers

def norm_header(h):
    return re.sub(r"[^a-z0-9]+", "_", (h or "").lower()).strip("_")


def norm_id(v):
    v = (v or "").strip().lower()
    v = re.sub(r"^https?://[^/]+/", "", v)
    for s in (".tar.gz", ".tgz", ".tar", ".zip", ".git"):
        if v.endswith(s):
            v = v[: -len(s)]
    return v


def alnum(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def parse_org_value(v):
    """-> (int | None, 'blank' | 'ok' | 'bad'). '1,234' is fine; '12.5k', 'N/A', '1 234', '12.5', '-5' are not."""
    if isinstance(v, int) and not isinstance(v, bool):
        return (v, "ok") if v >= 0 else (None, "bad")
    s = (v or "").strip() if isinstance(v, str) or v is None else str(v).strip()
    if s == "":
        return None, "blank"
    if re.fullmatch(r"\d+|\d{1,3}(,\d{3})+", s):
        return int(s.replace(",", "")), "ok"
    if re.fullmatch(r"\d+\.0+", s):
        return int(float(s)), "ok"
    return None, "bad"


def to_int(v):
    return parse_org_value(v)[0]


_NUMERIC = re.compile(r"^[+-]?\d+(\.\d+)?$")


_ID_CELL = re.compile(r"^[-A-Za-z0-9._/:!]+$")
ID_COLS = ("repo", "archive")


def csv_safe(v, identifier=False):
    """Neutralise spreadsheet formulas and embedded newlines in text cells; numbers pass through.

    `identifier` cells (repo, archive) are join keys for downstream consumers, so a name that merely starts with '-'
    (a legal repo name) stays byte-identical; anything with characters a repo/file name never needs is still escaped."""
    if not isinstance(v, str):
        return v
    if v[:1] in ("=", "+", "-", "@", "\t", "\r") and not _NUMERIC.match(v):
        if not (identifier and v[:1] == "-" and _ID_CELL.match(v)):
            v = "'" + v
    return v.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _open_private(path: Path, mode="w"):
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "a" else os.O_TRUNC)
    return os.fdopen(os.open(path, flags, 0o600), mode, newline="", encoding="utf-8")


class Notes:
    """Non-fatal notices of the run: printed and appended to run_notes.txt as they happen."""

    def __init__(self, path: Path | None):
        self.path, self.items = path, []

    def add(self, msg):
        self.items.append(msg)
        print(msg, flush=True)
        if self.path:
            with _open_private(self.path, "a") as f:
                f.write(msg.replace("\n", " ") + "\n")

    def close(self):
        if self.path and not self.items:
            with _open_private(self.path, "a") as f:
                f.write("none\n")


class Sink:
    """completeness_report.csv.partial / missing_items.csv.partial, appended + fsynced per archive, finalised at the end."""

    def __init__(self, out: Path):
        self.out = out
        self.rp, self.mp = out / "completeness_report.csv.partial", out / "missing_items.csv.partial"
        self.rf, self.mf = _open_private(self.rp), _open_private(self.mp)
        self.rw = csv.DictWriter(self.rf, fieldnames=REPORT_COLS, extrasaction="ignore")
        self.mw = csv.DictWriter(self.mf, fieldnames=MISSING_COLS, extrasaction="ignore")
        self.rw.writeheader()
        self.mw.writeheader()
        self.sync()

    @staticmethod
    def _clean(row):
        return {k: csv_safe(v, k in ID_COLS) for k, v in row.items()}

    def sync(self):
        for f in (self.rf, self.mf):
            f.flush()
            os.fsync(f.fileno())

    def add(self, report_rows, missing_rows):
        self.rw.writerows(self._clean(r) for r in report_rows)
        self.mw.writerows(self._clean(r) for r in missing_rows)
        self.sync()

    def abandon(self):
        for f in (self.rf, self.mf):
            try:
                f.close()
            except OSError:
                pass

    def finalize(self, report_sorted):
        self.sync()
        self.abandon()
        tmp = self.out / "completeness_report.csv.new"
        with _open_private(tmp) as f:
            w = csv.DictWriter(f, fieldnames=REPORT_COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(self._clean(r) for r in report_sorted)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.out / "completeness_report.csv")
        os.replace(self.mp, self.out / "missing_items.csv")
        self.rp.unlink(missing_ok=True)


# ---------------------------------------------------------------- organisation CSV

def read_text(path: Path):
    """-> (text, encoding). utf-8-sig, UTF-16 BOM, then cp1252, then latin-1 (which cannot fail)."""
    data = path.read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16"), "utf-16"
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1"), "latin-1"


def sniff_delimiter(text):
    lines = text.splitlines()
    head = lines[0] if lines else ""
    present = [c for c in ",;\t|" if c in head]
    if len(present) == 1:
        return present[0]
    try:
        d = csv.Sniffer().sniff("\n".join(lines[:20]), delimiters=",;\t|").delimiter
        if d in head:
            return d
    except csv.Error:
        pass
    counts = {c: head.count(c) for c in ",;\t|"}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def read_org_file(p: Path):
    """-> dict(id, owner, metrics{metric: column}, rows) or a reason string why the file is not an org report."""
    try:
        text, enc = read_text(p)
        if not text.strip():
            return "file is empty"
        rdr = csv.DictReader(io.StringIO(text, newline=""), delimiter=sniff_delimiter(text))
        heads = {norm_header(h): h for h in (rdr.fieldnames or []) if h is not None}
        idc = next((heads[a] for a in ID_ALIASES if a in heads), None) or \
            next((heads[a] for a in FALLBACK_ID_ALIASES if a in heads), None)
        metrics = {m: next(heads[a] for a in al if a in heads) for m, al in METRIC_ALIASES.items()
                   if any(a in heads for a in al)}
        if not idc:
            return f"no repo identity column (header: {list(heads)[:8]})"
        if not metrics:
            return f"no metric column (commits/loc/prs/branches/tags/files) in header {list(heads)[:8]}"
        owner = next((heads[a] for a in OWNER_ALIASES if a in heads), None)
        return {"id": idc, "owner": owner, "metrics": metrics, "rows": list(rdr), "enc": enc}
    except (OSError, UnicodeError, csv.Error) as e:
        return f"unreadable: {type(e).__name__}: {e}"


def _under(p: Path, skip):
    return any(s in p.parents for s in skip)


def load_org(input_dir: Path, skip, notes):
    """Merge every qualifying CSV under the input folder. -> org dict or None.

    One entry per (owner, name): rows for the same repo (the profiler writes one row per branch) collapse to the max
    per metric. Every CSV considered but not used is reported with its reason."""
    skip = [Path(s).resolve() for s in skip]
    groups, files = {}, []
    for p in sorted(input_dir.rglob("*")):
        low = p.name.lower()
        if not p.is_file() or low.startswith("._") or "__MACOSX" in p.parts or _under(p.resolve(), skip):
            continue
        rel = str(p.relative_to(input_dir))
        if low.endswith((".xlsx", ".xls")):
            notes.add(f"WARNING: {rel} not read, export as CSV (Garmr reads organisation reports only as .csv)")
            continue
        if not low.endswith(".csv") or low.endswith(forcheck.SUFFIX):
            continue
        res = read_org_file(p)
        if isinstance(res, str):
            notes.add(f"org csv not used: {rel} ({res})")
            continue
        files.append(rel)
        if res["enc"] not in ("utf-8", "utf-8-sig"):
            notes.add(f"org csv {rel} decoded as {res['enc']}")
        blank = 0
        for row in res["rows"]:
            raw = (row.get(res["id"]) or "").strip()
            if not raw:
                blank += 1
                continue
            raw_owner = (row.get(res["owner"]) or "").strip() if res["owner"] else ""
            ident = norm_id(raw)
            if "/" in ident:
                owner, _, name = ident.rpartition("/")
            else:
                owner, name = norm_id(raw_owner), ident
            label = raw if "/" in raw or not raw_owner else f"{raw_owner}/{raw}"
            g = groups.setdefault((owner, name), {"owner": owner, "name": name, "label": label, "rows": 0,
                                                  "vals": {m: None for m in ORG_METRICS}, "bad": set(),
                                                  "files": []})
            g["rows"] += 1
            if rel not in g["files"]:
                g["files"].append(rel)
            for m, col in res["metrics"].items():
                v, state = parse_org_value(row.get(col))
                if state == "bad":
                    g["bad"].add(m)
                elif v is not None:
                    g["vals"][m] = v if g["vals"][m] is None else max(g["vals"][m], v)
        if blank:
            notes.add(f"org csv {rel}: {blank} row(s) without a repo name ignored")
    if not files:
        return None
    entries = list(groups.values())
    return {"files": files, "entries": entries, "metrics": {m: m for m in ORG_METRICS},
            "index": build_index(entries)}


def load_org_csv(input_dir: Path, skip=(), notes=None):
    """Compatibility wrapper: the merged organisation report of the input folder, None when there is none."""
    return load_org(Path(input_dir), skip, notes or Notes(None))


def entry_keys(e):
    n, o = e["name"], e["owner"]
    keys = {n}
    if o:
        keys |= {f"{o}/{n}", f"{o}-{n}", f"{o}__{n}"}
    elif "__" in n:
        keys.add(n.replace("__", "/"))
    return keys


def build_index(entries):
    idx: dict[str, list] = {}
    for e in entries:
        for k in entry_keys(e):
            idx.setdefault(k, []).append(e)
    return idx


def match_row(org, repo_label, archive):
    """-> list of matching org entries ([] = none). An entry of a different org is never a match."""
    owner, _, name = repo_label.rpartition("/")
    owner, name = norm_id(owner), norm_id(name)
    stem_raw = ce.archive_stem(Path(archive.split("!")[-1]).name)

    def allowed(e):
        return not (owner and e["owner"] and e["owner"] != owner)

    for cand in (f"{owner}/{name}" if owner else None, f"{owner}-{name}" if owner else None,
                 f"{owner}__{name}" if owner else None, norm_id(stem_raw), name):
        hits = [e for e in org["index"].get(cand, []) if allowed(e)] if cand else []
        if hits:
            return hits
    # the archive may carry the repo under another name (acme-widgets-42 for acme/widgets, or a failed archive whose
    # label is just its file name): owner+name contained in the alphanumeric file stem
    text = alnum(stem_raw)
    scored = [(len(t), e) for e in org["entries"] if e["owner"] and allowed(e)
              for t in [alnum(e["owner"] + e["name"])] if t and t in text]
    if scored:
        best = max(s for s, _ in scored)
        return [e for s, e in scored if s == best]
    return []


# ---------------------------------------------------------------- comparison

def compare_metrics(org, row, metrics, tolerance_pct):
    """-> (errors: [finding], flags: [str], org_values: dict)"""
    errors, flags, vals = [], [], {}
    for m, col in org["metrics"].items():
        o = parse_org_value(row.get(col))[0]
        vals[m] = o
        if o is None:
            continue
        if m == "commits":
            hi, lo = metrics.get("commits_all"), metrics.get("commits_head")
            if hi is None:
                flags.append("ORG_UNCOMPARED:commits")
                continue
            found = hi
            lower_than = lo if lo is not None else hi
        elif m == "prs":
            counted = [metrics[k] for k in ("prs_git", "prs_meta") if metrics.get(k) is not None]
            if not counted:      # plain-git / unreadable layout never counted PRs: 0 would be a false shortfall
                flags.append("ORG_UNCOMPARED:prs")
                continue
            found = max(counted)
            lower_than = found
        else:
            found = metrics.get(m)
            if found is None:
                flags.append("LOC_UNMEASURED" if m == "loc" else f"ORG_UNCOMPARED:{m}")
                continue
            lower_than = found
        slack = math.ceil(round(found * tolerance_pct / 100, 9)) if m == "loc" else 0
        if o > found + slack:
            errors.append(ce.finding("FAIL", "org_report_mismatch",
                                     f"org report says {o} {m}; archive holds {found}", expected=o, found=found,
                                     missing=None) | {"item": m, "missing_count": o - found})
            if m == "loc" and o <= found * (1 + BRANCH_DIFF_PCT / 100):
                flags.append("LOC_BRANCH_DIFF")      # the profiler may have measured another branch
        elif o < lower_than - slack:
            flags.append(f"ORG_LOWER:{m}")
    return errors, flags, vals


# ---------------------------------------------------------------- analysis of one archive

def list_archives(input_dir: Path, skip: list[Path], notes=None):
    skip = [Path(s).resolve() for s in skip]
    out = []
    for p in sorted(input_dir.rglob("*")):
        if not ce.is_archive(p) or _under(p.resolve(), skip):
            continue
        if p.name.startswith("._") or "__MACOSX" in p.parts:
            if notes:
                notes.add(f"ignored macOS metadata file {p.relative_to(input_dir)}")
            continue
        out.append(p)
    return out


def safe(rel):
    return re.sub(r"[^A-Za-z0-9._-]", "_", rel)


def new_scratch(work: Path) -> Path:
    d = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=work))
    _SCRATCH.add(d)
    return d


def drop_scratch(d: Path):
    shutil.rmtree(d, ignore_errors=True)
    _SCRATCH.discard(d)


def cleanup_scratch():
    for d in list(_SCRATCH):
        drop_scratch(d)


def scoped(x, label=None):
    return dict(x, scope="archive", **({"scope_label": label} if label else {}))


def fc_state(fc):
    return "no" if fc is None else fc.error_kind or "unreadable" if fc.error else "yes"


def unusable_finding(fc):
    return ce.finding("FAIL", "for_check_unusable", f"{fc.path.name}: {fc.error}", item=fc.path.name)


def error_rec(repo, rel, check, detail, fc=None, item=None):
    return {"repo": repo, "provider": "unknown", "archive": rel, "metrics": {}, "for_check": fc_state(fc),
            "findings": [scoped(ce.finding("FAIL", check, detail, item=item)) if check in ARCHIVE_LEVEL
                         else ce.finding("FAIL", check, detail, item=item)]}


def _analyse_single(arch: Path, rel: str, ctx, fc=None):
    tmp = new_scratch(ctx["work"])
    out = []
    try:
        raw: list = []
        ce.discover_units(arch, tmp, raw)
        usable = fc if fc and not fc.error else None
        for name, root, layout, pre in raw:
            u = ce.analyse_unit(name, root, layout, pre, tmp, ctx["live"], True, ctx["want_loc"], usable)
            repos = u.get("repos") or []
            multi = len(repos) > 1
            for r in repos:
                shared = [scoped(x) if multi or x["check"] in ARCHIVE_LEVEL else x for x in u["unit_findings"]]
                out.append({"repo": r["repo"], "provider": r["provider"], "archive": rel, "metrics": r["metrics"],
                            "findings": shared + list(r["findings"])})
        if not out:
            out.append(error_rec(ce.archive_stem(arch.name), rel, "analysis_error",
                                 "no repository found or recorded for this archive"))
        if usable:
            extra = [scoped(x) for x in forcheck.archive_findings(usable, arch, tmp / safe(arch.name))]
            for rec in out:
                rec["findings"] += extra
    except Exception as e:  # keep the batch going; the repo is reported, not skipped
        if env_kind(e):
            raise
        out.append(error_rec(ce.archive_stem(arch.name), rel, "analysis_error", f"{type(e).__name__}: {e}"))
    finally:
        drop_scratch(tmp)
    for rec in out:
        rec["for_check"] = fc_state(fc)
        if fc is not None and fc.error:
            rec["findings"].append(scoped(unusable_finding(fc)))
    return out


def _member_ok(info):
    parts = PurePosixPath(info.filename).parts
    return not info.is_dir() and parts and "__MACOSX" not in parts and not parts[-1].startswith("._")


def _copy_member(z, info, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with z.open(info) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def _analyse_zip(zpath: Path, rel: str, ctx, fc=None, depth=0):
    """A .zip of tarballs ("Download all" bundle): the small for-check members are read first, then ONE tarball at a
    time is extracted to scratch, checked and deleted. Anything else is checked as a single archive."""
    try:
        z = zipfile.ZipFile(zpath)
        infos = [i for i in z.infolist() if _member_ok(i)]
    except Exception as e:
        if env_kind(e):
            raise
        return _analyse_single(zpath, rel, ctx, fc)          # not a readable zip: reported as archive_integrity
    with z:
        def rel_of(i):
            return Path(*[p for p in PurePosixPath(i.filename).parts if p not in ("", ".", "..", "/")])
        tars = sorted((i for i in infos if i.filename.lower().endswith(forcheck.TAR_SUFFIXES)), key=lambda i: i.filename)
        zips = sorted((i for i in infos if i.filename.lower().endswith(".zip")), key=lambda i: i.filename)
        fc_infos = [i for i in infos if i.filename.lower().endswith(forcheck.SUFFIX)]
        if not tars and not zips:
            return _analyse_single(zpath, rel, ctx, fc)
        zdir, recs = new_scratch(ctx["work"]), []
        try:
            inner_fcs = []
            for i in fc_infos:
                dest = zdir / rel_of(i)
                try:
                    _copy_member(z, i, dest)
                    inner_fcs.append(forcheck.load(dest))
                except Exception as e:
                    if env_kind(e):
                        raise
                    inner_fcs.append(forcheck._unusable(dest, "unreadable", f"cannot be read from the zip: {type(e).__name__}: {e}"))
            used: set = set()
            for i in tars + zips:
                member = rel_of(i)
                mrel, dest = f"{rel}!{member.as_posix()}", zdir / member
                try:
                    _copy_member(z, i, dest)
                except Exception as e:
                    if env_kind(e):
                        raise
                    mfc = forcheck.match_archive(inner_fcs, dest, used)
                    if mfc:
                        used.add(id(mfc))
                    recs.append(error_rec(ce.archive_stem(member.name), mrel, "archive_integrity",
                                          f"{member} cannot be read from the zip ({type(e).__name__}: {e})", mfc))
                    continue
                try:
                    mfc = forcheck.match_archive(inner_fcs, dest, used)
                    if mfc:
                        used.add(id(mfc))
                    if i in zips:
                        if depth >= MAX_ZIP_DEPTH:
                            ctx["notes"].add(f"nested zip {mrel} not opened: deeper than {MAX_ZIP_DEPTH} levels")
                            recs.append(error_rec(ce.archive_stem(member.name), mrel, "archive_integrity",
                                                  f"nested zip {member} is deeper than {MAX_ZIP_DEPTH} levels: not checked", mfc))
                        else:
                            recs += _analyse_zip(dest, mrel, ctx, mfc, depth + 1)
                    else:
                        recs += _analyse_single(dest, mrel, ctx, mfc)
                finally:
                    dest.unlink(missing_ok=True)
            if fc is not None:                       # a for-check kept beside the zip describes the zip itself
                extra = [scoped(unusable_finding(fc), rel)] if fc.error else \
                    [scoped(x, rel) for x in forcheck.transfer_findings(fc, zpath)]
                for rec in recs:
                    rec["findings"] += extra
            for orphan in inner_fcs:
                if id(orphan) not in used:
                    recs.append({"repo": orphan.label, "provider": orphan.meta.get("provider") or "unknown",
                                 "archive": rel, "metrics": {}, "findings": [], "for_check": fc_state(orphan),
                                 "orphan_fc": orphan})
        finally:
            drop_scratch(zdir)
    return recs


def analyse_archive(arch: Path, rel: str, ctx, fc=None):
    """Never raises (except for host problems): a crash becomes an analysis_error row for that archive."""
    try:
        recs = _analyse_zip(arch, rel, ctx, fc) if arch.name.lower().endswith(".zip") \
            else _analyse_single(arch, rel, ctx, fc)
    except Exception as e:
        if env_kind(e):
            raise
        recs = [error_rec(ce.archive_stem(arch.name), rel, "analysis_error", f"{type(e).__name__}: {e}", fc)]
    return recs or [error_rec(ce.archive_stem(arch.name), rel, "analysis_error", "no result for this archive", fc)]


# ---------------------------------------------------------------- report rows

def for_check_flags(rec):
    """Flags from the for-check state of one archive (missing, unusable, entities left blank) and kinds absent."""
    state = rec["for_check"]
    absent = sorted({f"KIND_ABSENT:{x['item']}" for x in rec["findings"] if x["check"] == "kind_absent"})
    if state == "no":
        return ["NO_FOR_CHECK"] + absent
    if state == "unreadable":
        return ["FOR_CHECK_UNREADABLE"] + absent
    if state == "empty":
        return ["FOR_CHECK_EMPTY"] + absent
    return sorted({f"FOR_CHECK_UNKNOWN:{x['item']}" for x in rec["findings"]
                   if x["check"] == "for_check_unknown"}) + absent


def missing_rows(rec):
    rows = []
    for x in rec["findings"]:
        if x["level"] != "FAIL":
            continue
        miss = x.get("missing")
        count = x.get("missing_count", len(miss) if miss else "")
        if count == "" and isinstance(x.get("expected"), int) and isinstance(x.get("found"), int):
            count = x["expected"] - x["found"]
        star = x.get("scope") == "archive"
        rows.append({"repo": "*" if star else rec["repo"], "provider": rec["provider"],
                     "archive": x.get("scope_label") or rec["archive"], "check": x["check"],
                     "item": x.get("item", x["check"]), "expected": x.get("expected", ""),
                     "found": x.get("found", ""), "missing_count": count,
                     "missing_items": ";".join(str(i) for i in miss) if miss else "", "detail": x["detail"]})
    return rows


def c_no_archive_row(fc, archive=""):
    return {c: "" for c in REPORT_COLS} | {
        "repo": fc.label, "provider": fc.meta.get("provider") or "unknown", "archive": archive, "status": "NO_ARCHIVE",
        "errors": 1, "warnings": 0, "failed_checks": "archive_missing", "for_check": fc_state(fc),
        "org_csv_row": "n/a"}


def no_archive_missing_row(fc, archive=""):
    name = fc.meta.get("archive") or forcheck.for_check_name(fc.path.name[: -len(forcheck.SUFFIX)])
    return {"repo": fc.label, "provider": fc.meta.get("provider") or "unknown", "archive": archive,
            "check": "archive_missing", "item": "archive", "expected": 1, "found": 0, "missing_count": 1,
            "missing_items": name, "detail": f"{fc.path.name} found but no archive {name} in "
                                             f"{archive or 'input folder'}"}


def org_values(e):
    return {f"org_{k}": ("" if e["vals"].get(k) is None else e["vals"][k]) for k in ORG_METRICS}


def unmatched_org_rows(org, matched):
    report, missing = [], []
    for e in org["entries"]:
        if id(e) in matched:
            continue
        flags = (["MULTI_ROW"] if e["rows"] > 1 else []) + [f"ORG_VALUE_UNPARSEABLE:{m}" for m in sorted(e["bad"])]
        report.append({c: "" for c in REPORT_COLS} | {
            "repo": e["label"], "provider": "unknown", "archive": "", "status": "NO_ARCHIVE", "errors": 1,
            "warnings": 0, "failed_checks": "archive_missing", "org_csv_row": "yes", "flags": ";".join(flags),
            **org_values(e)})
        missing.append({"repo": e["label"], "provider": "unknown", "archive": "", "check": "archive_missing",
                        "item": "archive", "expected": 1, "found": 0, "missing_count": 1, "missing_items": e["label"],
                        "detail": f"listed in {', '.join(e['files'])} but no archive found in input folder"})
    return report, missing


def build_rows(rec, st):
    """-> (report_rows, missing_rows) for one analysed repo record, in input order."""
    if rec.get("orphan_fc"):          # a for-check inside a zip whose tarball is not in that zip
        fc = rec["orphan_fc"]
        return [c_no_archive_row(fc, rec["archive"])], [no_archive_missing_row(fc, rec["archive"])]
    flags, vals, row_state, org = for_check_flags(rec), {}, "n/a", st["org"]
    if rec["for_check"] == "no" and st["require_fc"]:
        rec["findings"].append(scoped(ce.finding(
            "FAIL", "for_check_missing", f"no {forcheck.for_check_name(Path(rec['archive'].split('!')[0]).name)} "
                                         f"for this archive and GARMR_REQUIRE_FOR_CHECK is set", item="for_check")))
    if org:
        hits = match_row(org, rec["repo"], rec["archive"])
        if not hits:
            row_state = "no"
            flags.append("NOT_IN_ORG_CSV")
        else:
            row_state = "yes"
            e = hits[0]
            st["matched"] |= {id(h) for h in hits}
            if len(hits) > 1:
                # several candidate rows: which one is this repo is a guess, so nothing is compared
                flags.append("AMBIGUOUS_ORG_ROW")
            else:
                if e["rows"] > 1:
                    flags.append("MULTI_ROW")
                flags += [f"ORG_VALUE_UNPARSEABLE:{m}" for m in sorted(e["bad"])]
                errs, fl, vals = compare_metrics(org, e["vals"], rec["metrics"], st["tol"])
                rec["findings"] += errs
                flags += fl
    fails = [x for x in rec["findings"] if x["level"] == "FAIL"]
    warns = [x for x in rec["findings"] if x["level"] == "WARN"]
    m = rec["metrics"]
    row = {
        "repo": rec["repo"], "provider": rec["provider"], "archive": rec["archive"],
        "status": "INCOMPLETE" if fails else "COMPLETE", "errors": len(fails), "warnings": len(warns),
        "warned_checks": ";".join(sorted({x["check"] for x in warns})),
        "failed_checks": ";".join(sorted({x["check"] for x in fails})), "for_check": rec["for_check"],
        "commits_head": m.get("commits_head", ""), "commits_all": m.get("commits_all", ""),
        "prs_git": m.get("prs_git", ""), "prs_meta": m.get("prs_meta", ""), "labels": m.get("labels", ""),
        "issues": m.get("issues", ""), "branches": m.get("branches", ""), "tags": m.get("tags", ""),
        "files": m.get("files", ""), "loc": "" if m.get("loc") is None else m["loc"],
        "org_csv_row": row_state, **{f"org_{k}": ("" if vals.get(k) is None else vals[k]) for k in ORG_METRICS},
        "flags": ";".join(flags)}
    out = []
    for r in missing_rows(rec):
        if r["repo"] == "*":
            key = (r["archive"], r["check"], r["item"], r["detail"])
            if key in st["seen_star"]:
                continue
            st["seen_star"].add(key)
        out.append(r)
    return [row], out


# ---------------------------------------------------------------- run

def _probe_writable(d: Path, what):
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / f".garmr-probe-{os.getpid()}"
        os.close(os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        probe.unlink()
    except OSError as e:
        raise UsageError(f"{what} folder is not writable: {d} ({e})")


def _prepare(input_dir, output_dir, work_dir):
    _probe_writable(output_dir, "output")
    _probe_writable(work_dir, "work")
    for n in ("completeness_report.csv", "missing_items.csv", "completeness_report.csv.partial",
              "missing_items.csv.partial", "completeness_report.csv.new", "run_notes.txt"):
        (output_dir / n).unlink(missing_ok=True)
    for d in work_dir.glob(SCRATCH_PREFIX + "*"):       # leftovers of a killed run; nothing else is ever touched
        if d.is_dir() and not d.is_symlink():
            shutil.rmtree(d, ignore_errors=True)


def _check_space(archives, work_dir, notes):
    if not archives:
        return
    largest = max(a.stat().st_size for a in archives)
    try:
        free = shutil.disk_usage(work_dir)[2]
    except OSError as e:
        notes.add(f"WARNING: free space in {work_dir} could not be determined ({e}); continuing")
        return
    need = int(largest * SPACE_FACTOR)
    if free < need:
        raise UsageError(f"not enough free space in work folder {work_dir}: {free} bytes free, need about {need} "
                         f"(largest archive {largest} bytes x {SPACE_FACTOR})")


def _run(input_dir, output_dir, work_dir, live=None, want_loc=True, tolerance_pct=0.0):
    """-> result dict. Raises UsageError before anything is analysed; every later failure is reported in the result."""
    input_dir, output_dir, work_dir = Path(input_dir), Path(output_dir), Path(work_dir)
    _prepare(input_dir, output_dir, work_dir)
    prev_scratch = os.environ.get("GARMR_SCRATCH")
    os.environ["GARMR_SCRATCH"] = str(work_dir)
    notes = Notes(output_dir / "run_notes.txt")
    res = {"report": [], "missing": [], "notes": notes, "interrupted": None, "env": None, "crash": False,
           "archives": 0, "archive_units": 0, "no_fc": 0, "org_files": [], "finalized": False}
    sink = None
    try:
        archives = list_archives(input_dir, [output_dir, work_dir], notes)
        res["archives"] = len(archives)
        _check_space(archives, work_dir, notes)
        org = load_org(input_dir, [output_dir, work_dir], notes)
        res["org_files"] = org["files"] if org else []
        skip = [output_dir.resolve(), work_dir.resolve()]
        fcs = [f for f in forcheck.load_folder(input_dir) if not _under(f.path.resolve(), skip)]
        sink = Sink(output_dir)
        st = {"org": org, "tol": tolerance_pct, "matched": set(), "seen_star": set(),
              "require_fc": os.environ.get("GARMR_REQUIRE_FOR_CHECK", "").strip().lower() in ("1", "true", "yes")}
        ctx = {"work": work_dir, "live": live, "want_loc": want_loc, "notes": notes}
        used: set = set()
        seen_archives, no_fc = set(), set()
        done = 0
        for n, arch in enumerate(archives, 1):
            if _STOP["sig"]:
                break
            rel = str(arch.relative_to(input_dir))
            print(f"[{n}/{len(archives)}] {rel}", flush=True)
            fc = forcheck.match_archive(fcs, arch, used)
            if fc:
                used.add(id(fc))
            try:
                recs = analyse_archive(arch, rel, ctx, fc)
            except ce.Interrupted:      # a child process was killed mid-archive: it is NOT_CHECKED, not half-reported
                _STOP["sig"] = _STOP["sig"] or signal.SIGTERM
                break
            except Exception as e:
                kind = env_kind(e)
                if not kind:
                    raise
                res["env"] = (kind, f"{ENV_WORDS[kind]} while checking {rel}: {e}")
                break
            rows, miss = [], []
            for rec in recs:
                r, m = build_rows(rec, st)
                rows += r
                miss += m
                if not rec.get("orphan_fc"):
                    seen_archives.add(rec["archive"])
                    if rec["for_check"] == "no":
                        no_fc.add(rec["archive"])
            sink.add(rows, miss)
            res["report"] += rows
            res["missing"] += miss
            done = n
        res["archive_units"], res["no_fc"] = len(seen_archives), len(no_fc)
        if res["env"] or _STOP["sig"]:
            left = [str(a.relative_to(input_dir)) for a in archives[done:]]
            if res["env"]:
                notes.add("ENVIRONMENT PROBLEM, run stopped: " + res["env"][1])
            else:
                notes.add(f"INTERRUPTED by {signal.Signals(_STOP['sig']).name}: the archive in progress was abandoned "
                          "(running git/scc calls are killed), no later archive was started")
            for rel in left:
                notes.add(f"NOT_CHECKED: {rel}")
            res["interrupted"] = _STOP["sig"]
            sink.abandon()
            return res
        extra_r, extra_m = [], []
        for fc in fcs:
            if id(fc) not in used:
                extra_r.append(c_no_archive_row(fc))
                extra_m.append(no_archive_missing_row(fc))
        if org:
            r, m = unmatched_org_rows(org, st["matched"])
            extra_r += r
            extra_m += m
        sink.add(extra_r, extra_m)
        res["report"] += extra_r
        res["missing"] += extra_m
        res["report"].sort(key=lambda r: (r["status"] == "COMPLETE", r["repo"]))
        sink.finalize(res["report"])
        res["finalized"] = True
    except UsageError:
        raise
    except Exception:
        res["crash"] = True
        traceback.print_exc()
        notes.add("INTERNAL CRASH: partial results are in the *.partial files, not a final report")
        if sink:
            sink.abandon()
    finally:
        cleanup_scratch()
        notes.close()
        if prev_scratch is None:
            os.environ.pop("GARMR_SCRATCH", None)
        else:
            os.environ["GARMR_SCRATCH"] = prev_scratch
    return res


def run(input_dir, output_dir, work_dir, live=None, want_loc=True, tolerance_pct=0.0):
    res = _run(input_dir, output_dir, work_dir, live=live, want_loc=want_loc, tolerance_pct=tolerance_pct)
    return res["report"], res["missing"]


def _on_signal(signum, frame):
    _STOP["sig"] = signum
    ce.request_stop()       # wakes the running git/tar/scc call so a 30-minute fsck cannot outlast `docker stop`


def _summary(res, out):
    rep = res["report"]
    c = {s: sum(1 for r in rep if r["status"] == s) for s in ("COMPLETE", "INCOMPLETE", "NO_ARCHIVE")}
    print(f"\nSUMMARY: {c['COMPLETE']} COMPLETE, {c['INCOMPLETE']} INCOMPLETE, {c['NO_ARCHIVE']} NO_ARCHIVE "
          f"({len(rep)} rows)")
    if res["no_fc"]:
        print(f"{res['no_fc']} of {res['archive_units']} archives had no for-check: source-side checks were skipped")
    print("org csv used: " + (", ".join(res["org_files"]) if res["org_files"] else "none found"))
    print(f"{len(res['notes'].items)} notice(s), see run_notes.txt")
    print(f"wrote {out}/completeness_report.csv and {out}/missing_items.csv ({len(res['missing'])} error rows)")


def main(argv=None):
    old_umask = os.umask(0o077)
    handlers = {}
    try:
        if threading.current_thread() is threading.main_thread():
            for s in (signal.SIGINT, signal.SIGTERM):
                handlers[s] = signal.signal(s, _on_signal)
        _STOP["sig"] = None
        ce.clear_stop()
        return _main()
    except UsageError as e:
        print(str(e), file=sys.stderr)
        return 2
    except Exception:
        traceback.print_exc()
        return 3
    finally:
        for s, h in handlers.items():
            signal.signal(s, h)
        _STOP["sig"] = None
        ce.clear_stop()
        cleanup_scratch()
        os.umask(old_umask)


def _main():
    inp = Path(os.environ.get("EXPORT_CHECK_INPUT", INPUT_DIR))
    out = Path(os.environ.get("EXPORT_CHECK_OUTPUT", OUTPUT_DIR))
    work = Path(os.environ.get("EXPORT_CHECK_WORK", WORK_DIR))
    if not inp.is_dir():
        raise UsageError(f"input folder not found: {inp}")
    if not any(inp.iterdir()):
        raise UsageError(f"input folder is empty: {inp}")
    raw_tol = os.environ.get("LOC_TOLERANCE_PCT", "0").strip()
    try:
        tol = float(raw_tol)
        if not math.isfinite(tol) or tol < 0:
            raise ValueError
    except ValueError:
        raise UsageError(f"LOC_TOLERANCE_PCT must be a non-negative number, got {raw_tol!r}")
    if not shutil.which("scc"):
        raise UsageError("scc is not installed, so lines of code cannot be measured and the org-CSV LOC floor would be "
                         "silently skipped. Refusing to run: install scc (the Docker image includes it).")
    token = os.environ.get("GITHUB_TOKEN")
    live = ce.github_live(token) if token else None
    res = _run(inp, out, work, live=live, tolerance_pct=tol)
    if res["crash"]:
        return 3
    if res["env"]:
        print(f"stopped: {res['env'][1]}. Outputs so far are in {out}/*.partial; see run_notes.txt", file=sys.stderr)
        return 2
    if res["interrupted"]:
        print(f"interrupted; partial outputs are in {out}/*.partial, see run_notes.txt", file=sys.stderr)
        return 128 + res["interrupted"]
    _summary(res, out)
    if res["archives"] == 0:
        print(f"no archives (.zip .tar.gz .tgz .tar) found anywhere under {inp}: nothing was analysed", file=sys.stderr)
        return 2
    return 1 if any(r["status"] != "COMPLETE" for r in res["report"]) else 0


if __name__ == "__main__":
    sys.exit(main())
