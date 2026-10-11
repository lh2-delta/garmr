#!/usr/bin/env bash
# Run Garmr in Docker. Pre-checks the host, builds the image only when needed, returns Garmr's exit code unchanged.
#   ./run.sh              run (builds the image if it is missing or the sources changed since it was built)
#   ./run.sh --rebuild    rebuild the image first (also GARMR_REBUILD=1), e.g. after editing the code
set -euo pipefail
cd "$(dirname "$0")"

die() { echo "garmr: $*" >&2; exit 2; }

REBUILD="${GARMR_REBUILD:-0}"
for arg in "$@"; do
  case "$arg" in
    --rebuild) REBUILD=1 ;;
    *) die "unknown argument '$arg' (only --rebuild is supported)" ;;
  esac
done

IN="${INPUT_DIR:-./input}"; OUT="${OUTPUT_DIR:-./output}"; WORK="${WORK_DIR:-./work}"
IMAGE="${GARMR_IMAGE:-garmr:latest}"

command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is it running? are you in the 'docker' group?)"

[ -d "$IN" ] || die "input folder '$IN' does not exist: create it and put the repo .zip/.tar.gz files, their .for-check.csv files and the organisation's metrics .csv in it"
[ -n "$(ls -A "$IN")" ] || die "input folder '$IN' is empty: put the repo .zip/.tar.gz files, their .for-check.csv files and the organisation's metrics .csv in it"
# Create the writable folders ourselves, otherwise Docker creates them root-owned.
mkdir -p "$OUT" "$WORK" || die "cannot create output/work folders ('$OUT', '$WORK')"
[ -r "$IN" ] && [ -x "$IN" ] || die "input folder '$IN' is not readable by user $(id -un)"
[ -w "$OUT" ] || die "output folder '$OUT' is not writable by user $(id -un)"
[ -w "$WORK" ] || die "work folder '$WORK' is not writable by user $(id -un)"
# input is mounted read-only; the check above is for the user running the container (HOST_UID), which is this user.

HOST_UID="$(id -u)"; HOST_GID="$(id -g)"
export HOST_UID HOST_GID INPUT_DIR="$IN" OUTPUT_DIR="$OUT" WORK_DIR="$WORK" GARMR_IMAGE="$IMAGE"
if [ "$HOST_UID" -eq 0 ]; then
  echo "garmr: warning: running as root, so everything in '$OUT' will be root-owned" >&2
fi

# Fingerprint of everything baked into the image. A stale image (code edited since the last build) is rebuilt
# automatically, so a run never silently uses old checks.
GARMR_SRC_HASH="$(find Dockerfile docker-compose.yml check_export.py batch.py garmr vendor -type f ! -name '*.pyc' 2>/dev/null \
  | LC_ALL=C sort | xargs cat 2>/dev/null | sha256sum | cut -c1-16)"
export GARMR_SRC_HASH
HAVE_HASH="$(docker image inspect -f '{{ index .Config.Labels "garmr.src-hash" }}' "$IMAGE" 2>/dev/null || true)"

if [ "$REBUILD" = "1" ] || [ -z "$HAVE_HASH" ] || [ "$HAVE_HASH" != "$GARMR_SRC_HASH" ]; then
  if [ -n "$HAVE_HASH" ] && [ "$REBUILD" != "1" ]; then
    echo "garmr: sources changed since the image was built, rebuilding $IMAGE ..." >&2
  else
    echo "garmr: building image $IMAGE ..." >&2
  fi
  docker compose build || die "image build failed (offline? see the vendor/ note in README.md)"
fi

# GITHUB_TOKEN, LOC_TOLERANCE_PCT, GARMR_REQUIRE_FOR_CHECK, GARMR_GIT_TIMEOUT and GARMR_FSCK_TIMEOUT are passed
# through by docker-compose.yml (unset = empty/default). Values are never printed.
set +e
docker compose run --rm -T garmr
rc=$?
set -e

case "$rc" in
  0)   msg="all archives complete" ;;
  1)   msg="incomplete archives found (see output/missing_items.csv)" ;;
  2)   msg="usage/environment problem (input missing/empty, no archives, output/work not writable, low disk, scc missing, ...)" ;;
  3)   msg="internal crash (see the traceback above)" ;;
  130) msg="interrupted (SIGINT)" ;;
  143) msg="terminated (SIGTERM)" ;;
  *)   msg="unexpected exit code" ;;
esac
echo "garmr: exit $rc = $msg" >&2
exit "$rc"
