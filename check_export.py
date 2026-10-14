#!/usr/bin/env python3
"""Garmr: check that migration-downloader exports are complete (library + ad-hoc CLI; the batch tool is batch.py).

What "complete" means:
  * the archive reads end to end (no truncation / CRC errors)
  * no file is 0 bytes (small allowlist -> warning)
  * the bare git repo inside is intact (git fsck --full: every object is present and readable)
  * every PR / MR that git history proves existed has a metadata record
  * every label / milestone / parent issue that exported records reference was itself exported
  * optionally (--live + token) item counts match the GitHub API
  * with a <archive>.for-check.csv: counts, refs, files and transfer integrity match what the source reported
Stdlib + the git binary only (scc optional, for LOC).

Environment:
  GARMR_SCRATCH        directory for every temp file (default: system temp dir)
  GARMR_GIT_TIMEOUT    seconds allowed for each git/scc/tar call (default 1800); expiry is a FAIL `timeout` finding
  GARMR_FSCK_TIMEOUT   seconds allowed for git fsck (default 3600)
  GARMR_MAX_EXTRACT_BYTES / GARMR_MAX_EXTRACT_MEMBERS / GARMR_MAX_EXTRACT_RATIO / GARMR_EXTRACT_TIMEOUT
                       per-archive extraction caps (default 200 GiB / 5,000,000 members / 200x / 3600 s); a bigger
                       archive is a FAIL archive_integrity for that archive only, not a run-wide abort
  GARMR_MAX_JSON_BYTES one metadata JSON file larger than this (default 4 GiB) is a FAIL metadata_json, not loaded
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import threading
import urllib.parse
import urllib.request
import zipfile
import zlib
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")
ZERO_BYTE_ALLOW = {".gitkeep", ".keep", ".nojekyll", "__init__.py", "packed-refs", "FETCH_HEAD"}
GITHUB_JSON = re.compile(r"^(?P<kind>[a-z_]+?)_(?P<n>\d+)\.json$")
LFS_SIG = "version https://git-lfs.github.com/spec/v1"
GIT_SUFFIX = re.compile(r"\.git$")
MAX_MESSAGE_CHARS = 65536       # one commit message is never held beyond this
MAX_LFS_OIDS = 200000


def finding(level, check, detail, expected=None, found=None, missing=None, item=None):
    out = {"level": level, "check": check, "detail": detail}
    if item is not None:
        out["item"] = item
    if expected is not None:
        out["expected"] = expected
    if found is not None:
        out["found"] = found
    if missing is not None:
        out["missing"] = list(missing)
        total = getattr(missing, "total", None)
        if total is not None and total > len(out["missing"]):
            out["missing_count"] = total   # the list shows examples only; the count stays real
    return out


def is_archive(p: Path) -> bool:
    return p.is_file() and p.name.lower().endswith(ARCHIVE_SUFFIXES)


def archive_stem(name: str) -> str:
    for s in ARCHIVE_SUFFIXES:
        if name.lower().endswith(s):
            return name[: -len(s)]
    return name


def str_of(v) -> str:
    return v if isinstance(v, str) else ""


# ---------------------------------------------------------------- environment, timeouts, scratch

class EnvironmentProblem(Exception):
    """The host, not the export, is at fault (disk full, permissions, I/O). kind: disk_full | permission | io."""

    def __init__(self, kind, detail=""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail = kind, detail


class Interrupted(BaseException):
    """A stop was requested (SIGTERM/SIGINT) while a child process ran; the child was killed. BaseException so that
    the per-repo `except Exception` safety nets cannot turn a stop into an analysis_error row."""


class GitError(Exception):
    """A git command that must succeed exited non-zero: its output cannot be trusted as 'no facts'."""

    def __init__(self, what, code, stderr=""):
        super().__init__(f"{what} failed (exit {code})" + (f": {stderr.strip()[:200]}" if stderr and stderr.strip() else ""))
        self.what, self.code = what, code


_STOP = threading.Event()


def request_stop():
    _STOP.set()


def clear_stop():
    _STOP.clear()


class CmdTimeout(Exception):
    def __init__(self, what, seconds):
        super().__init__(f"{what} timed out after {seconds:g}s")
        self.what, self.seconds = what, seconds


_DISK = {getattr(errno, n) for n in ("ENOSPC", "EDQUOT", "EFBIG") if hasattr(errno, n)}
_PERM = {errno.EACCES, errno.EPERM, errno.EROFS}
_IO = {getattr(errno, n) for n in ("EIO", "ENOMEM", "EMFILE", "ENFILE", "ESTALE", "ENXIO", "ENODEV", "ENOTCONN",
                                   "EBUSY", "ENOBUFS", "EREMOTEIO") if hasattr(errno, n)}


def env_kind(e: OSError):
    """-> 'disk_full' | 'permission' | 'io' when errno says the host is at fault, else None (archive problem)."""
    code = getattr(e, "errno", None)
    if code in _DISK:
        return "disk_full"
    if code in _PERM:
        return "permission"
    if code in _IO:
        return "io"
    return None


def as_env(e: OSError):
    kind = env_kind(e)
    return EnvironmentProblem(kind, f"{type(e).__name__}: {e}") if kind else None


def _env_float(name, default):
    try:
        v = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return v if v > 0 else default


def git_timeout():
    return _env_float("GARMR_GIT_TIMEOUT", 1800.0)


def max_json_bytes():
    return _env_float("GARMR_MAX_JSON_BYTES", float(4 << 30))


def fsck_timeout():
    return _env_float("GARMR_FSCK_TIMEOUT", 3600.0)


def scratch_dir():
    d = os.environ.get("GARMR_SCRATCH")
    if not d:
        return None
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        raise (as_env(e) or EnvironmentProblem("io", f"{type(e).__name__}: {e}"))
    return d


def make_tmp(prefix, base=None):
    try:
        return tempfile.mkdtemp(prefix=prefix, dir=str(base) if base else scratch_dir())
    except OSError as e:
        raise (as_env(e) or EnvironmentProblem("io", f"{type(e).__name__}: {e}"))


def timeout_finding(label, e: CmdTimeout):
    return finding("FAIL", "timeout", f"{label}: {e.what} timed out after {e.seconds:g}s (not verified)", item=e.what)


def run_cmd(cmd, what, timeout=None, **kw):
    """subprocess.run(capture_output) that also notices a stop request within ~0.5 s and kills the child."""
    t = timeout or git_timeout()
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    check, inp = kw.pop("check", False), kw.pop("input", None)
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE if inp is not None else None, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, **kw)
    deadline = time.monotonic() + t
    while True:
        if _STOP.is_set():
            p.kill()
            p.communicate()
            raise Interrupted(what)
        left = deadline - time.monotonic()
        if left <= 0:
            p.kill()
            p.communicate()
            raise CmdTimeout(what, t)
        try:
            out, err = p.communicate(input=inp, timeout=min(0.5, left))
            break
        except subprocess.TimeoutExpired:
            inp = None      # already handed to the child by the first communicate()
    r = subprocess.CompletedProcess(cmd, p.returncode, out, err)
    if check and r.returncode:
        raise subprocess.CalledProcessError(r.returncode, cmd, out, err)
    return r


# ---------------------------------------------------------------- extraction

def _env_int(name, default):
    return int(_env_float(name, default))


MAX_NEST_DEPTH = 3                 # archives inside archives inside archives; deeper is reported, not opened
RATIO_MIN_BYTES = 1 << 30          # the compression-ratio cap only applies once the output is this big


class ExtractLimit(Exception):
    """The archive would expand past a cap or the free disk: a verdict on that archive, not a host fault."""


class _Budget:
    """Running totals for one extraction. Tar sizes come from member headers (checked before each write); zip sizes
    are pre-summed from the central directory, which the zip reader also enforces while reading."""

    def __init__(self, archive: Path, dest: Path):
        self.max_bytes = _env_int("GARMR_MAX_EXTRACT_BYTES", 200 << 30)
        self.max_members = _env_int("GARMR_MAX_EXTRACT_MEMBERS", 5_000_000)
        self.max_ratio = _env_float("GARMR_MAX_EXTRACT_RATIO", 200.0)
        self.deadline = time.monotonic() + _env_float("GARMR_EXTRACT_TIMEOUT", 3600.0)
        try:
            self.src = max(archive.stat().st_size, 1)
            self.free = shutil.disk_usage(dest).free
        except OSError:
            self.src, self.free = 1, None
        self.total = self.members = 0

    def add(self, size, members=1):
        if _STOP.is_set():
            raise Interrupted("extraction")
        if time.monotonic() > self.deadline:
            raise ExtractLimit("extraction exceeded its time limit (GARMR_EXTRACT_TIMEOUT)")
        self.members += members
        self.total += max(size or 0, 0)
        if self.members > self.max_members:
            raise ExtractLimit(f"archive has more than {self.max_members} members (GARMR_MAX_EXTRACT_MEMBERS)")
        if self.total > self.max_bytes:
            raise ExtractLimit(f"archive expands past {self.max_bytes} bytes (GARMR_MAX_EXTRACT_BYTES)")
        if self.total > RATIO_MIN_BYTES and self.total > self.max_ratio * self.src:
            raise ExtractLimit(f"archive expands {self.total // self.src}x its size (GARMR_MAX_EXTRACT_RATIO), "
                               "likely a decompression bomb")
        if self.free is not None and self.total > self.free:
            raise ExtractLimit(f"archive expands to {self.total}+ bytes but only {self.free} bytes are free "
                               "in the work folder")


def _passthrough(member, _path):
    return member


def _safe_extract(t: tarfile.TarFile, dest: Path, skipped, budget=None):
    """Extract every member that passes the 'data' filter; skip (and record) the ones that do not."""
    for m in t:
        try:
            fm = tarfile.data_filter(m, str(dest))
        except tarfile.FilterError:
            if skipped is not None:
                skipped.append(m.name)
            continue
        if fm is None:
            continue
        if budget:
            budget.add(fm.size if fm.isreg() else 0)
        t.extract(fm, str(dest), filter=_passthrough)


def _unsafe_zip_name(name):
    parts = PurePosixPath(name.replace("\\", "/")).parts
    return name.startswith(("/", "\\")) or ".." in parts or bool(re.match(r"^[A-Za-z]:", name))


def extract(archive: Path, dest: Path, skipped=None):
    """Extract fully; returns None on success or an error string describing a genuinely bad archive.

    Unsafe members (absolute/escaping links, ../ or absolute paths, device files) are skipped and their names appended
    to `skipped`. An archive that would blow a size/member/ratio/time cap or the free disk is an error string for
    that archive. Host problems (disk full, permissions, I/O) raise EnvironmentProblem, never an archive error.
    """
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise (as_env(e) or EnvironmentProblem("io", f"{type(e).__name__}: {e}"))
    is_zip = archive.name.lower().endswith(".zip")
    budget = _Budget(archive, dest)
    try:
        if is_zip:
            with zipfile.ZipFile(archive) as z:
                infos = z.infolist()
                budget.add(sum(i.file_size for i in infos), len(infos))
                bad = z.testzip()
                if bad:
                    return f"CRC error in member {bad}"
                # extractall() would silently rewrite ../ and absolute names into the tree; skip and report instead
                safe = [i for i in infos if not _unsafe_zip_name(i.filename)]
                if skipped is not None:
                    skipped.extend(i.filename for i in infos if _unsafe_zip_name(i.filename))
                z.extractall(dest, members=safe)
        else:
            with tarfile.open(archive) as t:
                _safe_extract(t, dest, skipped, budget)
    except (EnvironmentProblem, Interrupted):
        raise
    except ExtractLimit as e:
        return f"not extracted: {e}"
    except RuntimeError as e:
        if "encrypted" in str(e).lower() or "password" in str(e).lower():
            return f"encrypted zip, cannot be read without a password: {e}"
        return f"RuntimeError: {e}"
    except NotImplementedError as e:
        return f"unsupported compression method in archive: {e}"
    except zipfile.LargeZipFile as e:
        return f"zip too large for this reader: {e}"
    except OSError as e:
        env = as_env(e)
        if env:
            raise env from e
        return f"{type(e).__name__}: {e}"
    except (EOFError, tarfile.TarError, zipfile.BadZipFile, zlib.error) as e:
        return f"{type(e).__name__}: {e}"
    except Exception as e:  # malformed archives surface as ValueError/struct.error/...: still an archive problem
        return f"{type(e).__name__}: {e}"
    return None


def detect_layout(root: Path):
    if (root / "schema.json").exists() or any(root.glob("repositories_*.json")):
        return "github-migration"
    if (root / "project.json").exists() or (root / "tree" / "project.json").exists():
        return "gitlab-export"
    if (root / "HEAD").exists() and (root / "objects").is_dir():
        return "plain-git"
    if (root / ".git").exists():
        return "plain-git"
    return None


def find_unit_root(root: Path):
    """Archives often wrap everything in one top-level folder."""
    for _ in range(3):
        layout = detect_layout(root)
        if layout:
            return root, layout
        kids = [k for k in root.iterdir() if k.name != "__MACOSX"]
        if len(kids) == 1 and kids[0].is_dir():
            root = kids[0]
            continue
        break
    return root, None


def _unique_dir(work: Path, name):
    """work/<safe name>, or a numbered sibling when an earlier archive of the same name already used it, so two
    a.tar.gz from different folders never merge into one tree."""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    out, n = work / base, 0
    while out.exists():
        n += 1
        out = work / f"{base}.{n}"
    return out


def discover_units(path: Path, work: Path, units: list):
    """Append (name, root, layout, pre_findings) tuples."""
    _discover(path, work, units, 0)


def _discover(path: Path, work: Path, units: list, depth):
    if is_archive(path):
        out = _unique_dir(work, path.name)
        skipped: list = []
        err = extract(path, out, skipped)
        if err:
            units.append((path.name, None, "archive", [finding("FAIL", "archive_integrity", err)]))
            return
        n0 = len(units)
        _discover(out, work, units, depth + 1)
        if skipped:
            warn = finding("WARN", "unsafe_member", f"{len(skipped)} unsafe archive member(s) skipped, not extracted "
                           f"(absolute/escaping link, ../ path or special file): {skipped[:5]}",
                           found=len(skipped), missing=capped(skipped), item="members")
            for u in units[n0:]:
                u[3].append(warn)
        return
    if not path.is_dir():
        units.append((path.name, None, "unknown", [finding("FAIL", "input", "not a file or directory")]))
        return
    root, layout = find_unit_root(path)
    if layout:
        units.append((path.name, root, layout, []))
        return
    inner = sorted(p for p in path.rglob("*") if is_archive(p))
    if inner:
        if depth >= MAX_NEST_DEPTH:
            units.append((path.name, None, "archive", [finding(
                "FAIL", "archive_integrity", f"archives nested more than {MAX_NEST_DEPTH} levels deep, "
                f"{len(inner)} inner archive(s) not opened", missing=capped(str(p.relative_to(path)) for p in inner))]))
            return
        for a in inner:
            _discover(a, work, units, depth)
        return
    units.append((path.name, path, "unknown",
                  [finding("FAIL", "layout", "no GitHub/GitLab export layout, git repo or archive found")]))


# ---------------------------------------------------------------- generic checks

def zero_byte_findings(root: Path, layout=None):
    """0-byte files. A plain-git checkout's working tree legitimately holds empty files (.gitignore, py.typed), so
    there only the .git internals are judged; export layouts are all tool-written metadata and are judged whole."""
    out = []
    top = root / ".git" if layout == "plain-git" and (root / ".git").is_dir() else root
    for dirpath, _dirs, files in os.walk(top):
        for f in files:
            p = Path(dirpath) / f
            try:
                if p.is_symlink() or p.stat().st_size != 0:
                    continue
            except OSError:
                continue
            rel = str(p.relative_to(root))
            level = "WARN" if f in ZERO_BYTE_ALLOW else "FAIL"
            out.append(finding(level, "zero_byte", f"0-byte file: {rel}", missing=[rel] if level == "FAIL" else None))
    return out


def git(bare: Path, *args, check=False, timeout=None):
    what = "git " + (args[0] if args else "")
    return run_cmd(["git", "-c", "safe.directory=*", "-C", str(bare), *args], what, timeout, check=check,
                   env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def git_lines(bare: Path, *args, timeout=None, ok=(0,)):
    """Stream git output line by line (bounded memory). Raises CmdTimeout when the command overruns, Interrupted on a
    stop request and GitError on an exit code outside `ok`: a failing command must not read as 'no output'."""
    t = timeout or git_timeout()
    what = "git " + (args[0] if args else "")
    errf = tempfile.TemporaryFile()
    p = subprocess.Popen(["git", "-c", "safe.directory=*", "-C", str(bare), *args], stdout=subprocess.PIPE,
                         stderr=errf, text=True, encoding="utf-8", errors="replace",
                         env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    why = []
    done = threading.Event()

    def watch():
        deadline = time.monotonic() + t
        while not done.wait(0.5):
            if _STOP.is_set():
                why.append("stop")
            elif time.monotonic() > deadline:
                why.append("timeout")
            else:
                continue
            p.kill()
            return
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        for line in p.stdout:
            yield line
    finally:
        done.set()
        if p.poll() is None:
            p.kill()
        p.stdout.close()
        p.wait()
        watcher.join()
    try:
        if why == ["stop"]:
            raise Interrupted(what)
        if why:
            raise CmdTimeout(what, t)
        if p.returncode not in ok:
            errf.seek(0)
            raise GitError(what, p.returncode, errf.read().decode("utf-8", "replace"))
    finally:
        errf.close()


def git_int(bare: Path, *args):
    """Integer output of a git command that must succeed (rev-list --count ...)."""
    r = git(bare, *args)
    if r.returncode != 0:
        raise GitError("git " + args[0], r.returncode, r.stderr)
    return int(r.stdout.strip())


def git_dir_of(p: Path):
    return p / ".git" if (p / ".git").exists() else p


class LocUnmeasured(Exception):
    """Lines of code could not be measured (no scc, unreadable tree, scc output unusable): said out loud, never skipped."""


def _env_fault(err):
    err = err.lower()
    if "no space left" in err or "quota" in err:
        raise EnvironmentProblem("disk_full", err.strip()[:200])
    if "read-only file system" in err or "permission denied" in err:
        raise EnvironmentProblem("permission", err.strip()[:200])


def measure_loc(bare: Path, ref="HEAD"):
    """Code lines at `ref` using scc. Raises LocUnmeasured when that is not possible.

    The tree is checked out from the object store rather than `git archive`d (archive drops export-ignore'd paths) and
    scc runs with every ignore mechanism off (.gitignore/.ignore/.sccignore), so the count covers what git holds."""
    if not shutil.which("scc"):
        raise LocUnmeasured("scc is not installed")
    t = git_timeout()
    tmp = make_tmp("loc_")
    try:
        tree = os.path.join(tmp, "tree")
        os.mkdir(tree)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_INDEX_FILE": os.path.join(tmp, "index")}
        base = ["git", "-c", "safe.directory=*", f"--git-dir={bare}", f"--work-tree={tree}"]
        for cmd, what in (([*base, "read-tree", ref], "git read-tree"),
                          ([*base, "checkout-index", "-a", "-f", "--prefix", tree + os.sep], "git checkout-index")):
            r = run_cmd(cmd, what, t, env=env)
            _env_fault(r.stderr)
            if r.returncode != 0:
                raise LocUnmeasured(f"{what} failed (exit {r.returncode}): {r.stderr.strip()[:200]}")
        r = run_cmd(["scc", "--format", "json", "--no-cocomo", "--no-gitignore", "--no-ignore", "--no-scc-ignore", tree],
                    "scc", t)
        if r.returncode != 0:
            raise LocUnmeasured(f"scc failed (exit {r.returncode}): {r.stderr.strip()[:200]}")
        try:
            return sum(int(x.get("Code", 0)) for x in json.loads(r.stdout))
        except (ValueError, TypeError, AttributeError) as e:
            raise LocUnmeasured(f"scc output unreadable: {type(e).__name__}") from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


FSCK_CORRUPT = re.compile(r"corrupt|mismatch|inflate|crc|checksum|signature|truncat|garbage|unable to unpack|"
                          r"premature end|bad packed", re.I)
FSCK_MISSING = re.compile(r"missing|broken link|invalid sha1 pointer|unable to read|bad object|not a valid object|"
                          r"cannot be accessed|bad sha1", re.I)


def fsck_findings(bare: Path, label):
    """git fsck --full reads every object (connectivity-only never opens blobs). Any non-zero exit is a FAIL;
    missing, corrupt and malformed objects are reported separately."""
    r = git(bare, "fsck", "--full", "--no-dangling", timeout=fsck_timeout())
    if r.returncode == 0:
        return []
    groups = {"missing": [], "corrupt": [], "malformed": []}
    for ln in (r.stdout + "\n" + r.stderr).splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(("Checking ", "notice:")):
            continue
        key = "corrupt" if FSCK_CORRUPT.search(ln) else "missing" if FSCK_MISSING.search(ln) else "malformed"
        groups[key].append(ln)
    out = []
    for key, lines in groups.items():
        if lines:
            out.append(finding("FAIL", "git_integrity", f"{label}: git fsck found {len(lines)} {key} object problem(s) "
                               f"(exit {r.returncode}): " + " | ".join(lines[:3]), found=len(lines), item=key))
    if not out:
        out.append(finding("FAIL", "git_integrity", f"{label}: git fsck failed (exit {r.returncode})"))
    return out


# ---------------------------------------------------------------- forged / rewritten / recreated history
# Heuristics, never proof: a legitimate import or a clock-skewed laptop trips some of them, so every signal is a WARN
# that a person reads. Thresholds are named so a false-positive pattern can be tuned in one place.
FUTURE_SLACK_S = 86400           # a commit dated more than a day ahead of now
SKEW_S = 86400                   # author date after committer date, or a child older than its parent, by more than this
BULK_TS_MIN_COMMITS = 20         # this many commits on one exact committer second, and >= half of all commits
SINGLE_COMMITTER_MIN_COMMITS = 50
SINGLE_COMMITTER_MIN_AUTHORS = 3
SQUASH_MIN_FILES = 20            # one commit holding at least this many files looks like a flattened history
MAX_HISTORY_COMMITS = 500_000    # bound on memory; the scan says so when it stopped early
REFLOG_REWRITE = re.compile(r"\((?:amend|forced)[^)]*\)|rebase|forced-update|filter-(?:branch|repo)|reset: moving", re.I)
MAX_REFLOG_FILES, MAX_REFLOG_BYTES = 200, 2_000_000


def _hw(label, detail, **kw):
    return finding("WARN", "history_forgery", f"{label}: {detail}", **kw)


def _reflog_rewrites(gitdir: Path):
    logs = gitdir / "logs"
    if not logs.is_dir():
        return 0
    hits, files = 0, 0
    for p in logs.rglob("*"):
        if not p.is_file():
            continue
        files += 1
        if files > MAX_REFLOG_FILES:
            break
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for ln in fh.read(MAX_REFLOG_BYTES).splitlines():
                    if REFLOG_REWRITE.search(ln.partition("\t")[2]):
                        hits += 1
        except OSError:
            continue
    return hits


def history_scan(bare: Path, label: str):
    """(findings, first_commit_ts) for signs of forged, rewritten or recreated history. Needs fsck to have passed."""
    out: list = []
    now = time.time()
    commits = {}                      # sha -> (parents, committer ts)
    authors, committers = set(), set()
    ctimes: dict = {}
    future = reversed_dates = 0
    ex_future = ex_rev = ex_child = None
    child_older = 0
    truncated = False
    fmt = "%H%x1f%P%x1f%an <%ae>%x1f%cn <%ce>%x1f%at%x1f%ct"
    for ln in git_lines(bare, "log", "--all", f"--format={fmt}"):
        parts = ln.rstrip("\n").split("\x1f")
        if len(parts) != 6:
            continue
        sha, par, an, cn, at, ct = parts
        try:
            at, ct = int(at), int(ct)
        except ValueError:
            continue
        if len(commits) >= MAX_HISTORY_COMMITS:
            truncated = True
            break
        commits[sha] = (par.split(), ct)
        authors.add(an)
        committers.add(cn)
        ctimes[ct] = ctimes.get(ct, 0) + 1
        if ct > now + FUTURE_SLACK_S or at > now + FUTURE_SLACK_S:
            future += 1
            ex_future = ex_future or sha
        if at > ct + SKEW_S:
            reversed_dates += 1
            ex_rev = ex_rev or sha
    if not commits:
        return out, None
    n = len(commits)
    first_ts = min(ts for _, ts in commits.values())
    if truncated:
        out.append(_hw(label, f"history scan stopped after {MAX_HISTORY_COMMITS} commits; later commits were not checked"))
    if future:
        out.append(_hw(label, f"{future} commit(s) dated in the future (e.g. {ex_future[:12]})", found=future, item="future_dated"))
    if reversed_dates:
        out.append(_hw(label, f"{reversed_dates} commit(s) whose author date is later than committer date "
                       f"(e.g. {ex_rev[:12]})", found=reversed_dates, item="author_after_committer"))
    for sha, (parents, ct) in commits.items():
        for p in parents:
            pc = commits.get(p)
            if pc and pc[1] > ct + SKEW_S:
                child_older += 1
                ex_child = ex_child or sha
                break
    if child_older:
        out.append(_hw(label, f"{child_older} commit(s) older than its parent by more than a day (e.g. {ex_child[:12]}); "
                       "timestamps are not monotonic along the history", found=child_older, item="non_monotonic"))
    top_ts, top_n = max(ctimes.items(), key=lambda kv: kv[1])
    if top_n >= BULK_TS_MIN_COMMITS and top_n * 2 >= n:
        out.append(_hw(label, f"{top_n} of {n} commits share the same second as committer time, as a script rewrite "
                       "or bulk import would produce", found=top_n, expected=n, item="bulk_timestamp"))
    if n >= SINGLE_COMMITTER_MIN_COMMITS and len(committers) == 1 and len(authors) >= SINGLE_COMMITTER_MIN_AUTHORS:
        out.append(_hw(label, f"all {n} commits have one committer but {len(authors)} different authors "
                       "(history re-committed by one identity)", found=len(authors), item="single_committer"))
    head = "HEAD" if git(bare, "rev-parse", "--verify", "-q", "HEAD").returncode == 0 else None
    if head:
        roots = sum(1 for _ in git_lines(bare, "rev-list", "--max-parents=0", head))
        if roots > 1:
            out.append(_hw(label, f"{roots} root commits reachable from HEAD (unrelated histories were joined or "
                           "the history was rebuilt)", found=roots, item="multi_root"))
        if n == 1:
            files = sum(1 for _ in git_lines(bare, "ls-tree", "-r", "--name-only", head))
            if files >= SQUASH_MIN_FILES:
                out.append(_hw(label, f"history is a single commit holding {files} files, looks squashed or recreated",
                               found=files, item="squashed"))
    reachable = set(commits) if not truncated else None
    if reachable is not None:
        unreachable = 0
        for ln in git_lines(bare, "cat-file", "--batch-all-objects", "--batch-check=%(objecttype) %(objectname)"):
            kind, _, sha = ln.strip().partition(" ")
            if kind == "commit" and sha not in reachable:
                unreachable += 1
        if unreachable:
            out.append(_hw(label, f"{unreachable} unreachable commit(s) are stored but no ref leads to them "
                           "(deleted or rewritten history)", found=unreachable, item="unreachable"))
    rw = _reflog_rewrites(bare)
    if rw:
        out.append(_hw(label, f"reflog records {rw} history-rewriting operation(s) (amend/rebase/forced update/filter)",
                       found=rw, item="reflog_rewrite"))
    bad = 0
    for ln in git_lines(bare, "log", "--all", "--format=%G?"):
        if ln.strip() == "B":
            bad += 1
    if bad:
        out.append(_hw(label, f"{bad} commit(s) carry a signature that does not match the commit (BAD signature)",
                       found=bad, item="bad_signature"))
    return out, first_ts


def latest_branch(bare: Path, default_branch=None):
    """The branch (refs/heads/*) whose tip commit is newest, the same rule the profiler uses: a tie goes to the default branch,
    then to the first name in order. '' when there is no branch, or when GARMR_USE_DEFAULT_BRANCH=1 asks for the old behaviour:
    the caller then keeps the default branch. The default branch alone can be a stale stub while the real history sits elsewhere."""
    if os.environ.get("GARMR_USE_DEFAULT_BRANCH", "").strip().lower() in ("1", "true", "yes", "on"):
        return ""
    prefix = "refs/heads/"
    tips = []
    for ln in git_lines(bare, "for-each-ref", "--format=%(refname)%09%(committerdate:unix)", prefix):
        ref, _, ts = ln.partition("\t")
        if ref.startswith(prefix) and ts.strip().isdigit():
            tips.append((int(ts), ref[len(prefix):]))
    if not tips:
        return ""
    newest = max(ts for ts, _ in tips)
    tied = sorted(name for ts, name in tips if ts == newest)
    return default_branch if default_branch in tied else tied[0]


_BRANCH_MAPS: dict = {}


def branch_override(label):
    """The branch a caller asked Garmr to measure for this repo, or ''. GARMR_BRANCH_MAP is the path of a JSON object
    {"owner/name": "branch", "name": "branch"} (keys lower-case; the full label is tried before the bare name). The client's
    summary names the branch its numbers were measured on, and only that branch makes the comparison like with like. An
    unreadable or malformed map is ignored, so a bad file never fails a check."""
    path = os.environ.get("GARMR_BRANCH_MAP", "").strip()
    if not path:
        return ""
    try:
        mtime = os.stat(path).st_mtime
        cached = _BRANCH_MAPS.get(path)
        if not cached or cached[0] != mtime:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            clean = {str(k).lower(): v for k, v in data.items() if isinstance(v, str) and v} if isinstance(data, dict) else {}
            _BRANCH_MAPS[path] = cached = (mtime, clean)
    except (OSError, ValueError):
        return ""
    lab = (label or "").lower()
    for key in (lab, lab.rpartition("/")[2]):
        if key and key in cached[1]:
            return cached[1][key]
    return ""


def choose_branch(bare: Path, default_branch=None, label=None):
    """(branch, how) to measure. how: 'client' when the branch map names a branch that exists in this archive (it wins over
    everything, even GARMR_USE_DEFAULT_BRANCH), 'newest' when the newest-commit rule chose one, '' when the default branch stays."""
    want = branch_override(label)
    if want and git(bare, "show-ref", "--verify", "--quiet", f"refs/heads/{want}").returncode == 0:
        return want, "client"
    name = latest_branch(bare, default_branch)
    return (name, "newest") if name else ("", "")


def imported_history(created_at, first_ts):
    """True when the first commit is older than the repository record itself (import, mirror, recreation)."""
    if not created_at or first_ts is None:
        return False
    try:
        c = datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return first_ts < c - SKEW_S


# What each history_forgery signal (its `item`) says about the history, as a tag a person can filter on
HISTORY_TAG_OF_SIGNAL = {
    "created_after_history": "imported",
    "future_dated": "suspected_forged", "author_after_committer": "suspected_forged", "non_monotonic": "suspected_forged",
    "bulk_timestamp": "suspected_forged", "single_committer": "suspected_forged", "bad_signature": "suspected_forged",
    "multi_root": "flattened", "squashed": "flattened",
    "unreachable": "rewritten", "reflog_rewrite": "rewritten",
}
HISTORY_TAGS = ("imported", "forked", "suspected_forged", "flattened", "rewritten", "faulty")


def history_tags(findings, metrics):
    """Tags for what is odd about the history itself, built from signals already computed. They are labels for a person to
    read (suspected, never proof) and change no verdict: the findings that warn or fail are exactly the same without them."""
    tags = set()
    for x in findings:
        if x.get("check") == "history_forgery":
            tag = HISTORY_TAG_OF_SIGNAL.get(x.get("item"))
            if tag:
                tags.add(tag)
        elif x.get("level") == "FAIL" and x.get("check") in ("git_integrity", "repo_git", "archive_integrity"):
            tags.add("faulty")
    if metrics.get("fork"):
        tags.add("forked")
    if metrics.get("imported_prs"):          # merge commits whose PR numbers belong to the repo's earlier life
        tags.add("imported")
    if metrics.get("base_force_pushes"):     # the platform recorded base branches being force-pushed after PRs merged
        tags.add("rewritten")
    return [t for t in HISTORY_TAGS if t in tags]


def created_ts(created_at):
    try:
        return datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def foreign_merge(entries, pr, created_at):
    """True when every merge commit for PR number N looks like it belongs to another repository: dated before this
    repo existed, or naming a head branch other than the one PR #N has here (numbers collide after an import)."""
    c = created_ts(created_at) if created_at else None
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    ref = str_of(head.get("ref")).removeprefix("refs/heads/")
    for ts, src in entries:
        old = c is not None and ts is not None and ts < c - SKEW_S
        other = bool(ref and src and src.partition("/")[2] != ref)
        if not (old or other):
            return False
    return True


def created_vs_first_commit(label, created_at, first_ts):
    """History older than the repo itself means the repo was created after its commits existed (import / recreation)."""
    if imported_history(created_at, first_ts):
        c = datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).timestamp()
        days = int((c - first_ts) // 86400)
        return [_hw(label, f"first commit is {days} day(s) older than the repository's created_at ({created_at}); "
                    "the repo was recreated or imported from existing history", item="created_after_history")]
    return []


REF_PAT = r"(?:[\w.-]+/[\w.-]+)?#\d+"
CLOSE_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[: ]+(" + REF_PAT + r"(?:\s*(?:,|and|&)\s*" + REF_PAT + ")*)",
                      re.I)
ONE_REF = re.compile(r"([\w.-]+/[\w.-]+)?#(\d+)")
BARE_REF = re.compile(r"(?<![\w/.#-])#(\d+)\b")


def commit_messages_dated(bare: Path):
    """(committer ts or None, message) per commit, streamed."""
    buf, size = [], 0

    def emit():
        head, sep, body = "".join(buf).partition("\x1f")
        if not sep:
            head, body = "", "".join(buf)
        body = body.strip()
        return (int(head) if head.isdigit() else None, body) if body else None

    for line in git_lines(bare, "log", "--all", "--format=%ct%x1f%B%x1e"):
        if "\x1e" in line:
            head, _, tail = line.partition("\x1e")
            buf.append(head)
            item = emit()
            if item:
                yield item
            buf, size = [tail], len(tail)
        elif size < MAX_MESSAGE_CHARS:
            buf.append(line)
            size += len(line)
    item = emit()
    if item:
        yield item


def commit_messages(bare: Path):
    for _ts, msg in commit_messages_dated(bare):
        yield msg


def empty_facts():
    return {"pr_numbers": set(), "pr_source": "none", "metrics": {}, "merge_prs": set(), "squash_prs": set(), "merge_info": {},
            "closes": set(), "tags": set(), "branches": set(), "lfs_oids": set(), "refs_map": {}, "bare_mentions": set(),
            "pr_ref_count": 0, "refs_known": False, "first_commit_ts": None}


def git_facts(bare: Path, label: str, scan_lfs=True, want_loc=False, default_branch=None, empty_ok=False, slug=None):
    """(findings, facts) for one git dir. facts: pr_numbers, pr_source, metrics, ..."""
    f: list = []
    try:
        facts = _git_facts(f, git_dir_of(bare), label, scan_lfs, want_loc, default_branch, empty_ok, slug or label)
    except CmdTimeout as e:
        f.append(timeout_finding(label, e))
        facts = empty_facts()
    except GitError as e:
        # fsck passed, so a failing read means the repo could not be examined, never that it has nothing to report
        f.append(finding("FAIL", "git_integrity", f"{label}: {e} (not verified)"))
        facts = empty_facts()
    return f, facts


def _git_facts(f, bare, label, scan_lfs, want_loc, default_branch, empty_ok, slug):
    facts = empty_facts()
    metrics = facts["metrics"]
    if (bare / "shallow").exists():
        f.append(finding("FAIL", "git_integrity", f"{label}: shallow repository, history is truncated"))
    bad = fsck_findings(bare, label)
    if bad:
        f += bad
        return facts
    refs_map = {}
    for line in git_lines(bare, "for-each-ref", "--format=%(refname) %(objectname)"):
        if " " in line:
            k, v = line.rstrip("\n").split(" ", 1)
            refs_map[k] = v
    refs = list(refs_map)
    facts["refs_map"], facts["refs_known"] = refs_map, True
    if refs:
        try:
            hf, facts["first_commit_ts"] = history_scan(bare, label)
            f += hf
        except CmdTimeout as e:
            f.append(finding("WARN", "history_forgery", f"{label}: history forgery scan not completed, {e.what} timed out "
                             f"after {e.seconds:g}s (not verified)", item="timeout"))
        except GitError as e:
            f.append(finding("WARN", "history_forgery", f"{label}: history forgery scan not completed, {e}", item="git_error"))
    head_ok = git(bare, "rev-parse", "--verify", "-q", "HEAD").returncode == 0
    if not refs:
        f.append(finding("WARN" if empty_ok else "FAIL", "git_integrity",
                         f"{label}: no refs, repository has no history"
                         + (" (record/for-check says the repo is empty)" if empty_ok else "")))
    elif not head_ok:
        f.append(finding("WARN", "git_integrity", f"{label}: HEAD does not resolve (unborn/empty default branch)"))
    ref_nums = {int(m.group(1)) for r in refs for m in [re.match(r"refs/(?:pull|merge-requests)/(\d+)/head$", r)] if m}
    facts["pr_ref_count"] = len(ref_nums)
    merge_prs, squash_prs, closes, mentions = set(), set(), set(), set()
    merge_info: dict[int, list] = {}   # PR number -> [(commit ts, "owner/branch" or None)] to spot another repo's numbers
    if refs:
        for ts, m in commit_messages_dated(bare):
            merge_prs |= {int(n) for n in re.findall(r"^Merge pull request #(\d+)", m, re.M)}
            for n, src in re.findall(r"^Merge pull request #(\d+)(?: from (\S+))?", m, re.M):
                merge_info.setdefault(int(n), []).append((ts, src or None))
            # "See merge request group/proj!N" names its project; another project's MR is not this repo's
            merge_prs |= {int(n) for path, n in re.findall(r"See merge request (\S*?)!(\d+)", m)
                          if not path or path.lower() == slug.lower()}
            squash_prs |= {int(n) for n in re.findall(r"\(#(\d+)\)\s*$", m.splitlines()[0])}
            for grp in CLOSE_RE.findall(m):
                for other, n in ONE_REF.findall(grp):
                    if other and other.lower() != slug.lower():
                        continue  # closes other-owner/other-repo#77 is not a reference into this repo
                    closes.add(int(n))
            mentions |= {int(n) for n in BARE_REF.findall(m)}
    nums, source = ref_nums, "refs"
    if not nums:
        source = "commit-messages"
        nums = merge_prs | squash_prs
    # for-each-ref succeeded, so these are known even for a repo with no refs (0); a failed read leaves them absent
    metrics["branches"] = sum(1 for r in refs if r.startswith("refs/heads/"))
    metrics["tags"] = sum(1 for r in refs if r.startswith("refs/tags/"))
    if refs:
        metrics["commits_all"] = git_int(bare, "rev-list", "--count", "--all")
    lfs_oids = set()
    default_ref = f"refs/heads/{default_branch}" if default_branch and f"refs/heads/{default_branch}" in refs_map else "HEAD"
    if refs and git(bare, "rev-parse", "--verify", "-q", default_ref).returncode == 0:
        # the for-check's commit count is the platform's default-branch history, so this stays on the default branch
        metrics["commits_head"] = git_int(bare, "rev-list", "--count", default_ref)
    # everything else is measured on the branch with the newest commit (the rule the profiler uses), falling back to the default
    measured, by = choose_branch(bare, default_branch, label) if refs else ("", "")
    ref = f"refs/heads/{measured}" if measured else default_ref
    metrics["measured_branch"] = measured or (default_branch or "")
    metrics["measured_by"] = by or "default"
    if refs and git(bare, "rev-parse", "--verify", "-q", ref).returncode == 0:
        metrics["commits_measured"] = git_int(bare, "rev-list", "--count", ref)
        metrics["files"] = sum(1 for _ in git_lines(bare, "ls-tree", "-r", "--name-only", ref))
        if want_loc:
            try:
                metrics["loc"] = measure_loc(bare, ref)
            except CmdTimeout as e:
                f.append(timeout_finding(label, e))
                metrics["loc"] = None
            except LocUnmeasured as e:
                f.append(finding("WARN", "loc_unmeasured", f"{label}: lines of code not measured: {e}", item="loc"))
                metrics["loc"] = None
        if scan_lfs:
            n = sum(1 for _ in git_lines(bare, "grep", "-l", "-I", LFS_SIG, ref, "--", ok=(0, 1)))
            if n:
                have = (bare / "lfs" / "objects").exists()
                for ln in git_lines(bare, "grep", "-h", "^oid sha256:", ref, "--", ok=(0, 1)):
                    m = re.search(r"oid sha256:([0-9a-f]{64})", ln)
                    if m and len(lfs_oids) < MAX_LFS_OIDS:
                        lfs_oids.add(m.group(1))
                f.append(finding("WARN", "lfs", f"{label}: {n} LFS pointer file(s) at HEAD; "
                                 + ("lfs/objects present" if have else "LFS objects are NOT in the archive")))
    facts.update({"pr_numbers": nums, "pr_source": source, "merge_prs": merge_prs, "merge_info": merge_info, "squash_prs": squash_prs,
                  "closes": closes, "bare_mentions": mentions, "lfs_oids": lfs_oids,
                  "tags": {r[len("refs/tags/"):] for r in refs if r.startswith("refs/tags/")},
                  "branches": {r[len("refs/heads/"):] for r in refs if r.startswith("refs/heads/")}})
    return facts


def wiki_findings(wiki: Path, root: Path):
    """A *.wiki.git is a git repo too: full fsck plus 'has at least one ref'."""
    label = f"wiki {wiki.relative_to(root)}"
    out = fsck_findings(wiki, label)
    if out:
        return out
    try:
        has_ref = any(True for _ in git_lines(wiki, "for-each-ref", "--count=1"))
    except GitError as e:
        return [finding("FAIL", "git_integrity", f"{label}: {e} (not verified)")]
    if not has_ref:
        return [finding("FAIL", "git_integrity", f"{label}: wiki repository has no refs, content is missing",
                        missing=[str(wiki.relative_to(root))])]
    return []


def num_of(url):
    m = re.search(r"/(?:pull|pulls|issues|merge_requests)/(\d+)", str(url))
    return int(m.group(1)) if m else None


VISIBILITIES = ("public", "private", "internal")
GL_VISIBILITY_LEVEL = {0: "private", 10: "internal", 20: "public"}


def visibility_of(rec):
    """public / private / internal / unknown from one repository record (GitHub `private`, GitLab `visibility` /
    `visibility_level`, Bitbucket `is_private`). Anything absent or unrecognised is `unknown`, never a guess."""
    if not isinstance(rec, dict):
        return "unknown"
    v = rec.get("visibility")
    if isinstance(v, str) and v.strip().lower() in VISIBILITIES:
        return v.strip().lower()
    lvl = rec.get("visibility_level")
    if isinstance(lvl, str) and lvl.strip().isdigit():
        lvl = int(lvl)
    if isinstance(lvl, int) and not isinstance(lvl, bool) and lvl in GL_VISIBILITY_LEVEL:
        return GL_VISIBILITY_LEVEL[lvl]
    for k in ("is_private", "private"):
        if isinstance(rec.get(k), bool):
            return "private" if rec[k] else "public"
    return "unknown"


def visibility_findings(label, mine, others):
    """WARN when the archive's visibility is known and a for-check / live API value (others: [(origin, value)]) is
    known and different. Informational: a missing value on either side is skipped silently."""
    if mine not in VISIBILITIES:
        return []
    return [finding("WARN", "visibility_mismatch", f"{label}: archive record says {mine}, {origin} says {theirs}",
                    expected=theirs, found=mine, item=origin)
            for origin, theirs in others if theirs in VISIBILITIES and theirs != mine]


def pr_categories(recs, number):
    """-> {merged, unmerged} counts of distinct numbered PR/MR records, or None when there are none or one carries
    none of merged_at / closed_at / state (its status would be a guess). `unmerged` is everything not merged, closed
    without a merge and still open alike: a consumer comparing against merged PRs only needs that split. Duplicates
    count once, as merged if any of them is."""
    merged = {}
    for p in recs:
        n = number(p)
        if n is None:
            continue
        if not any(k in p for k in ("merged_at", "closed_at", "state")):
            return None
        is_merged = bool(str_of(p.get("merged_at")).strip()) or str(p.get("state") or "").strip().lower() == "merged"
        merged[n] = merged.get(n, False) or is_merged
    if not merged:
        return None
    return {"merged": sum(merged.values()), "unmerged": len(merged) - sum(merged.values())}


def set_pr_categories(m, cats):
    if cats:
        m["prs_merged"], m["prs_unmerged"] = cats["merged"], cats["unmerged"]


class CappedList(list):
    total = 0


def capped(items, n=200):
    """First n items as examples; .total keeps the real size so finding() can report it."""
    items = list(items)
    out = CappedList(items[:n])
    out.total = len(items)
    return out


# ---------------------------------------------------------------- metadata completeness (any record, git or not)

CORE_FIELDS = {
    "pull_request": ["url", "user", "repository", "title", "created_at", "base.ref", "base.sha", "head.ref", "head.sha"],
    "issue": ["url", "user", "repository", "title", "created_at"],
    "issue_comment": ["url", "user", "created_at"],
    "pull_request_review": ["url", "user", "pull_request"],
    "pull_request_review_comment": ["url", "user", "pull_request", "created_at"],
    "commit_comment": ["url", "user", "created_at"],
    "issue_event": ["url", "actor", "event", "created_at"],
    "label": ["url", "name", "color"],
    "milestone": ["url", "title"],
    "release": ["url", "tag_name"],
    "repository": ["url", "name", "owner"],
    "user": ["url"],
}
KIND_TYPE = {"pull_requests": "pull_request", "issues": "issue", "issue_comments": "issue_comment",
             "pull_request_reviews": "pull_request_review", "pull_request_review_comments": "pull_request_review_comment",
             "commit_comments": "commit_comment", "issue_events": "issue_event", "labels": "label",
             "milestones": "milestone", "releases": "release", "repositories": "repository", "users": "user"}
# kinds a GitHub migration normally carries; an empty one is INFO kind_absent unless something proves it should exist
EXPECTED_KINDS = ["issues", "pull_requests", "issue_comments", "pull_request_reviews", "pull_request_review_comments",
                  "issue_events", "releases", "milestones", "labels", "commit_comments"]
ENTITY_KIND = {"prs": "pull_requests", "issues": "issues", "labels": "labels", "milestones": "milestones",
               "releases": "releases", "issue_comments": "issue_comments",
               "review_comments": "pull_request_review_comments", "commit_comments": "commit_comments"}
GL_CORE_FIELDS = {  # GitLab ndjson relations
    "merge_requests": ["iid", "title", "state", "source_branch", "target_branch", "created_at"],
    "issues": ["iid", "title", "state", "created_at"],
    "labels": ["title", "color"],
    "milestones": ["title"],
}
# key present but null is fine for these (deleted accounts, optional text); absent key is not
NULL_OK = {"user", "actor", "color"}
PEER_EXCLUDE = {"issue", "pull_request", "commit_id", "before_commit_oid", "after_commit_oid", "label", "milestone",
                "assignee", "message", "body", "merged_by", "closed_at"}  # legitimately vary per record type
USER_KEYS = {"user", "actor", "assignee", "assignees", "owner", "author", "committer", "merged_by", "closed_by",
             "reviewer", "creator", "sender", "reviewers", "requested_reviewers"}
REPO_KEYS = {"repo", "repository", "commit_repository", "parent"}
SHA_KEYS = {"sha", "merge_commit_sha", "commit_id", "before_commit_oid", "after_commit_oid"}
HEX_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
HEX40 = HEX_SHA


def walk(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from walk(v, f"{path}.{k}" if path else k)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v, path)
    else:
        yield path, o


def lookup(rec, dotted):
    cur = rec
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def record_id(rec, i):
    return str(rec.get("url") or rec.get("iid") or rec.get("id") or f"#{i}")


def _common(freqs, n):
    """Threshold-free peer rule: present in more than half of the peers => expected in all of them."""
    return [k for k, c in freqs.items() if c < n and c * 2 > n and k not in PEER_EXCLUDE]


# Records of one kind can carry type-specific fields (a "referenced" event has commit_repository, a "closed" one does not;
# an attachment hangs off exactly one parent key), so peers are only compared within the same sub-kind.
ATTACHMENT_PARENTS = ("issue_comment", "pull_request", "pull_request_review_comment", "issue", "release")
PEER_GROUP = {
    "issue_event": lambda r: str(r.get("event")),
    "attachment": lambda r: tuple(k for k in ATTACHMENT_PARENTS if k in r),
}
PEER_GROUP["issue_events"], PEER_GROUP["attachments"] = PEER_GROUP["issue_event"], PEER_GROUP["attachment"]


def _peer_missing(recs):
    """({rec index: [missing field, ...]}) among uniform peers; fields most peers have but this one lacks."""
    n = len(recs)
    if n < 4:
        return {}
    freq: dict[str, int] = {}
    for r in recs:
        for k in r:
            freq[k] = freq.get(k, 0) + 1
    common = _common(freq, n)
    sub_common = {}
    for k in {k for r in recs for k, v in r.items() if isinstance(v, dict)}:
        holders = [r[k] for r in recs if isinstance(r.get(k), dict)]
        sf: dict[str, int] = {}
        for d in holders:
            for sub in d:
                sf[sub] = sf.get(sub, 0) + 1
        if len(holders) >= 4:
            sub_common[k] = _common(sf, len(holders))
    miss = {}
    for i, r in enumerate(recs):
        m = [k for k in common if k not in r]
        for k, subs in sub_common.items():
            if isinstance(r.get(k), dict):
                m += [f"{k}.{s}" for s in subs if s not in r[k]]
        if m:
            miss[i] = m
    return miss


def incomplete_records(recs, core, group=None):
    """Records missing fields the kind requires, or fields most of their same-kind peers have. Applies to ALL records."""
    peer: dict[int, list] = {}
    buckets: dict = {}
    for i, r in enumerate(recs):
        buckets.setdefault(group(r) if group else None, []).append(i)
    for idxs in buckets.values():
        for j, m in _peer_missing([recs[i] for i in idxs]).items():
            peer[idxs[j]] = m
    out = []
    for i, r in enumerate(recs):
        rid = record_id(r, i)
        for fld in core:
            ok, v = lookup(r, fld)
            if not ok or (v in (None, "") and fld.split(".")[-1] not in NULL_OK and fld not in NULL_OK):
                out.append(f"{rid}:{fld}")
        out += [f"{rid}:{k}" for k in peer.get(i, []) if k not in core]
    return out


def page_findings(pages):
    out = []
    for kind, pg in pages.items():
        nums = sorted(n for n, _c, _f in pg)
        have = set(nums)
        gaps = []
        for n in range(1, nums[-1] + 1):
            if n not in have:
                gaps.append(f"{kind}_{n:06d}.json")
                if len(gaps) >= 200:
                    break
        if gaps:
            gaps = capped(gaps)
            gaps.total = nums[-1] - len(have)   # the loop above stops listing at 200, the shortfall is exact
            out.append(finding("FAIL", "page_sequence", f"{kind}: page file(s) missing from the sequence: {gaps[:5]}",
                               expected=nums[-1], found=len(nums), missing=gaps, item=kind))
        final = max(pg, key=lambda x: x[0])
        others = [c for n, c, _f in pg if n != final[0]]
        if others:
            # the last page may be short, never longer than a full page: a non-final page below the median full page
            # (or below the last page) lost records
            want = max(statistics.median(others), final[1])
            short = [f for n, c, f in sorted(pg) if n != final[0] and c < want]
            if short:
                out.append(finding("FAIL", "page_partial", f"{kind}: non-final page(s) hold fewer than {want:g} records "
                                   f"(truncated?): {short[:5]}", missing=capped(short), item=kind))
    return out


def is_bot(url):
    name = urllib.parse.unquote(url.rstrip("/").rsplit("/", 1)[-1]).lower()
    return name.endswith("[bot]") or name == "ghost"


def inside_root(root: Path, rel: str):
    """The archive file a tarball:// / upload reference names, or None when it escapes the archive root."""
    rel = urllib.parse.unquote(rel).strip()
    if not rel or rel.startswith("/") or "\x00" in rel:
        return None
    base = root.resolve()
    p = (root / rel)
    try:
        real = p.resolve()
        real.relative_to(base)
    except (ValueError, OSError, RuntimeError):
        return None
    return p


def file_exists(root: Path, rel: str):
    p = inside_root(root, rel)
    try:
        return p is not None and p.exists()
    except OSError:
        return False


def reference_findings(root: Path, all_recs, host):
    """Everything a record points at (users, repos, files) must be exported. Applies to ALL records."""
    out = []
    known = set()
    for rec in all_recs:
        for path, v in walk(rec):
            segs = path.split(".")
            if segs[-1] == "url" and isinstance(v, str) and not (set(segs[:-1]) & USER_KEYS):
                known.add(v.split("#")[0])
    user_re = re.compile("^" + re.escape(host) + r"[^/#?]+$")
    repo_re = re.compile("^" + re.escape(host) + r"[^/#?]+/[^/#?]+$")
    users, bots, repos, files = set(), set(), set(), set()
    for rec in all_recs:
        for path, v in walk(rec):
            if not isinstance(v, str):
                continue
            segs = path.split(".")
            leaf = segs[-1]
            as_user = leaf in USER_KEYS or (leaf == "url" and len(segs) > 1 and segs[-2] in USER_KEYS)
            as_repo = leaf in REPO_KEYS or (leaf == "url" and len(segs) > 1 and segs[-2] in REPO_KEYS)
            if v.startswith("tarball://"):
                files.add(v[len("tarball://"):].split("/", 1)[1].rstrip("/") if "/" in v[len("tarball://"):] else "")
            elif as_user and user_re.match(v) and v not in known:
                (bots if is_bot(v) else users).add(v)
            elif as_repo and repo_re.match(v) and v not in known:
                repos.add(v)
    if users:
        out.append(finding("FAIL", "dangling_reference", f"{len(users)} user(s) referenced but not in the export: "
                           f"{sorted(users)[:5]}", missing=capped(sorted(users)), item="users"))
    if bots:
        out.append(finding("WARN", "dangling_reference", f"{len(bots)} bot/ghost account(s) referenced but not in the "
                           f"users file (not exportable): {sorted(bots)[:5]}", missing=capped(sorted(bots)), item="bots"))
    if repos:
        out.append(finding("WARN", "dangling_reference", f"{len(repos)} repo(s) referenced but not exported "
                           f"(forks?): {sorted(repos)[:5]}", missing=capped(sorted(repos)), item="repositories"))
    gone = sorted(f for f in files if not file_exists(root, f))
    if gone:
        out.append(finding("FAIL", "file_refs", f"{len(gone)} file(s) referenced by metadata are not in the archive "
                           f"(or point outside it): {gone[:5]}", missing=capped(gone), item="files"))
    return out


def missing_shas(bare: Path, shas):
    if not shas:
        return set()
    r = run_cmd(["git", "-c", "safe.directory=*", "-C", str(bare), "cat-file", "--batch-check"], "git cat-file",
                input="\n".join(sorted(shas)) + "\n")
    if r.returncode != 0:
        raise GitError("git cat-file", r.returncode, r.stderr)
    return {ln.split()[0] for ln in r.stdout.splitlines() if ln.endswith(" missing")}


# ---------------------------------------------------------------- for-check / live comparisons

# count entities the for-check can carry (default_branch/size_kb/has_* are not numeric lower bounds)
COUNT_ENTITIES = ["prs", "issues", "labels", "milestones", "releases", "issue_comments", "review_comments",
                  "commit_comments", "branches", "tags", "collaborators", "commits"]
GITHUB_REF = re.compile(r"^refs/(heads/.+|tags/.+|pull/\d+/head)$")
GITLAB_REF = re.compile(r"^refs/(heads|tags)/.+$")
GITLAB_ENTITIES = ["issues", "prs", "labels", "milestones", "releases", "branches", "tags", "commits"]
SOFT_COUNTS = {"collaborators"}       # the API counts the owner/outside collaborators the export does not carry
OPTIONAL_COUNTS = {"collaborators"}   # comes from an optional embedded field: key absent => comparison skipped


def compare_counts(label, mine, bounds, origin):
    """Archive count below the lower bound fails; above the upper bound only warns. bounds: {entity: (lo, hi)}."""
    out = []
    for k, (lo, hi) in bounds.items():
        a = mine.get(k)
        if a is None:
            continue
        if a < lo:
            if k in SOFT_COUNTS:
                out.append(finding("WARN", "live_counts", f"{label}: {k}: archive {a} vs {origin} {lo} (definitions "
                                   f"differ: the API counts the repo owner / outside collaborators)",
                                   expected=lo, found=a, item=k) | {"missing_count": lo - a})
                continue
            out.append(finding("FAIL", "live_counts", f"{label}: {k}: archive {a} vs {origin} {lo} ({lo - a} missing)",
                               expected=lo, found=a, item=k) | {"missing_count": lo - a})
        elif a > hi:
            out.append(finding("WARN", "live_counts", f"{label}: {k}: archive {a} vs {origin} {hi} "
                               f"(deleted since export?)", expected=hi, found=a, item=k))
    return out


def live_bounds(live, owner, name, label, side=None):
    """-> ({entity: (v, v)}, [finding]). A failing API call is a FAIL (nothing was verified), never guessed; so is an
    entity the API would not count (403/404/rate limit), which is reported per entity (`item`) instead of vanishing."""
    try:
        api = dict(live(owner, name) or {})
    except Exception as e:  # network/auth/rate limit
        return {}, [finding("FAIL", "live_check_failed", f"{label}: live API check failed, counts NOT verified: "
                            f"{type(e).__name__}: {e}")]
    errs = api.pop("_errors", None) or {}
    vis = api.pop("visibility", None)    # a string, informational: not a count and never a live_check_failed
    if side is not None:
        side["visibility"] = vis
    skipped = [(k, errs.get(k, "no answer")) for k, v in api.items() if v is None]
    out = [finding("WARN" if k in SOFT_COUNTS else "FAIL", "live_check_failed",
                   f"{label}: live API could not count {k}, NOT verified ({why})", item=k) for k, why in skipped]
    return {k: (v, v) for k, v in api.items() if v is not None}, out


def for_check_findings(fc, label, single, mine, facts, ref_pattern, entities, record_default_branch=None):
    """Counts (min/max of the two snapshots), refs and default branch of one repo against its for-check rows.

    -> (findings, bounds). No usable for-check: ([], {}). A for-check with repo sections none of which matches this
    repo is a FAIL for_check_unmatched, never a silent skip.
    """
    if not fc or fc.error:
        return [], {}
    key = fc.match_repo(label, single)
    if key is None:
        keys = fc.repo_keys()
        if not keys:
            return [], {}
        theirs = sorted(fc.labels.get(k, k) for k in keys)
        return [finding("FAIL", "for_check_unmatched", f"{label}: no for-check repo section matches this repo; the "
                        f"for-check has {theirs[:5]}, the archive has {label!r} (renamed, wildcard, or several repos?): "
                        f"counts and refs were NOT compared", missing=capped(theirs), item=label)], {}
    bounds, unknown = fc.bounds(key, entities)
    out = [finding("INFO", "for_check_unknown", f"{label}: {e} is blank in the for-check, not compared", item=e)
           for e in unknown]
    out += [finding("INFO", "for_check_unknown", f"{label}: {e} is absent from the archive's repository record, "
                    f"not compared", item=e)
            for e in entities if e in OPTIONAL_COUNTS and e in bounds and mine.get(e) is None]
    out += ref_findings(label, *fc.ref_snapshots(key), facts.get("refs_map") or {}, ref_pattern,
                        known=facts.get("refs_known"))
    want = fc.source_default_branch(key)
    if want and record_default_branch and want != record_default_branch:
        out.append(finding("WARN", "default_branch", f"{label}: source default branch {want!r}, archive record says "
                           f"{record_default_branch!r}", expected=want, found=record_default_branch, item="source"))
    return out + compare_counts(label, mine, bounds, "source"), bounds


# for-check entity -> (the org-reference metric it supersedes, the name used in the extra_metrics column)
FOR_CHECK_SUPERSEDES = {"prs": ("prs", "prs_metadata"), "commits": ("commits", "commits"),
                        "branches": ("branches", "branches"), "tags": ("tags", "tags")}


def apply_for_check_reference(m, mine, bounds):
    """The for-check holds the SOURCE's own counts, so for the metrics it bounds it outranks any org reference (the profiler
    re-run or the client's summary): batch.compare_metrics skips them and flags ORG_SUPERSEDED. What the archive holds above
    the for-check's upper bound is an extra, reported in the extra_metrics column."""
    sup, extras = [], []
    for ent, (metric, name) in FOR_CHECK_SUPERSEDES.items():
        if ent not in bounds:
            continue
        sup.append(metric)
        a, hi = mine.get(ent), bounds[ent][1]
        if a is not None and a > hi:
            extras.append((name, a - hi, hi))
    m["for_check_supersedes"], m["for_check_extras"] = sup, extras


def for_check_orphans(fc, labels):
    """for-check repo sections that match no repo of the unit."""
    if not fc or fc.error or not labels:
        return []
    keys = fc.repo_keys()
    used = {fc.match_repo(lb, len(labels) == 1) for lb in labels} - {None}
    out = []
    for k in sorted(keys - used):
        out.append(finding("FAIL", "for_check_unmatched", f"for-check repo section {fc.labels.get(k, k)!r} matches no "
                           f"repo in the archive (archive has {sorted(labels)[:5]}): its counts and refs were NOT "
                           f"compared", item=fc.labels.get(k, k)))
    return out


def repo_export_findings(fc, root: Path):
    """requested repos the source did not export; exported repos with no repositories/<owner>/<name>.git."""
    out = []
    exported = {r.lower() for r in fc.exported}
    if exported:
        gone = sorted(r for r in fc.requested if r.lower() not in exported)
        if gone:
            out.append(finding("FAIL", "repo_not_exported", f"{len(gone)} requested repo(s) are not in the source's "
                               f"export list: {gone[:5]}", expected=len(fc.requested),
                               found=len(fc.requested) - len(gone), missing=gone, item="requested"))
    present = {f"{g.parent.name}/{g.name[:-4]}".lower() for g in (root / "repositories").glob("*/*.git")
               if not g.name.endswith(".wiki.git")}
    absent = sorted(r for r in fc.exported if r.lower() not in present)
    if absent:
        out.append(finding("FAIL", "repo_not_exported", f"{len(absent)} repo(s) the source says were exported have no "
                           f"repositories/<owner>/<name>.git in the archive: {absent[:5]}",
                           expected=len(fc.exported), found=len(fc.exported) - len(absent), missing=absent,
                           item="exported"))
    return out


def ref_findings(label, before, after, archive_refs, pattern, known=True):
    """Refs on the source both before and after the export must be in the archive's git (deletions mid-export are
    tolerated); a ref at a different sha than either snapshot only warns (the branch moved).

    `known` False = the archive's refs could not be read (fsck failed / no git): nothing to compare against."""
    if not archive_refs and not known:
        return []
    both = sorted(r for r in before if r in after and pattern.match(r))
    lost = [r for r in both if r not in archive_refs]
    out = []
    if lost:
        out.append(finding("FAIL", "refs", f"{label}: {len(lost)} ref(s) on the source before and after the export are "
                           f"not in the archive's git: {lost[:5]}", expected=len(both), found=len(both) - len(lost),
                           missing=capped(lost)))
    moved = [r for r in both if r in archive_refs
             and archive_refs[r] not in {before[r], after[r]} and (before[r] or after[r])]
    if moved:
        out.append(finding("WARN", "refs", f"{label}: {len(moved)} ref(s) point at a different commit than the source "
                           f"recorded (moved during export?): {moved[:5]}", missing=capped(moved), item="sha"))
    return out


# ---------------------------------------------------------------- GitHub migration

def read_json(p: Path, findings):
    """-> (ok, data). Unreadable / non-UTF-8 / invalid JSON is a FAIL metadata_json finding, never a crash."""
    try:
        size = p.stat().st_size
        if size > max_json_bytes():
            findings.append(finding("FAIL", "metadata_json", f"{p.name}: {size} bytes is over the {max_json_bytes()} byte "
                                    f"limit (GARMR_MAX_JSON_BYTES), not loaded and NOT verified", missing=[p.name]))
            return False, None
        return True, json.loads(p.read_bytes().decode("utf-8"))
    except OSError as e:
        findings.append(finding("FAIL", "metadata_json", f"{p.name}: unreadable ({type(e).__name__}: {e})", missing=[p.name]))
    except ValueError as e:  # includes UnicodeDecodeError and JSONDecodeError
        findings.append(finding("FAIL", "metadata_json", f"{p.name}: unreadable JSON ({e})", missing=[p.name]))
    return False, None


def load_github_kinds(root: Path, findings):
    kinds: dict[str, list] = {}
    pages: dict[str, list] = {}
    for p in sorted(root.glob("*.json")):
        m = GITHUB_JSON.match(p.name)
        if not m:
            continue
        try:
            size = p.stat().st_size
        except OSError as e:
            findings.append(finding("FAIL", "metadata_json", f"{p.name}: unreadable ({type(e).__name__}: {e})",
                                    missing=[p.name]))
            continue
        if size == 0:
            continue
        ok, data = read_json(p, findings)
        if not ok:
            continue
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            findings.append(finding("FAIL", "metadata_json", f"{p.name}: content is {type(data).__name__}, expected a "
                                    f"list of records", missing=[p.name]))
            continue
        recs = [x for x in data if isinstance(x, dict)]
        if len(recs) != len(data):
            findings.append(finding("FAIL", "metadata_json", f"{p.name}: {len(data) - len(recs)} record(s) are not JSON "
                                    f"objects", missing=[p.name]))
        kinds.setdefault(m.group("kind"), []).extend(recs)
        pages.setdefault(m.group("kind"), []).append((int(m.group("n")), len(recs), p.name))
    return kinds, pages


def ref_urls(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [u for x in v for u in ref_urls(x)]
    if isinstance(v, dict):
        return [v["url"]] if isinstance(v.get("url"), str) else []
    return [str(v)]


REPO_URL = re.compile(r"^(https?://[^/]+/[^/#?]+/[^/#?]+)")


def repo_key(url):
    m = REPO_URL.match(url) if isinstance(url, str) else None
    return m.group(1).lower() if m else None


def record_repo_key(rec):
    """Which repo a record belongs to: its repository key, else its url prefix, else its parent issue/PR url."""
    for fld in ("repository", "repo", "commit_repository"):
        v = rec.get(fld)
        k = repo_key(v.get("url") if isinstance(v, dict) else v)
        if k:
            return k
    k = repo_key(rec.get("url"))
    if k:
        return k
    for fld in ("issue", "pull_request"):
        for u in ref_urls(rec.get(fld)):
            k = repo_key(u)
            if k:
                return k
    return None


def index_by_repo(kinds):
    """One pass over all records: {repo_key | None: {kind: [records]}}."""
    idx: dict = {}
    for kind, recs in kinds.items():
        for r in recs:
            idx.setdefault(record_repo_key(r), {}).setdefault(kind, []).append(r)
    return idx


def split_name(url, rec):
    """owner, name of a repository record (url may be null/malformed)."""
    u = str_of(url).rstrip("/")
    parts = u.split("/")
    if "/" in u and len(parts) >= 2 and parts[-1] and parts[-2]:
        return parts[-2], parts[-1]
    owner = str_of(rec.get("owner")).rstrip("/").rsplit("/", 1)[-1] or "?"
    return owner, str_of(rec.get("name")) or "?"


def int_or_none(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def check_github(root: Path, live, scan_lfs, want_loc, fc=None, ctx=None):
    """Returns (unit_findings, repo_records)."""
    ctx = ctx if ctx is not None else {}
    uf, records = [], []
    kinds, pages = load_github_kinds(root, uf)
    repos = kinds.get("repositories", [])
    req = [n for n, ok in (("schema.json", (root / "schema.json").exists()),
                           ("repositories_*.json", bool(repos) or any(root.glob("repositories_*.json"))),
                           ("users_*.json", any(root.glob("users_*.json")))) if not ok]
    if req:
        uf.append(finding("FAIL", "required_file", f"required archive file(s) missing: {req}", missing=req))
    uf += page_findings(pages)
    by_type: dict[str, list] = {}
    for kind, recs in kinds.items():
        for r in recs:
            by_type.setdefault(str(r.get("type") or KIND_TYPE.get(kind, kind)), []).append(r)
    for kind, recs in by_type.items():
        gaps = incomplete_records(recs, CORE_FIELDS.get(kind, []), PEER_GROUP.get(kind))
        if gaps:
            uf.append(finding("FAIL", "record_incomplete", f"{kind}: {len(gaps)} field(s) missing from "
                              f"{len({g.rsplit(':', 1)[0] for g in gaps})} record(s), e.g. {gaps[:3]}",
                              missing=capped(gaps), item=kind))
    usable = fc if fc and not fc.error else None
    if usable and repos:
        uf += repo_export_findings(usable, root)
    if repos:
        m = re.match(r"https?://[^/]+/", str_of(repos[0].get("url")))
        host = m.group(0) if m else "https://github.com/"
        uf += reference_findings(root, [r for recs in kinds.values() for r in recs], host)
    present = {}
    if (root / "repositories").is_dir():
        for g in sorted((root / "repositories").glob("*/*.git")):
            if not g.name.endswith(".wiki.git"):
                present[f"{g.parent.name}/{g.name[:-4]}".lower()] = g
    S = SimpleNamespace(kinds=kinds, idx=index_by_repo(kinds), single=len(repos) == 1, present=present, fc=usable,
                        live=live, scan_lfs=scan_lfs, want_loc=want_loc, evidence={}, soft={}, ctx=ctx)
    listed = set()
    for r in repos:
        owner, name = split_name(r.get("url"), r)
        label = f"{owner}/{name}"
        f: list = []
        rec = {"repo": label, "provider": "github", "findings": f, "metrics": {}}
        records.append(rec)
        try:
            _github_repo(S, root, r, owner, name, label, f, rec, listed)
        except EnvironmentProblem:
            raise
        except CmdTimeout as e:
            f.append(timeout_finding(label, e))
        except Exception as e:
            f.append(finding("FAIL", "analysis_error", f"{label}: {type(e).__name__}: {e}"))
    for key, g in present.items():
        if g.resolve() in listed:
            continue
        owner, name = g.parent.name, g.name[:-4]
        label = f"{owner}/{name}"
        rel = str(g.relative_to(root))
        f = [finding("FAIL", "repo_unlisted", f"{label}: {rel} is in the archive but not listed in repositories_*.json "
                     f"(its metadata was not exported)", missing=[rel], item=label)]
        rec = {"repo": label, "provider": "github", "findings": f, "metrics": {}}
        records.append(rec)
        try:
            gf, facts = git_facts(g, label, scan_lfs, want_loc)
            f += gf
            rec["metrics"] = dict(facts["metrics"])
            mine = {"branches": facts["metrics"].get("branches"), "tags": facts["metrics"].get("tags"),
                    "commits": facts["metrics"].get("commits_head")}
            f += for_check_findings(usable, label, False, mine, facts, GITHUB_REF, ["branches", "tags", "commits"])[0]
        except EnvironmentProblem:
            raise
        except Exception as e:
            f.append(finding("FAIL", "analysis_error", f"{label}: {type(e).__name__}: {e}"))
    if (root / "repositories").is_dir():
        for g in sorted((root / "repositories").glob("*/*.wiki.git")):
            try:
                uf += wiki_findings(g, root)
            except CmdTimeout as e:
                uf.append(timeout_finding(str(g.relative_to(root)), e))
    if repos:
        uf += kind_findings(S)
        uf += for_check_orphans(usable, [x["repo"] for x in records])
    return uf, records


def note(S, kind, why):
    S.evidence.setdefault(kind, []).append(why)


def kind_findings(S):
    """A kind with no records at all: INFO kind_absent, or FAIL kind_missing when something proves it must exist."""
    out = []
    repos = S.kinds.get("repositories", [])
    for kind in EXPECTED_KINDS:
        total = len(S.kinds.get(kind, []))
        if kind in ("labels", "milestones"):
            total += sum(len(ref_urls(r.get(kind))) for r in repos)
        if total:
            continue
        why = S.evidence.get(kind)
        if why:
            out.append(finding("FAIL", "kind_missing", f"{kind}: no records exported, but {why[0]}"
                               + (f" (+{len(why) - 1} more reason(s))" if len(why) > 1 else ""),
                               missing=capped(why, 10), item=kind))
        elif S.soft.get(kind):
            out.append(finding("WARN", "kind_missing", f"{kind}: no records exported, but {S.soft[kind][0]} "
                               f"(imported history: those numbers may belong to the repo's earlier life)",
                               missing=capped(S.soft[kind], 10), item=kind))
        else:
            out.append(finding("INFO", "kind_absent", f"{kind}: no records in the export (none, or the file is "
                               f"missing/empty); nothing proves it should have any", item=kind))
    return out


def first_gaps(seen, limit=10):
    """(count of numbers in 1..max(seen) not in seen, first `limit` of them) without walking the range."""
    s = sorted(seen)
    total = s[-1] - len(s)
    gaps, prev = [], 0
    for v in s:
        n = prev + 1
        while n < v and len(gaps) < limit:
            gaps.append(n)
            n += 1
        prev = v
        if len(gaps) >= limit:
            break
    return total, gaps


def _github_repo(S, root, r, owner, name, label, f, rec, listed):
    kinds, idx, single, fc = S.kinds, S.idx, S.single, S.fc
    url = str_of(r.get("url"))
    key = repo_key(url) or url.lower()

    def of_repo(kind):
        out = list(idx.get(key, {}).get(kind, []))
        return out + idx.get(None, {}).get(kind, []) if single else out

    vis = visibility_of(r)
    rec["metrics"]["visibility"] = vis
    bare = S.present.get(f"{owner}/{name}".lower())
    if bare is None:
        rel = f"repositories/{owner}/{name}.git"
        f.append(finding("FAIL", "repo_git", f"{label}: listed in metadata but {rel} is missing", missing=[rel]))
        return
    listed.add(bare.resolve())
    fkey = fc.match_repo(label, single) if fc else None
    empty_ok = int_or_none(r.get("size")) == 0 or int_or_none(r.get("commits")) == 0
    if fc and fkey:
        empty_ok = empty_ok or fc.bounds(fkey, ["commits"])[0].get("commits") == (0, 0)
    gf, facts = git_facts(bare, label, S.scan_lfs, S.want_loc, r.get("default_branch"), empty_ok, label)
    f += gf
    f += created_vs_first_commit(label, r.get("created_at"), facts.get("first_commit_ts"))
    m = rec["metrics"] = dict(facts["metrics"], visibility=vis)
    if r.get("fork"):
        m["fork"] = True
    m["base_force_pushes"] = sum(1 for e in of_repo("issue_events") if e.get("event") == "base_ref_force_pushed")
    prs = of_repo("pull_requests")
    meta_prs = {n for n in (num_of(p.get("url")) or int_or_none(p.get("number")) for p in prs) if n is not None}
    set_pr_categories(m, pr_categories(prs, lambda p: num_of(p.get("url")) or int_or_none(p.get("number"))))
    pr_nums = facts["pr_numbers"]
    m["prs_git"], m["prs_meta"] = len(pr_nums), len(meta_prs)
    if meta_prs or (fc and fkey and any(fc.ref_snapshots(fkey))):
        S.ctx["refs_expected"] = True
    missing = sorted(pr_nums - meta_prs)
    # a fork / mirror / migrated history carries other repos' "Merge pull request #N" lines: only a hint there
    foreign = imported_history(r.get("created_at"), facts.get("first_commit_ts")) or bool(r.get("fork"))
    if missing:
        f.append(finding("WARN" if foreign and facts["pr_source"] == "commit-messages" else "FAIL", "pull_requests",
                         f"{label}: git history has {len(pr_nums)} PR(s) ({facts['pr_source']}), "
                         f"metadata has {len(meta_prs)}; missing: {missing[:20]}{'...' if len(missing) > 20 else ''}",
                         expected=len(pr_nums), found=len(meta_prs), missing=capped(missing)))
        why = f"git has {len(pr_nums)} PR(s) ({facts['pr_source']}), e.g. #{missing[0]}"
        if foreign and facts["pr_source"] == "commit-messages":
            S.soft.setdefault("pull_requests", []).append(why)   # numbers from an earlier life: a hint, not proof
        else:
            note(S, "pull_requests", why)
    issues = of_repo("issues")
    if meta_prs and facts["refs_known"]:
        if facts["pr_ref_count"] == 0:
            f.append(finding("FAIL", "git_refs", f"{label}: metadata lists {len(meta_prs)} PR(s) but the archive's git "
                             f"has no refs/pull/* at all: {sorted(meta_prs)[:20]}", expected=len(meta_prs), found=0,
                             missing=capped(sorted(meta_prs))))
        else:
            ghost = sorted(meta_prs - pr_nums)
            if ghost:
                f.append(finding("FAIL", "git_refs", f"{label}: {len(ghost)} PR(s) in metadata have no refs/pull/N/head "
                                 f"in the archive's git: {ghost[:20]}", expected=len(meta_prs),
                                 found=len(meta_prs) - len(ghost), missing=capped(ghost)))
    issue_nums = {n for n in (num_of(i.get("url")) for i in issues) if n}
    marker = facts["closes"] | facts["merge_prs"] | facts["squash_prs"]
    gone = sorted(marker - issue_nums - meta_prs - (pr_nums - meta_prs))
    if gone:
        f.append(finding("WARN" if foreign else "FAIL", "number_refs", f"{label}: git history refers to issue/PR "
                         f"#{gone[:10]} (closes/merge/squash) but the export has no record of it"
                         + (" (imported/fork history: the numbers may belong to another repository)" if foreign else ""),
                         missing=capped(gone)))
        if not foreign:
            note(S, "issues", f"git history says closes/merges #{gone[0]}, which no record accounts for")
    every = set(issue_nums) | meta_prs
    for kind, recs in idx.get(key, {}).items():
        for x in recs:
            every.add(num_of(x.get("url")))
            every |= {num_of(u) for fld in ("issue", "pull_request") for u in ref_urls(x.get(fld))}
    every.discard(None)
    bare_only = sorted(facts["bare_mentions"] - marker - every - pr_nums)
    if bare_only:
        f.append(finding("WARN", "number_refs", f"{label}: git history mentions #{bare_only[:10]} (bare reference) but "
                         f"no exported record has that number", missing=capped(bare_only), item="mentions"))
    by_num = {num_of(p.get("url")): p for p in prs}
    unmerged = sorted(n for n in facts["merge_prs"] if n in by_num and not by_num[n].get("merged_at"))
    imported = [n for n in unmerged if facts["merge_info"].get(n)
                and foreign_merge(facts["merge_info"][n], by_num[n], r.get("created_at"))]
    unmerged = [n for n in unmerged if n not in imported]
    if imported:
        m["imported_prs"] = True
        f.append(finding("WARN", "pr_state", f"{label}: merge commit(s) for PR(s) {imported[:10]} predate this repo or "
                         f"name another head branch than the PR record of that number; the numbers likely belong to "
                         f"the repo's earlier life, so the unmerged metadata is not judged"))
    if unmerged:
        f.append(finding("FAIL", "pr_state", f"{label}: git has a merge commit for PR(s) {unmerged[:10]} but the "
                         f"metadata shows them unmerged (merged_at empty)", missing=[f"#{n}" for n in unmerged]))
    sq = sorted(n for n in facts["squash_prs"] - facts["merge_prs"] if n in by_num and not by_num[n].get("merged_at"))
    if sq:
        f.append(finding("WARN", "pr_state", f"{label}: squash-style commit refers to PR(s) {sq[:10]} that the "
                         f"metadata shows unmerged"))
    rel = [x for x in of_repo("releases") if not x.get("draft")]
    no_tag = sorted({x.get("tag_name") for x in rel if x.get("tag_name") and x.get("tag_name") not in facts["tags"]})
    if no_tag:
        f.append(finding("FAIL", "release_tag", f"{label}: release(s) point at tag(s) missing from the archive's "
                         f"git: {no_tag[:5]}", missing=capped(no_tag)))
    db = r.get("default_branch")
    if db and facts["refs_map"] and f"refs/heads/{db}" not in facts["refs_map"]:
        f.append(finding("FAIL", "default_branch", f"{label}: metadata default branch {db!r} is "
                         f"not a branch in the archive's git", missing=[db]))
    hard, soft = set(), set()
    owned = [(k, x) for k, recs in idx.get(key, {}).items() for x in recs]
    if single:
        owned += [(k, x) for k, recs in idx.get(None, {}).items() for x in recs]
    for k, x in owned:
        merged = bool(x.get("merged_at")) and str(x.get("type") or KIND_TYPE.get(k)) == "pull_request"
        for path, v in walk(x):
            leaf = path.split(".")[-1]
            if leaf in SHA_KEYS and isinstance(v, str) and HEX_SHA.match(v):
                (hard if leaf == "merge_commit_sha" and merged else soft).add(v)
    for level, group in (("FAIL", hard), ("WARN", soft - hard)):
        lost = sorted(missing_shas(git_dir_of(bare), group))
        if lost:
            f.append(finding(level, "commits", f"{label}: {len(lost)} commit sha(s) referenced by metadata are not "
                             f"in the archive's git" + ("" if level == "FAIL" else " (test merge / fork / force-pushed "
                             "commits can legitimately be absent)") + f": {[x[:10] for x in lost[:5]]}",
                             expected=len(group), found=len(group) - len(lost), missing=capped(lost)))
    # real archives embed labels/milestones in the repository record; keep the standalone files as a fallback
    labels = set(ref_urls(r.get("labels")))
    labels |= {u for u in (x.get("url") for x in of_repo("labels")) if isinstance(u, str)}
    m["labels"] = len(labels)
    m["issues"] = len([i for i in issues if "/pull/" not in str(i.get("url"))])
    used = {u for x in prs + issues for u in ref_urls(x.get("labels"))}
    if used - labels:
        f.append(finding("FAIL", "labels",
                         f"{label}: {len(used - labels)} label(s) referenced by issues/PRs but not exported "
                         f"({len(labels)} exported): {sorted(used - labels)[:5]}",
                         expected=len(used), found=len(used & labels), missing=capped(sorted(used - labels))))
        if not labels:
            note(S, "labels", f"issues/PRs reference {len(used)} label(s)")
    ms = set(ref_urls(r.get("milestones")))
    ms |= {u for u in (x.get("url") for x in of_repo("milestones")) if isinstance(u, str)}
    used_ms = {u for x in prs + issues for u in ref_urls(x.get("milestone"))}
    if used_ms - ms:
        f.append(finding("FAIL", "milestones", f"{label}: milestone(s) referenced but not exported: "
                         f"{sorted(used_ms - ms)[:5]}", expected=len(used_ms), found=len(used_ms & ms),
                         missing=capped(sorted(used_ms - ms))))
        if not ms:
            note(S, "milestones", f"issues/PRs reference {len(used_ms)} milestone(s)")
    parents = {num_of(x.get("url")) for x in prs + issues}
    events = of_repo("issue_events")
    ev_orphans = {num_of(u) for e in events for u in ref_urls(e.get("pull_request")) + ref_urls(e.get("issue"))}
    ev_orphans -= parents | {None}
    if ev_orphans:
        f.append(finding("FAIL", "events_parent", f"{label}: issue_events point at issue/PR "
                         f"#{sorted(ev_orphans)[:10]} that was not exported", missing=capped(sorted(ev_orphans))))
    seen = (parents | pr_nums | {num_of(e.get("url")) for e in events}) - {None}
    if seen:
        total, gaps = first_gaps(seen)
        if total > 0:
            f.append(finding("WARN", "number_gaps", f"{label}: issue/PR numbers 1..{max(seen)} share one sequence; "
                             f"{total} unaccounted for (deleted, or issues not exported): {gaps}"))
    for kind, fld in (("issue_comments", "issue"), ("pull_request_reviews", "pull_request"),
                      ("pull_request_review_comments", "pull_request")):
        orphan_urls = {u for x in of_repo(kind) for u in ref_urls(x.get(fld))}
        orphans = {num_of(u) for u in orphan_urls} - parents - {None}
        if orphans:
            f.append(finding("FAIL", "comments_parent",
                             f"{label}: {kind} point at issue/PR #{sorted(orphans)[:10]} that was not exported",
                             missing=capped(sorted(orphans))))
        if of_repo(kind) and kind in ("issue_comments",) and not issues and any("/issues/" in u for u in orphan_urls):
            note(S, "issues", f"{kind} reference issue(s) that are not exported")
        if of_repo(kind) and kind != "issue_comments" and not prs and orphans:
            note(S, "pull_requests", f"{kind} reference PR(s) that are not exported")
    if S.live or S.fc:
        collab = r.get("collaborators") if "collaborators" in r else None
        collab_n = len(collab) if isinstance(collab, list) else int_or_none(collab)
        mine = {"prs": len(meta_prs), "issues": m["issues"], "labels": len(labels), "milestones": len(ms),
                "releases": len(of_repo("releases")), "issue_comments": len(of_repo("issue_comments")),
                "review_comments": len(of_repo("pull_request_review_comments")),
                "commit_comments": len(of_repo("commit_comments")), "branches": m.get("branches"),
                "tags": m.get("tags"), "collaborators": collab_n, "commits": m.get("commits_head")}
        bounds, side = {}, {}
        if S.live:
            bounds, live_f = live_bounds(S.live, owner, name, label, side)
        fc_f, fc_bounds = for_check_findings(fc, label, single, mine, facts, GITHUB_REF, COUNT_ENTITIES,
                                             r.get("default_branch"))
        f += fc_f
        apply_for_check_reference(m, mine, fc_bounds)
        others = [("live API", side.get("visibility"))]
        if fc and fkey:
            others.append(("for-check", fc.source_visibility(fkey)))
        f += visibility_findings(label, vis, others)
        # an entity the for-check already bounds is verified by it; the live API is only the fallback
        f += [x for x in (live_f if S.live else []) if x.get("item") not in fc_bounds]
        for src in (fc_bounds, bounds):
            for ent, kind in ENTITY_KIND.items():
                if src.get(ent, (0, 0))[0] > 0 and mine.get(ent) == 0:
                    note(S, kind, f"the {'for-check' if src is fc_bounds else 'live API'} counts {src[ent][0]} {ent}")
        # a live API answer is only used for entities the for-check has no bounds for
        f += compare_counts(label, mine, {k: v for k, v in bounds.items() if k not in fc_bounds}, "live")


# ---------------------------------------------------------------- GitLab export

def read_ndjson(p: Path, findings):
    """One line at a time, so the file itself is never held in memory (the parsed records are: they are what is checked)."""
    out = []
    bad_objs = 0
    try:
        with open(p, "rb") as fh:
            for i, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line.decode("utf-8"))
                except ValueError as e:
                    findings.append(finding("FAIL", "metadata_json", f"{p.name}:{i}: bad JSON ({e})", missing=[p.name]))
                    break
                if isinstance(rec, dict):
                    out.append(rec)
                else:
                    bad_objs += 1
    except OSError as e:
        findings.append(finding("FAIL", "metadata_json", f"{p.name}: unreadable ({type(e).__name__}: {e})", missing=[p.name]))
        return out
    if bad_objs:
        findings.append(finding("FAIL", "metadata_json", f"{p.name}: {bad_objs} line(s) are not JSON objects",
                                missing=[p.name]))
    return out


UPLOAD_RE = re.compile(r"/uploads/([0-9a-f]{32})/([^)\s\"'\\]+)")


def find_bundle(root: Path):
    for n in ("repo.bundle", "project.bundle"):
        for cand in [root / n, *sorted(root.glob(f"*/{n}"))]:
            if cand.is_file():
                return cand
    return None


def check_gitlab(root: Path, work: Path, scan_lfs, want_loc, fallback_name, fc=None):
    uf, f = [], []
    pj = {}
    pj_path = next((p for p in (root / "project.json", root / "tree" / "project.json") if p.exists()), None)
    if pj_path is not None:
        try:
            has_body = pj_path.stat().st_size > 0
        except OSError:
            has_body = False
        if has_body:
            ok, data = read_json(pj_path, f)
            pj = data if ok and isinstance(data, dict) else {}
    label = str_of(pj.get("path_with_namespace")) or str_of(pj.get("name")) or fallback_name
    vis = visibility_of(pj)
    rec = {"repo": label, "provider": "gitlab", "findings": f, "metrics": {"visibility": vis}}
    tree = root / "tree" / "project"
    if not tree.is_dir():
        f.append(finding("FAIL", "metadata", "tree/project/ missing"))
        return uf, [rec]
    nd = {p.stem: read_ndjson(p, f) for p in tree.glob("*.ndjson") if p.stat().st_size}
    for rel, recs in nd.items():
        gaps = incomplete_records(recs, GL_CORE_FIELDS.get(rel, []))
        if gaps:
            f.append(finding("FAIL", "record_incomplete", f"{rel}: {len(gaps)} field(s) missing from records, e.g. "
                             f"{gaps[:3]}", missing=capped(gaps), item=rel))
    found_ups: set = set()
    for nd_path in tree.glob("*.ndjson"):
        try:
            with open(nd_path, "r", encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    found_ups |= {f"uploads/{h}/{n}" for h, n in re.findall(UPLOAD_RE, ln)}
        except OSError:
            continue        # already reported as unreadable by read_ndjson
    ups = sorted(found_ups)
    lost = [u for u in ups if not file_exists(root, u)]
    if lost:
        f.append(finding("FAIL", "file_refs", f"{len(lost)} upload(s) referenced by issues/MRs/notes are not in the "
                         f"archive: {lost[:5]}", expected=len(ups), found=len(ups) - len(lost), missing=capped(lost),
                         item="uploads"))
    lfs_files = set()
    if (root / "lfs-objects").is_dir():
        for q in (root / "lfs-objects").rglob("*"):
            if q.is_file():
                lfs_files.add("".join(q.relative_to(root / "lfs-objects").parts))
    listed_oids = {v for rec_ in nd.get("lfs_objects", []) for path, v in walk(rec_)
                   if path.split(".")[-1] in ("oid", "file_sha256") and isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)}
    bundle = find_bundle(root)
    facts = None
    if bundle is None:
        f.append(finding("FAIL", "repo_git", "no repo.bundle / project.bundle in export", missing=["repo.bundle"]))
    else:
        holder = Path(make_tmp("gl_", work))
        try:
            clone = holder / "c.git"
            try:
                r = run_cmd(["git", "clone", "--bare", "-q", str(bundle), str(clone)], "git clone")
                if r.returncode != 0:
                    f.append(finding("FAIL", "git_integrity", "bundle does not clone: " + r.stderr.strip()[:200]))
                else:
                    # clone --bare only takes heads/tags; MR refs live elsewhere in the bundle
                    fr = run_cmd(["git", "-C", str(clone), "fetch", "-q", str(bundle), "+refs/*:refs/*"], "git fetch")
                    if fr.returncode != 0:
                        f.append(finding("WARN", "git_refs", f"fetching MR refs from the bundle failed, MR ref checks "
                                         f"may be incomplete: {fr.stderr.strip()[:200]}"))
                    gf, facts = git_facts(clone, bundle.name, scan_lfs, want_loc, slug=label)
                    f += [x for x in gf if x["check"] != "lfs"]  # GitLab exports carry LFS objects: judged below
                    rec["metrics"] = dict(facts["metrics"], visibility=vis)
                    listed_oids |= facts["lfs_oids"]
            except CmdTimeout as e:
                f.append(timeout_finding(bundle.name, e))
        finally:
            shutil.rmtree(holder, ignore_errors=True)
    set_pr_categories(rec["metrics"], pr_categories(nd.get("merge_requests", []), lambda x: int_or_none(x.get("iid"))))
    if facts:
        have = {x.get("iid") for x in nd.get("merge_requests", [])}
        rec["metrics"]["prs_git"], rec["metrics"]["prs_meta"] = len(facts["pr_numbers"]), len(have)
        missing = sorted(facts["pr_numbers"] - have)
        foreign = imported_history(pj.get("created_at"), facts.get("first_commit_ts"))
        if missing:
            f.append(finding("WARN" if foreign and facts["pr_source"] == "commit-messages" else "FAIL", "merge_requests",
                             f"git history has {len(facts['pr_numbers'])} MR(s) ({facts['pr_source']}), "
                             f"metadata has {len(have)}; missing: {missing[:20]}",
                             expected=len(facts["pr_numbers"]), found=len(have), missing=capped(missing)))
        iids = {x.get("iid") for x in nd.get("issues", [])} | have
        gone = sorted((facts["closes"] | facts["merge_prs"]) - iids - facts["pr_numbers"])
        if gone:
            f.append(finding("WARN" if foreign else "FAIL", "number_refs", f"git history refers to issue/MR "
                             f"#{gone[:10]} (closes/merge) but the export has no record of it"
                             + (" (imported history: the numbers may belong to another project)" if foreign else ""),
                             missing=capped(gone)))
    lost_lfs = sorted(o for o in listed_oids if o not in lfs_files)
    if lost_lfs:
        f.append(finding("FAIL", "file_refs", f"{len(lost_lfs)} LFS object(s) listed by metadata or pointed at by git "
                         f"are not in lfs-objects/: {[o[:12] for o in lost_lfs[:5]]}", expected=len(listed_oids),
                         found=len(listed_oids) - len(lost_lfs), missing=capped(lost_lfs), item="lfs"))
    titles = {x.get("title") for x in nd.get("labels", [])}
    rec["metrics"]["labels"] = len(titles)
    used, group_used = set(), set()
    for x in nd.get("issues", []) + nd.get("merge_requests", []):
        links = x.get("label_links")
        for lk in links if isinstance(links, list) else []:
            lab = lk.get("label") if isinstance(lk, dict) else None
            t = lab.get("title") if isinstance(lab, dict) else None
            if t is None:
                continue
            (group_used if lab.get("group_id") or lab.get("type") == "GroupLabel" else used).add(t)
    if used - titles:
        f.append(finding("FAIL", "labels", f"{len(used - titles)} label(s) referenced but not exported: "
                         f"{sorted(used - titles)[:5]}", expected=len(used), found=len(used & titles),
                         missing=capped(sorted(used - titles))))
    if group_used - titles:
        f.append(finding("WARN", "labels", f"{len(group_used - titles)} group-level label(s) referenced but not in the "
                         f"project's labels (they live on the group): {sorted(group_used - titles)[:5]}",
                         missing=capped(sorted(group_used - titles)), item="group"))
    m = rec["metrics"]
    mine = {"issues": len(nd.get("issues", [])), "prs": len(nd.get("merge_requests", [])), "labels": len(titles),
            "milestones": len(nd.get("milestones", [])), "releases": len(nd.get("releases", [])),
            "branches": m.get("branches"), "tags": m.get("tags"), "commits": m.get("commits_head")}
    gl_f, gl_bounds = for_check_findings(fc, label, True, mine, facts or {}, GITLAB_REF, GITLAB_ENTITIES, pj.get("default_branch"))
    f += gl_f
    apply_for_check_reference(m, mine, gl_bounds)
    fkey = fc.match_repo(label, True) if fc and not fc.error else None
    if fkey:
        f += visibility_findings(label, vis, [("for-check", fc.source_visibility(fkey))])
    return uf, [rec]


def plain_git_name(root: Path, fallback):
    r = git(git_dir_of(root), "config", "--get", "remote.origin.url")
    parts = [p for p in re.split(r"[/:]", r.stdout.strip()) if p]
    if len(parts) >= 2:
        return f"{parts[-2]}/{GIT_SUFFIX.sub('', parts[-1])}"
    return fallback


# ---------------------------------------------------------------- orchestration

PROVIDER_OF_LAYOUT = {"github-migration": "github", "gitlab-export": "gitlab", "plain-git": "git"}


def placeholder_repo(name, layout):
    return {"repo": archive_stem(name), "provider": PROVIDER_OF_LAYOUT.get(layout, "unknown"), "findings": [],
            "metrics": {}, "placeholder": True}


def analyse_unit(name, root, layout, pre, work, live=None, scan_lfs=True, want_loc=False, fc=None):
    """-> {'name','layout','unit_findings','repos':[{repo,provider,findings,metrics}]}

    Never returns an empty `repos` (a placeholder row carries unit-level findings) and never raises, except
    EnvironmentProblem (the host is at fault; the caller must stop and tell the operator)."""
    uf = list(pre)
    repos: list = []
    try:
        _analyse_unit(uf, repos, name, root, layout, work, live, scan_lfs, want_loc, fc)
    except EnvironmentProblem:
        raise
    except CmdTimeout as e:
        uf.append(timeout_finding(archive_stem(name), e))
    except Exception as e:
        uf.append(finding("FAIL", "analysis_error", f"{archive_stem(name)}: {type(e).__name__}: {e}"))
    if not repos:
        repos.append(placeholder_repo(name, layout))
    return {"name": name, "layout": layout, "unit_findings": uf, "repos": repos}


def _analyse_unit(uf, repos, name, root, layout, work, live, scan_lfs, want_loc, fc):
    if root is None:
        return
    usable = fc if fc and not fc.error else None
    ctx: dict = {}
    zero = zero_byte_findings(root, layout)
    uf += zero
    if layout == "github-migration":
        more, recs = check_github(root, live, scan_lfs, want_loc, fc, ctx)
        uf += more
        repos += recs
    elif layout == "gitlab-export":
        more, recs = check_gitlab(root, work, scan_lfs, want_loc, archive_stem(name), fc)
        uf += more
        repos += recs
        uf += for_check_orphans(usable, [x["repo"] for x in recs])
    elif layout == "plain-git":
        label = plain_git_name(root, archive_stem(name))
        key = usable.match_repo(label, True) if usable else None
        empty_ok = bool(key) and usable.bounds(key, ["commits"])[0].get("commits") == (0, 0)
        rec = {"repo": label, "provider": "git", "findings": [], "metrics": {}}
        repos.append(rec)
        gf, facts = git_facts(root, label, scan_lfs, want_loc, empty_ok=empty_ok)
        rec["findings"] += gf
        rec["metrics"] = dict(facts["metrics"])
        if key and any(usable.ref_snapshots(key)):
            ctx["refs_expected"] = True
        mine = {"branches": facts["metrics"].get("branches"), "tags": facts["metrics"].get("tags"),
                "commits": facts["metrics"].get("commits_head")}
        rec["findings"] += for_check_findings(fc, label, True, mine, facts, GITHUB_REF, ["branches", "tags", "commits"])[0]
        uf += for_check_orphans(usable, [label])
    if ctx.get("refs_expected"):
        for x in zero:   # a zeroed packed-refs while PR/ref metadata exists means every packed ref is gone
            if x["check"] == "zero_byte" and x["detail"].rsplit("/", 1)[-1].endswith("packed-refs"):
                x["level"] = "FAIL"
                x["missing"] = [x["detail"].split(": ", 1)[-1]]


def check_path(path, workdir=None, live=None, scan_lfs=True, want_loc=False):
    path = Path(path)
    own = workdir is None
    work = Path(make_tmp("exportcheck_")) if own else Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    try:
        raw: list = []
        discover_units(path, work, raw)
        units = []
        for name, root, layout, pre in raw:
            u = analyse_unit(name, root, layout, pre, work, live, scan_lfs, want_loc)
            fs = list(u["unit_findings"]) + [x for r in u["repos"] for x in r["findings"]]
            u["findings"] = fs
            u["verdict"] = "INCOMPLETE" if any(x["level"] == "FAIL" for x in fs) else "COMPLETE"
            units.append(u)
        flat = [dict(x, unit=u["name"]) for u in units for x in u["findings"]]
        verdict = "COMPLETE" if units and all(u["verdict"] == "COMPLETE" for u in units) else "INCOMPLETE"
        return {"input": str(path), "verdict": verdict, "units": units, "findings": flat}
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- live GitHub counts

def github_live(token, api_base="https://api.github.com"):
    def call(path):
        req = urllib.request.Request(api_base + path, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r), r.headers

    def count_list(path):
        n, page = 0, 1
        sep = "&" if "?" in path else "?"
        while True:
            data, _ = call(f"{path}{sep}per_page=100&page={page}")
            n += len(data)
            if len(data) < 100:
                return n
            page += 1

    def search(owner, repo, kind):
        q = urllib.parse.quote(f"repo:{owner}/{repo} is:{kind}")
        return call(f"/search/issues?q={q}&per_page=1")[0]["total_count"]

    def last_page(path):
        data, hdr = call(path + ("&" if "?" in path else "?") + "per_page=1")
        m = re.search(r'[?&]page=(\d+)>; rel="last"', hdr.get("Link", "") or "")
        return int(m.group(1)) if m else len(data)

    errors: dict = {}

    def safe(name, fn, *a):
        try:
            return fn(*a)
        except Exception as e:  # no access / not enabled / rate limited: reported per entity, never counted as a pass
            errors[name] = f"{type(e).__name__}: {e}"[:200]
            return None

    def repo_visibility(base):
        return visibility_of(call(base)[0])

    def live(owner, repo):
        errors.clear()
        base = f"/repos/{owner}/{repo}"
        out = {"prs": search(owner, repo, "pr"), "issues": search(owner, repo, "issue"),
               "labels": count_list(f"{base}/labels"), "milestones": count_list(f"{base}/milestones?state=all"),
               "releases": count_list(f"{base}/releases"),
               "issue_comments": safe("issue_comments", count_list, f"{base}/issues/comments"),
               "review_comments": safe("review_comments", count_list, f"{base}/pulls/comments"),
               "commit_comments": safe("commit_comments", count_list, f"{base}/comments"),
               "branches": safe("branches", count_list, f"{base}/branches"),
               "tags": safe("tags", count_list, f"{base}/tags"),
               "collaborators": safe("collaborators", count_list, f"{base}/collaborators?affiliation=all"),
               "commits": safe("commits", last_page, f"{base}/commits"),
               "visibility": safe("visibility", repo_visibility, base)}
        out["_errors"] = dict(errors)
        return out
    return live


def print_report(rep):
    for u in rep["units"]:
        print(f"\n[{u['verdict']}] {u['name']}  ({u['layout']})")
        for x in u["findings"]:
            print(f"  {x['level']:<4} {x['check']:<16} {x['detail']}")
        if not u["findings"]:
            print("  all checks passed")
    print(f"\nOVERALL: {rep['verdict']}  ({sum(1 for u in rep['units'] if u['verdict'] == 'COMPLETE')}"
          f"/{len(rep['units'])} units complete)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ad-hoc check of one archive/dir. For folder runs use batch.py.")
    ap.add_argument("paths", nargs="+", help="archive, 'Download all' zip, or extracted directory")
    ap.add_argument("--json", help="write full report to this file")
    ap.add_argument("--workdir", help="extract here (kept); default is a temp dir that is removed")
    ap.add_argument("--live", action="store_true", help="compare counts with the GitHub API (needs token)")
    ap.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN"))
    ap.add_argument("--api-base", default="https://api.github.com")
    ap.add_argument("--no-lfs-scan", action="store_true")
    ap.add_argument("--strict", action="store_true", help="treat warnings as failures")
    a = ap.parse_args(argv)
    missing = [p for p in a.paths if not Path(p).exists()]
    if missing:
        print(f"not found: {missing}", file=sys.stderr)
        return 2
    live = None
    if a.live:
        if not a.github_token:
            print("--live needs --github-token or GITHUB_TOKEN", file=sys.stderr)
            return 2
        live = github_live(a.github_token, a.api_base)
    try:
        reports = [check_path(p, a.workdir, live, not a.no_lfs_scan) for p in a.paths]
    except EnvironmentProblem as e:
        print(f"environment problem ({e.kind}): {e.detail}", file=sys.stderr)
        return 2
    for r in reports:
        print_report(r)
    if a.json:
        Path(a.json).write_text(json.dumps(reports[0] if len(reports) == 1 else reports, indent=2))
    bad = any(r["verdict"] != "COMPLETE" for r in reports)
    if a.strict:
        bad = bad or any(x["level"] == "WARN" for r in reports for x in r["findings"])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
