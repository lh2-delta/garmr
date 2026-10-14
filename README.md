# Garmr

Garmr checks that what `migration-downloader` produced is complete: everything the git history proves existed
(PRs, labels, objects, ...) must also be in the archive, nothing may be 0 bytes, and the numbers in the
organisation's own metrics CSV must not exceed what the archives hold.

## Run

```bash
mkdir input                       # put every repo .zip / .tar.gz, its <archive>.for-check.csv AND the org's metrics .csv in it
./run.sh                          # creates ./output and ./work if missing; builds the image the first time and automatically
                                  # again whenever the sources changed (it compares a fingerprint stored in the image)
./run.sh --rebuild                # force a rebuild; same as GARMR_REBUILD=1 ./run.sh
```

Use `./run.sh`, not bare `docker compose`: it checks the host first (docker installed, daemon reachable, input exists and is
not empty, input/output/work usable by you; each failure prints one message and exits 2), creates `output` and `work` so Docker
does not create them root-owned, passes your UID/GID to the container (as root you get a warning that outputs are root-owned),
runs `docker compose run --rm -T` (container removed afterwards, works without a TTY) and returns Garmr's exit code unchanged
with a one-line meaning, e.g. `garmr: exit 1 = incomplete archives found`.

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | all archives complete |
| 1 | any archive `INCOMPLETE` or `NO_ARCHIVE` |
| 2 | usage / environment: input missing or empty, zero archives, output/work not writable, low disk, an unparseable `LOC_TOLERANCE_PCT`, `scc` missing, disk full or permission error during extraction |
| 3 | internal crash |
| 130 / 143 | interrupted (SIGINT) / terminated (SIGTERM, e.g. `docker stop`) |

The container runs with `init: true` and `stop_grace_period: 60s`, so `docker stop` / Ctrl-C lets Garmr clean up its scratch
trees (130/143) instead of being SIGKILLed after 10 s. A stop request kills the running git / `tar` / `scc` child within about a
second (a 30-minute `fsck` does not delay it); the archive in progress is abandoned and listed as `NOT_CHECKED` in `run_notes.txt`.

**Folders and environment**

Folders live next to `docker-compose.yml` by default (`./input`, `./output`, `./work`, all gitignored). Elsewhere:
`INPUT_DIR=/big/in OUTPUT_DIR=/big/out WORK_DIR=/big/work ./run.sh`. Inside the container the paths are fixed:
`/data/input` (read-only), `/data/output`, `/data/work`. On an SELinux host (Fedora/RHEL) bind mounts need relabelling:
append `:z` to the three volume lines in `docker-compose.yml` (not added by default).

| Variable | Meaning |
|---|---|
| `GITHUB_TOKEN` | optional: also compare counts with the GitHub API (see below). A live check that cannot run is a FAIL (`live_check_failed`), not silence. Never printed |
| `LOC_TOLERANCE_PCT` | slack on the org-CSV `loc` comparison (default 0); a non-number or negative value exits 2 with a message |
| `GARMR_REQUIRE_FOR_CHECK` | `1`: an archive without a for-check fails (`for_check_missing`) instead of only being flagged `NO_FOR_CHECK` |
| `GARMR_GIT_TIMEOUT` | seconds allowed per git / `scc` call (default 1800); on expiry the check `timeout` fails (the history-forgery scan is the exception: its timeout is a `history_forgery` WARN, since it is a heuristic) |
| `GARMR_MAX_EXTRACT_BYTES` / `_MEMBERS` / `_RATIO` / `GARMR_EXTRACT_TIMEOUT` | per-archive extraction caps (default 200 GiB, 5,000,000 members, 200x expansion once past 1 GiB, 3600 s). An archive past a cap, or declaring more than the free disk, is a `FAIL archive_integrity` for that archive only; the run continues |
| `GARMR_MAX_JSON_BYTES` | a metadata `.json` over this size (default 4 GiB) is a `FAIL metadata_json` and is not loaded |
| `GARMR_FSCK_TIMEOUT` | seconds allowed for `git fsck --full` (default 3600) |
| `HOST_UID` / `HOST_GID` | user the container runs as; set by `run.sh` |
| `INPUT_DIR` / `OUTPUT_DIR` / `WORK_DIR` | host folders (above) |
| `GARMR_IMAGE` | image tag (default `garmr:latest`) |
| `GARMR_SCRATCH` | internal: where LOC temp files go (inside `work/`); do not set |

**Disk.** `work/` needs roughly **3.2x the largest single tarball** (a zip bundle is processed one member at a time): the
extracted tree (about 1.2x), plus a bare clone of a GitLab bundle and the LOC checkout of HEAD, which are about one archive each
and exist at the same time. LOC temp files also live in `work/` (`GARMR_SCRATCH`). Scratch dirs are `work/garmr-*`; leftovers from a crashed run are
removed at the next start. A run with too little free space exits 2 before extracting.

**`scc` is required.** It measures lines of code for the org-CSV LOC floor, and a run without it would skip that check without
saying so and look more complete than it is. So the image build **fails** if `scc` cannot be installed (download retried 5 times),
and Garmr exits 2 and writes nothing if `scc` is not on `PATH`. Offline or flaky network: put `scc_Linux_x86_64.tar.gz` from
<https://github.com/boyter/scc/releases> in `./vendor/` (a copy is already there); the build uses it instead of downloading.
The apt step retries 3 times with a 30 s timeout, so an offline build fails in about a minute. The base image `python:3.12-slim`
is not pinned by digest; pinning it (`FROM python:3.12-slim@sha256:...`) is a manual hardening step.

**Inputs.** Either loose files: `<name>.tar.gz` and `<name>.for-check.csv` side by side in `./input`; or the downloader's
**"Download all (.zip)"** bundle dropped in as it is: when a `.zip` contains tarballs, Garmr opens it and checks each tarball
(one at a time) against the `<name>.for-check.csv` that sits beside it *inside the zip* (report rows show the archive as
`bundle.zip!name.tar.gz`). A tarball with no for-check next to it is still checked and flagged `NO_FOR_CHECK`; a for-check
whose tarball is not in the zip is a `NO_ARCHIVE` row. A `for-check.csv` packed *inside* a `.tar.gz` is not read: it records
that archive's own hash and file list, so it cannot live in it. Extract it next to the tarball instead.

## Outputs (`./output`)

All files are created mode `0600` (owner only), since they list repo names and what is missing.

| File | Contents |
|---|---|
| `completeness_report.csv` | one row per repo: `COMPLETE` / `INCOMPLETE` / `NO_ARCHIVE`, provider (auto-detected), `warned_checks` (names of the WARN checks that fired, `;`-separated, e.g. `history_forgery;lfs`; `warnings` is their count), `failed_checks`, `gaps` (information only, see "Gaps column" below), `for_check` (`yes` / `no` / `unreadable` / `empty`), counts found in the archive, the org's numbers, `flags`. `visibility`: `public` / `private` / `internal` / `unknown`, read from the repository record (GitHub `private`, GitLab `visibility` / `visibility_level` 20 / 10 / 0, Bitbucket `is_private`); `unknown` when the record lacks it, is unreadable or the layout is plain git, never guessed. `prs_merged` / `prs_unmerged`: counts of distinct PR (GitHub) / MR (GitLab) records in the metadata. merged = non-empty `merged_at` (or state `merged`); unmerged = every other record, closed without a merge and still open alike (one combined count, no separate "open"). `state` is only the fallback (GitHub export records carry none). A number with several records counts once, as merged if any of them is. The two are blank (not 0) when the export carries no PR/MR records, the layout has none (plain git), or a record has none of `merged_at` / `closed_at` / `state`. `prs_git` and `prs_meta` are unchanged. `extra_metrics` (what the archive holds more of than the reference, e.g. `prs_git:+3;commits:+12`) and `history_tags` (`imported`, `forked`, `suspected_forged`, `flattened`, `rewritten`, `faulty`) are described under "Org metrics CSV" below |
| `missing_items.csv` | only errors: one row per failed check with `expected`, `found`, `missing_count`, `missing_items` (e.g. PR numbers in git but with no metadata record). `missing_items` lists at most 200 examples; `missing_count` is always the real total |
| `run_notes.txt` | non-fatal notices: CSVs skipped or unused, `.xlsx` files not read, archives that were not checked |

While running, `*.partial` files grow one archive at a time and survive a crash, so a long run is not lost. The final
`completeness_report.csv` and `missing_items.csv` are replaced atomically at the end, and stale outputs from the previous run are
removed at start, so a file in `output/` always belongs to the run that wrote it.

### Gaps column

`completeness_report.csv` has a `gaps` column right after `failed_checks`: `;`-separated `<check>=<missing>/<expected>` for each
FAILED `commits` and `pull_requests` check, e.g. `commits=3/120;pull_requests=1/40`. The numbers are the `missing_count` and
`expected` of that check's row in `missing_items.csv`, so the two files always agree. For `commits`, expected is the number of
distinct merge commits the metadata references and missing is how many of them are absent from the archive's git. An entry is
written only when both numbers are known integers and expected > 0; the column is blank when neither check failed, when the
failure is of another kind (`archive_integrity`, `org_report_mismatch`, ...), and on `NO_ARCHIVE` rows. WARN-only checks are never
listed. It is information only: Garmr stays strict and `status` / `failed_checks` ignore it. Squirrel reads it to apply its own 5%
tolerance to these count-based failures; the tolerance is not applied here.

### Statuses and flags

`COMPLETE` (all checks passed), `INCOMPLETE` (a check failed), `NO_ARCHIVE` (a for-check or org-CSV row has no archive). Flags in
the `flags` column are notices, not failures:

| Flag | Meaning |
|---|---|
| `NO_FOR_CHECK` | archive has no for-check; only offline checks ran |
| `FOR_CHECK_UNKNOWN:<entity>` | that count is blank in both for-check snapshots: not compared |
| `KIND_ABSENT:<kind>` | the archive has no file of this kind (e.g. no `labels_*.json`) |
| `MULTI_ROW` | several org-CSV rows (one per branch) for the repo were collapsed by max |
| `ORG_VALUE_UNPARSEABLE:<metric>` | the org-CSV cell is not a number; not compared |
| `ORG_LOWER:<metric>` | the archive holds more than the org CSV says (allowed; also an `extra_metrics` WARN and a column entry) |
| `LOC_BRANCH_DIFF` | `loc` differs, possibly because CSV and archive use different branches |
| `NOT_IN_ORG_CSV` | archive's repo has no org-CSV row |
| `AMBIGUOUS_ORG_ROW` | more than one org-CSV row could match; not guessed, so no org number is compared for this repo |
| `LOC_UNMEASURED` | the org CSV has a `loc` but Garmr could not measure the archive's lines of code (see the `loc_unmeasured` WARN); not compared |
| `ORG_UNCOMPARED:<metric>` | the org CSV has this metric but the archive's value is unknown (e.g. `prs` for a plain git repo): not compared |

## What is checked

Principle: **git is the ground truth, and whatever metadata exists must itself be complete.** If git history shows
something, the archive must hold its metadata. Metadata that git cannot show (closed-unmerged PRs, MRs, comments, labels,
issues) is not ignored: it is checked for completeness too.

Provider is auto-detected per archive: `github-migration` (`schema.json`/`repositories_*.json`), `gitlab-export`
(`project.json` + `repo.bundle`), or a plain git repo.

**Archive and files**

| Check | Fails when |
|---|---|
| `archive_integrity` | tar/zip truncated or CRC error |
| `zero_byte` | any 0-byte file (allowlist `.gitkeep`, `.keep`, `.nojekyll`, `__init__.py`, `packed-refs` -> warning) |
| `required_file` | `schema.json`, `repositories_*.json` or `users_*.json` absent |
| `page_sequence` / `page_partial` | a numbered file is missing from `kind_000001..N.json`, or a non-final page holds fewer records than the others (truncated) |
| `file_refs` | a file the metadata points at (`tarball://root/...`, GitLab `/uploads/...`, LFS objects) is not in the archive |

**Git is intact and shows nothing that is absent from the metadata**

| Check | Fails when |
|---|---|
| `git_integrity` | `git fsck` finds a missing object, repo is shallow, or has no refs |
| `pull_requests` / `merge_requests` | a PR/MR in git (`refs/pull/*`, `refs/merge-requests/*`, else merge-commit messages) has no record |
| `number_refs` | a commit says `closes/fixes/resolves #N` or merges/squashes `#N` but no issue/PR record N exists (plain `#N` mentions are ignored) |
| `pr_state` | git has `Merge pull request #N` but the PR record has no `merged_at`. Only a WARN when the merge commit predates the repo's `created_at`, or names a head branch (`from owner/branch`) other than the PR record's `head.ref`: after an import the numbers belong to the repo's earlier life and collide with this repo's own PRs |
| `git_refs` | a PR record has no `refs/pull/N/head` in the archive's git |
| `release_tag` / `default_branch` | a release's tag, or the repo's default branch, is not in the archive's git |
| `commits` | a commit sha in the metadata (merge, event, comment commit) is not in the archive's git (PR base/head shas only warn) |

**Metadata is complete, whether or not git shows it**

| Check | Fails when |
|---|---|
| `record_incomplete` | a record lacks fields its kind requires (e.g. a PR with no `title` / `head.sha`), or lacks a field its peers all have. Peers are the records of the same sub-kind: issue events are grouped by `event` and attachments by which parent key they carry, so fields legitimately specific to one type (e.g. `commit_repository` on `referenced` events) are not flagged. `missing_count` is the real number of fields |
| `dangling_reference` | a user a record points at is not in `users_*.json` (missing fork repos only warn) |
| `labels` / `milestones` / `comments_parent` / `events_parent` | an issue/PR references a label/milestone that was not exported, or a comment/event points at an issue/PR with no record |
| `repo_git` | repo listed in metadata but its `.git` is missing |

**Organisation metrics CSV (lower bound)**

The CSV in `./input` is the summary output of codebase-profiler / repo-metrics-v2. It is a floor: the archive must hold at
least what it lists. `org_report_mismatch` fails when the CSV number is **greater** than the archive's (commits compared
with commits across all refs; loc is `scc` code lines on the measured branch; also prs, branches, tags, files).

**Measured branch.** Garmr measures on the branch (`refs/heads/*`) whose tip commit is newest, the rule the profiler uses; a tie
goes to the default branch, then to the first name. LOC, `files` and the LFS scan use that branch, and the report says which
in `measured_branch` and counts its history in `commits_measured`. `commits_head` stays on the **default** branch, because the
for-check's commit count is the platform's default-branch history and must be compared like with like. With no branch at all,
or with `GARMR_USE_DEFAULT_BRANCH=1`, everything stays on the default branch as before.

**Client's branch.** `GARMR_BRANCH_MAP` may name a JSON file `{"org/repo" or "repo": "branch"}` (keys case-insensitive, full label
tried before the bare name). A mapped branch that exists in the archive wins over the newest rule and over
`GARMR_USE_DEFAULT_BRANCH`. A missing branch, unreadable file or malformed file is ignored and the normal rule applies. The report's
`measured_by` says which rule chose: `client`, `newest` or `default`; compare `measured_branch` with the branch you asked for.

**PRs** are looked for twice: the CSV's merged-PR count must be reached by the PR numbers git history names (`prs_git`) and,
separately, by the merged PR records in the metadata (`prs_merged`; all PR records, `prs_meta`, when the export carries no
merged_at/closed_at/state). A side that falls short makes one `org_report_mismatch` finding (item `prs`) naming the side(s); a
side that cannot be counted is flagged `ORG_UNCOMPARED:prs_git` / `ORG_UNCOMPARED:prs_metadata`, never judged.

**Extras.** A CSV number below what the archive holds is not a failure: it is an **"Extra <metric> detected"** WARN (check
`extra_metrics`, so it appears in `warned_checks`; for PRs: "Extra PRs detected: 3 more PRs in git history than the
reference's 5"), the amount goes into the `extra_metrics` column (`prs_git:+3;prs_metadata:+1;commits:+12`, empty when there is
none) and the old `ORG_LOWER:<metric>` flag is still set. Unmerged or imported PRs usually explain extra PR numbers. Commits are
extra only when the default branch alone holds more (other branches may legitimately hold more); loc applies the tolerance first.

**The for-check outranks this CSV.** A `.for-check.csv` holds the source platform's own counts taken before and after the
export, so for every metric it bounds (`prs`, `commits`, `branches`, `tags`) it is the reference and the org CSV (the
profiler re-run or the client's summary) is not compared for that metric: the report flags `ORG_SUPERSEDED:<metric>` and the
`source` count check (`live_counts`: below the lower bound fails, above the upper bound warns) decides. Whatever the archive
holds above the for-check's upper bound is an extra and goes into `extra_metrics` too (`prs_metadata:+2`). Metrics the
for-check does not cover (`loc`, `files`) and archives without a for-check keep using the org CSV. Precedence, strongest first:
for-check, live API, org CSV.

**`history_tags`** (column, `;`-separated, stable order) names what is odd about the history itself. They are labels for a
person, built from signals Garmr already computes; suspected, never proof, and they change no verdict.

| Tag | Set when |
|---|---|
| `imported` | the first commit is older than the repository's `created_at` (`created_after_history`), or merge commits carry PR numbers that belong to the repo's earlier life (the `pr_state` imported-numbers WARN) |
| `forked` | the repository record says it is a fork |
| `suspected_forged` | a `history_forgery` signal: future-dated commits, author date after committer date, a child older than its parent, bulk same-second commits, one committer over many authors, a BAD signature |
| `flattened` | one squashed commit, or several root commits |
| `rewritten` | reflog rewrites or unreachable commits, or the platform recorded `base_ref_force_pushed` events |
| `faulty` | a failed `git_integrity`, `repo_git` or `archive_integrity` check (missing or corrupt objects, shallow, no refs, truncated archive) |

### Source-side ground truth: `<archive>.for-check.csv`

`migration-downloader` writes `<archive stem>.for-check.csv` next to each archive (`acme-widgets-42.tar.gz` +
`acme-widgets-42.for-check.csv`), recording what the source said before, during and after the export. Garmr pairs them
by that name (also by the `archive` meta row when the for-check sits in a different folder than the archive; a
same-folder file with a different stem is not paired and shows up as `NO_ARCHIVE`). `*.for-check.csv` is never taken for
the org metrics CSV.

Format: columns `repo,section,key,value`; a blank value means unknown and is never guessed or failed on. Sections: `meta`,
`requested_repo`, `export_repo`, `counts_before` / `counts_after`, `ref_before` / `ref_after`, `file`, `file_summary`, `error`.

`for_check` column and flags (in `flags`):

| Value / flag | Meaning |
|---|---|
| `for_check=yes` / `no` / `unreadable` / `empty` | a for-check was found and parsed / none for this archive / found but could not be parsed / found but has no rows |
| `NO_FOR_CHECK` | archive has no for-check; only the offline checks ran |
| `FOR_CHECK_UNREADABLE` | the file could not be parsed (wrong header, bad encoding); nothing crashes, no for-check checks run |
| `FOR_CHECK_EMPTY` | the file is empty or has a header but no rows; nothing was compared (status `empty` in the `for_check` column) |
| `FOR_CHECK_UNKNOWN:<entity>` | that count is blank in both snapshots: not compared, but flagged |

A for-check with no matching archive gives a `NO_ARCHIVE` row (`for_check=yes`) and an `archive_missing` row in `missing_items.csv`.

New checks (all FAIL unless noted; `item` is the value in `missing_items.csv`):

| Check / item | Fails when |
|---|---|
| `transfer_integrity` / `sha256` | sha256 of the archive file differs from meta `archive_sha256` (streamed) |
| `transfer_integrity` / `bytes` | file size differs from meta `archive_bytes` |
| `transfer_integrity` / `content_length` | meta `content_length` (what the server reported) differs from the file size |
| `export_state` | meta `state` is set and not `exported` / `finished` |
| `file_listing` / `missing` | a `file` row's path is not in the extracted archive (paths listed in `missing_items`) |
| `file_listing` / `size` | the path exists but its size differs |
| `file_listing` / `objects` | git object files under `repositories/*/*.git/objects/` are fewer (count or bytes) than `file_summary` says |
| `repo_not_exported` / `requested` | a `requested_repo` is absent from the `export_repo` rows (only when those exist) |
| `repo_not_exported` / `exported` | an `export_repo` has no `repositories/<owner>/<name>.git` in the archive (GitHub layout, case-insensitive) |
| `live_counts` / `<entity>` | the archive holds fewer than the lower bound (see below); `missing_count` is the shortfall. More than the upper bound only warns. Same check name as the live API comparison |
| `refs` | a ref on the source both before and after the export is not in the archive's git. Same ref at another sha only warns |
| `default_branch` / `source` (WARN) | source default branch differs from the archive's repository record |
| `visibility_mismatch` (WARN) | the archive record's visibility is known and a `visibility` count row of the for-check (`counts_before` / `counts_after`; value `public` / `private` / `internal`), or the GitHub API with `GITHUB_TOKEN`, states a different one; `item` names the source (`for-check` / `live API`). Informational: a missing value on either side is skipped silently |

`file_listing` runs only for `.tar.gz` / `.tgz` / `.tar`, against the extraction root (a single wrapper folder is tolerated);
extra files in the archive are fine. GitHub compares `refs/heads/*`, `refs/tags/*`, `refs/pull/N/head`; GitLab only heads and
tags. GitLab count comparison covers issues, MRs, labels, milestones, releases, branches, tags, commits.

**How the snapshots avoid false alarms.** Counts are taken just before the export started and just after it finished. For each
repo and entity the bounds are (min, max) of the two known values; one known value gives (v, v); none gives skip + flag. The
archive must hold at least the minimum: an item deleted mid-export (5 before, 3 after) is not missing, and an item created
mid-export (higher after) is not required. Refs are required only if they existed at both snapshots. If a for-check and a live
API answer (`GITHUB_TOKEN`) both exist for an entity, the for-check wins. A unit is matched to for-check rows by repo label
(case-insensitive); with exactly one repo on each side they match whatever the label.

**Optional, with `GITHUB_TOKEN`:** `live_counts` compares each entity (PRs, issues, labels, milestones, releases, comments,
branches, tags, collaborators, commits) with the GitHub API. API higher than the archive fails with the number missing; archive
higher only warns. This is the only way to catch a file that is complete and well-formed but lacks entries nothing else
references (for example 3 of 5 labels where no issue uses the other 2).

Further checks, all FAIL unless noted (`item` is the value in `missing_items.csv`):

| Check | Fails when |
|---|---|
| `for_check_unusable` | the for-check exists but cannot be parsed or used |
| `for_check_missing` | archive has no for-check and `GARMR_REQUIRE_FOR_CHECK=1` |
| `for_check_unmatched` | a for-check row set matches no archive/repo |
| `live_check_failed` | `GITHUB_TOKEN` is set but the API comparison could not be completed, or the API would not count one entity (403/404/rate limit): one finding per entity in `item` (WARN for `collaborators`, which needs push access) |
| `repo_unlisted` | a repository is in the archive but not in its repository metadata (or the reverse) |
| `kind_missing` | a metadata kind that must exist is absent. For `pull_requests`, a WARN instead of a FAIL when the only evidence is PR numbers in commit messages and the history is imported (first commit older than the repo's `created_at`, or a fork) |
| `timeout` | a git / `scc` / fsck call exceeded `GARMR_GIT_TIMEOUT` / `GARMR_FSCK_TIMEOUT` (except the history-forgery scan: WARN) |
| `archive_integrity` | the archive does not read, would expand past an extraction cap or the free disk, or nests archives more than 3 levels deep |
| `git_integrity` | also when a git read that must succeed (`rev-list`, `for-each-ref`...) fails after fsck passed: the repo is reported not verified, never as having nothing to report |
| `loc_unmeasured` (WARN) | lines of code could not be measured (no `scc`, tree unreadable, `scc` output unusable) |
| `analysis_error` | a check crashed on this archive; the archive is reported INCOMPLETE rather than skipped |
| `metadata_json` | a metadata `.json` file is not valid JSON |
| `unsafe_member` (WARN) | archive member with an absolute path or `..` (tar or zip) was not extracted |

Git is verified with `git fsck --full` (object content, not just connectivity), with the timeouts above.

Warnings (never fail a repo): `ORG_LOWER`, `NOT_IN_ORG_CSV`, LFS pointers whose objects GitHub never exports, issue/PR number gaps,
`unsafe_member`, collaborators count differences (and skipped entirely when the archive has no collaborators key).

### Organisation CSV format

Any `*.csv` under `./input` (subfolders included, except `*.for-check.csv`) with a repo identity column and at least one metric
column is used; several files are merged. Delimiter and encoding are auto-detected. Identity columns: `repo`, `repo_name`, `name`,
`full_name`, ... then `project_name` as a fallback; metric aliases: `total_commits`, `loc`, `merged_prs`, `total_files`, `branches`,
`tags`, ...; an `org`/`owner` column disambiguates, and rows of a different org never match. Repos are matched by `owner/name`,
`owner-name`, `name` or the archive file name. Duplicate rows for a repo (one per branch) are collapsed by max (`MULTI_ROW`). Repos
listed with no archive get `NO_ARCHIVE` plus an `archive_missing` row. **`.xlsx` files are not read** (a loud warning in the
output and `run_notes.txt`): export to CSV. `LOC_TOLERANCE_PCT` allows slack on `loc`.

The CSV is a **lower bound**: an archive below the CSV is an error (`org_report_mismatch`), an archive above it is only flagged.

## Tests

```bash
python3 -m pytest -q
```

## What Garmr cannot know / limits

- **Not calibrated on real exports.** The GitLab layout is detected as `project.json` at the root or in `tree/`, plus
  `repo.bundle` or `project.bundle`, and has only been tried on synthetic fixtures. The GitHub layout is not officially documented;
  the checks follow GitHub migration tarballs as observed. The for-check path was tested on synthetic fixtures only.
- GitHub metadata `.json` files are parsed whole (memory is several times the file size, so `GARMR_MAX_JSON_BYTES` bounds it); GitLab
  `.ndjson` is read line by line, but its parsed records are held. A repo with tens of millions of records needs a big host.
- Commit-message markers (`Merge pull request #N`, `(#N)`, `closes #N`, GitLab `See merge request path!N`) prove a PR existed in
  *this* repo only when the history is its own: for a fork or a repo whose history is older than its `created_at` they are only
  a WARN (`pull_requests` / `number_refs`). A GitLab MR line naming another project is ignored.
- LOC is counted on a checkout of the **measured branch** (see below) from the object store, with `.gitignore`, `.ignore`, `.sccignore` and
  `export-ignore` all disregarded, so tracked-but-ignored files count.
- **Nothing has been validated on large (multi-GB) archives**: timings, disk needs and timeouts are untested at that scale.
- **The first real runs may produce false positives.** Send the `missing_items.csv` rows and the checks can be tuned.
- A well-formed file that lacks entries nothing else references (3 of 5 labels where no issue uses the other 2) can only be
  caught with a for-check or `GITHUB_TOKEN`.
- Collaborators comparison is warn-only, and skipped when the archive has no collaborators key.
- Platform counts and export counts can be defined differently (what counts as a PR, a comment, a branch), so a small mismatch
  is not always a lost item.
- Issues/PRs deleted at the source look like gaps in the number sequence (warning only).
- Docker: the base image is not pinned by digest (manual hardening step); the image is minimal, with no HEALTHCHECK.

## History-forgery signals (`history_forgery`, always WARN)

Heuristics, not proof; a person reads them. Thresholds are named constants in `check_export.py`.
Future-dated commits, author date after committer date, child older than parent, bulk same-second committer time,
one committer over many authors, multiple roots, single squashed commit, unreachable commits, reflog rewrites,
BAD gpg signature (only when the signer's key is in the keyring; unverifiable signatures are not flagged), and
repo `created_at` newer than the first commit. Not covered: ref/commit-count upper bounds still only WARN.
