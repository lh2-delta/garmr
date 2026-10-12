"""Garmr: reader for <archive>.for-check.csv, the source-side ground truth migration-downloader writes next to each archive.

Long format, columns `repo,section,key,value`. A blank value means "unknown" and is never guessed or failed on.
This module loads the file, exposes snapshot bounds, and runs the archive-level checks (transfer, state, file listing,
repositories). Per-repo count/ref comparisons live in check_export, which only calls the accessors below.
"""
from __future__ import annotations

import csv
import hashlib
import os
import stat
from pathlib import Path

from check_export import COUNT_ENTITIES, archive_stem, capped, finding

SUFFIX = ".for-check.csv"
COLUMNS = ["repo", "section", "key", "value"]
GOOD_STATES = {"exported", "finished"}
TAR_SUFFIXES = (".tar.gz", ".tgz", ".tar")
SKIP_DIRS = {"__MACOSX"}
def to_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def for_check_name(archive_name: str) -> str:
    return archive_stem(archive_name) + SUFFIX


class ForCheck:
    def __init__(self, path: Path, rows=(), error=None):
        self.path, self.error = Path(path), error
        self.error_kind = "unreadable" if error else None
        self.meta: dict[str, str] = {}
        self.requested: list[str] = []
        self.exported: list[str] = []
        self.files: dict[str, int] = {}
        self.summary: dict[str, int | None] = {}
        self.counts = {"before": {}, "after": {}}   # repo(lower) -> {entity: raw value}
        self.refs = {"before": {}, "after": {}}     # repo(lower) -> {refname: sha}
        self.labels: dict[str, str] = {}            # repo(lower) -> label as written
        for repo, section, key, value in rows:
            self._add(repo, section, key, value)

    def _add(self, repo, section, key, value):
        if section == "meta":
            self.meta[key] = value
        elif section == "requested_repo":
            self.requested.append(key)
        elif section == "export_repo":
            self.exported.append(key)
        elif section == "file":
            self.files[key] = to_int(value)
        elif section == "file_summary":
            self.summary[key] = to_int(value)
        elif section.startswith(("counts_", "ref_")):
            tag = section.rsplit("_", 1)[1]
            store = self.counts if section.startswith("counts_") else self.refs
            if tag in store:
                self.labels.setdefault(repo.lower(), repo)
                store[tag].setdefault(repo.lower(), {})[key] = value

    @property
    def label(self) -> str:
        """Display name when no archive matches: the single repo label, else the archive/for-check file stem."""
        repos = {r for r in (list(self.requested) + list(self.exported)) if r}
        if len(repos) == 1:
            return next(iter(repos))
        name = self.meta.get("archive") or self.path.name[: -len(SUFFIX)]
        return archive_stem(name)

    def repo_keys(self) -> set[str]:
        return {k for store in (self.counts, self.refs) for tag in store.values() for k in tag}

    def match_repo(self, label: str, single_unit: bool):
        """Case-insensitive label match; a lone for-check repo also matches a lone unit whatever its label."""
        keys = self.repo_keys()
        if label.lower() in keys:
            return label.lower()
        if single_unit and len(keys) == 1:
            return next(iter(keys))
        return None

    def bounds(self, key: str, entities=COUNT_ENTITIES):
        """-> ({entity: (lo, hi)}, [entities unknown in both snapshots]).

        Both snapshots known -> (min, max); one known -> (v, v). Anything that dropped between the two (deleted while
        the export ran) therefore only has to be present up to the lower value.
        """
        before, after = self.counts["before"].get(key, {}), self.counts["after"].get(key, {})
        out, unknown = {}, []
        for e in entities:
            vals = [v for v in (to_int(before.get(e)), to_int(after.get(e))) if v is not None]
            if vals:
                out[e] = (min(vals), max(vals))
            else:
                unknown.append(e)
        return out, unknown

    def source_default_branch(self, key: str):
        for tag in ("after", "before"):
            v = (self.counts[tag].get(key, {}).get("default_branch") or "").strip()
            if v:
                return v
        return None

    def ref_snapshots(self, key: str):
        return self.refs["before"].get(key, {}), self.refs["after"].get(key, {})


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def _unusable(path, kind, why):
    fc = ForCheck(path, error=why)
    fc.error_kind = kind          # "unreadable" | "empty"
    return fc


def load(path: Path) -> ForCheck:
    """Never raises. An unusable file becomes a ForCheck with .error (why) and .error_kind (unreadable|empty) set:
    bad header/delimiter/encoding, no rows, no `meta` rows, or neither `counts_*` nor `file` rows."""
    try:
        text = _decode(Path(path).read_bytes())
        if not text.strip():
            return _unusable(path, "empty", "file is empty")
        rdr = csv.reader(text.splitlines())
        header = [h.strip().lower() for h in next(rdr, [])]
        if header[:4] != COLUMNS:
            hint = ""
            for d in (";", "\t", "|"):
                if [h.strip().lower() for h in (text.splitlines()[0].split(d))][:4] == COLUMNS:
                    hint = f" (delimiter looks like {d!r}, must be a comma)"
            return _unusable(path, "unreadable", f"header is {header}, expected {COLUMNS}{hint}")
        rows = [(r + [""] * 4)[:4] for r in rdr if r]
    except (OSError, UnicodeError, csv.Error) as e:
        return _unusable(path, "unreadable", f"{type(e).__name__}: {e} (expected UTF-8 or UTF-16 comma-separated)")
    if not rows:
        return _unusable(path, "empty", "file has a header but no rows")
    fc = ForCheck(path, rows)
    sections = {r[1] for r in rows}
    if "meta" not in sections:
        return _unusable(path, "unreadable", "no `meta` rows: not a migration-downloader for-check")
    if not any(s.startswith("counts_") or s == "file" for s in sections):
        return _unusable(path, "unreadable", "no `counts_*` and no `file` rows: nothing to check against")
    return fc


def _hidden(p: Path) -> bool:
    return p.name.startswith("._") or any(part in SKIP_DIRS for part in p.parts)


def load_folder(input_dir: Path) -> list[ForCheck]:
    return [load(p) for p in sorted(Path(input_dir).rglob("*" + SUFFIX)) if not _hidden(p)]


def match_archive(fcs: list[ForCheck], arch: Path, used: set):
    """Same-stem file next to the archive first (case-insensitive), then a for-check kept in another folder whose
    meta `archive` names this file: only when exactly one unused for-check claims it and, if that for-check recorded
    an archive_sha256, the sha matches. A same-folder file with another stem is never matched by name."""
    want = for_check_name(arch.name).lower()
    for fc in fcs:
        if id(fc) not in used and fc.path.parent == arch.parent and fc.path.name.lower() == want:
            return fc
    cands = [fc for fc in fcs if id(fc) not in used and fc.path.parent != arch.parent
             and (fc.meta.get("archive") or "").lower() == arch.name.lower()]
    if len(cands) != 1:
        return None
    sha = (cands[0].meta.get("archive_sha256") or "").strip().lower()
    if sha:
        try:
            if sha256_of(arch) != sha:
                return None
        except OSError:
            return None
    return cands[0]


# ---------------------------------------------------------------- archive-level checks

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


STALE_HINT = " (this for-check may belong to an older export of the same repo)"


def _older_than_archive(fc: ForCheck, archive: Path) -> bool:
    try:
        return archive.stat().st_mtime > fc.path.stat().st_mtime + 1
    except OSError:
        return False


def transfer_findings(fc: ForCheck, archive: Path):
    out, size = [], archive.stat().st_size
    hint = STALE_HINT if _older_than_archive(fc, archive) else ""
    want_sha = (fc.meta.get("archive_sha256") or "").strip().lower()
    if want_sha:
        got = sha256_of(archive)
        if got != want_sha:
            out.append(finding("FAIL", "transfer_integrity", f"sha256 of the archive is {got[:12]}..., downloader "
                               f"recorded {want_sha[:12]}...: changed after download{hint}", expected=want_sha, found=got,
                               item="sha256"))
    want_bytes = to_int(fc.meta.get("archive_bytes"))
    if want_bytes is not None and want_bytes != size:
        out.append(finding("FAIL", "transfer_integrity", f"archive is {size} bytes, downloader wrote {want_bytes}{hint}",
                           expected=want_bytes, found=size, item="bytes"))
    served = to_int(fc.meta.get("content_length"))
    if served is not None and served != size:
        out.append(finding("FAIL", "transfer_integrity", f"archive is {size} bytes, server reported {served}",
                           expected=served, found=size, item="content_length"))
    return out


def state_findings(fc: ForCheck):
    state = (fc.meta.get("state") or "").strip()
    if state and state not in GOOD_STATES:
        return [finding("FAIL", "export_state", f"export state is {state!r}, expected one of {sorted(GOOD_STATES)}",
                        expected="exported", found=state, item="state")]
    return []


def _bases(root: Path):
    """Extraction root first, then its single wrapper folder (if any)."""
    kids = [k for k in root.iterdir() if k.name not in SKIP_DIRS]
    return [root] + ([kids[0]] if len(kids) == 1 and kids[0].is_dir() else [])


def object_totals(bases) -> tuple[int, int]:
    """(count, bytes) of every file under repositories/*/*.git/objects/ in the extracted archive."""
    count = size = 0
    for base in bases:
        for objects in base.glob("repositories/*/*.git/objects"):
            for dirpath, _d, names in os.walk(objects):
                for n in names:
                    try:
                        st = os.lstat(os.path.join(dirpath, n))
                    except OSError:
                        continue
                    if stat.S_ISREG(st.st_mode):      # dangling or hostile symlinks are not objects
                        count += 1
                        size += st.st_size
        if count:
            break
    return count, size


def _inside(base: Path, rel: str):
    """base/rel, or None when the for-check's path is absolute or resolves outside the extraction root (a hostile or
    corrupt listing must not make Garmr stat files elsewhere on the host)."""
    if not rel or rel.startswith("/") or "\x00" in rel:
        return None
    p = base / rel
    try:
        p.resolve().relative_to(base.resolve())
    except (ValueError, OSError, RuntimeError):
        return None
    return p


def file_listing_findings(fc: ForCheck, archive: Path, extract_root: Path):
    if not archive.name.lower().endswith(TAR_SUFFIXES) or not extract_root.is_dir():
        return []
    bases, out = _bases(extract_root), []
    missing, resized = [], []
    for rel, size in sorted(fc.files.items()):
        found = next((c for b in bases for c in [_inside(b, rel)] if c is not None and c.is_file()), None)
        if found is None:
            missing.append(rel)
        elif size is not None and found.stat().st_size != size:
            resized.append(rel)
    if missing:
        out.append(finding("FAIL", "file_listing", f"{len(missing)} file(s) the source listed are not in the archive: "
                           f"{missing[:5]}", expected=len(fc.files), found=len(fc.files) - len(missing),
                           missing=capped(missing), item="missing"))
    if resized:
        out.append(finding("FAIL", "file_listing", f"{len(resized)} file(s) differ in size from the source listing: "
                           f"{resized[:5]}", missing=capped(resized), item="size"))
    want_n, want_b = fc.summary.get("objects_count"), fc.summary.get("objects_bytes")
    have_n, have_b = object_totals(bases)
    if (want_n is not None and have_n < want_n) or (want_b is not None and have_b < want_b):
        out.append(finding("FAIL", "file_listing", f"git objects in the archive: {have_n} files / {have_b} bytes; "
                           f"source listed {want_n} / {want_b}", expected=want_n, found=have_n, item="objects"))
    return out


def archive_findings(fc: ForCheck, archive: Path, extract_root: Path):
    return transfer_findings(fc, archive) + state_findings(fc) + file_listing_findings(fc, archive, extract_root)
