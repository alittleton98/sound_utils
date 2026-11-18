#!/usr/bin/env bash
# setup.sh - one-time setup for sfx_sort (Linux / macOS; on Windows run sfx_sort.py directly).
#
#   ./setup.sh --library /path/to/sound_library    set the library, check deps, dry run, offer the weekly job
#   ./setup.sh                                     same; asks for the library if none is saved yet
#   ./setup.sh --yes                               install the weekly job without asking
#   ./setup.sh --reset-config                      rewrite sfx_sort.toml with defaults (keeps the library path, backs up the old file)
#   ./setup.sh --uninstall                         remove the weekly job
#
# The library can also come from $SFX_LIBRARY. Schedule (default Sunday 03:30):
#   SFX_CRON="15 4 * * 1" ./setup.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/sfx_sort.py"
CONFIG="${SFX_SORT_CONFIG:-$HERE/sfx_sort.toml}"
CRON_SCHEDULE="${SFX_CRON:-30 3 * * 0}"
CRON_TAG="# sfx_sort weekly ($HERE)"
ASSUME_YES=0
RESET_CONFIG=0
LIBRARY="${SFX_LIBRARY:-}"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --library|-l)   [[ $# -ge 2 ]] || die "--library needs a path"; LIBRARY="$2"; shift ;;
    --library=*)    LIBRARY="${1#*=}" ;;
    --yes|-y)       ASSUME_YES=1 ;;
    --reset-config) RESET_CONFIG=1 ;;
    --uninstall)
      crontab -l 2>/dev/null | grep -vF "$CRON_TAG" | crontab - || true
      say "Weekly job removed."; exit 0 ;;
    -h|--help)      sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)              die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

[[ -f "$SCRIPT" ]] || die "sfx_sort.py not found next to setup.sh"

# --- 1. Python ---------------------------------------------------------------
say "Checking Python"
PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "Python 3 is not installed (e.g. sudo apt install python3 / brew install python)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || die "Python 3.8+ required, found $("$PY" -V 2>&1)"
echo "    $("$PY" -V) at $PY"

if ! "$PY" -c 'import tomllib' 2>/dev/null && ! "$PY" -c 'import tomli' 2>/dev/null; then
  say "Python is older than 3.11 - installing the 'tomli' TOML reader"
  if command -v apt-get >/dev/null && sudo -n true 2>/dev/null; then
    sudo apt-get install -y python3-tomli
  elif command -v dnf >/dev/null && sudo -n true 2>/dev/null; then
    sudo dnf install -y python3-tomli
  else
    "$PY" -m pip install --user tomli 2>/dev/null \
      || "$PY" -m pip install --user --break-system-packages tomli \
      || die "could not install tomli; run: $PY -m pip install --user tomli"
  fi
fi

# --- 2. Config and library ----------------------------------------------------
saved_library() {
  [[ -f "$CONFIG" ]] || return 0
  "$PY" - "$CONFIG" <<'EOF'
import sys
try: import tomllib
except ModuleNotFoundError: import tomli as tomllib
print(tomllib.load(open(sys.argv[1], "rb")).get("library", ""))
EOF
}

if [[ $RESET_CONFIG -eq 1 && -f "$CONFIG" ]]; then
  "$PY" "$SCRIPT" --config "$CONFIG" --init --force
elif [[ ! -f "$CONFIG" ]]; then
  "$PY" "$SCRIPT" --config "$CONFIG" --init
fi

if [[ -z "$LIBRARY" && -z "$(saved_library)" ]]; then
  [[ -t 0 ]] || die "no library folder set. Run: ./setup.sh --library /path/to/sound_library"
  read -r -e -p "Path to your SFX library folder: " LIBRARY
  [[ -n "$LIBRARY" ]] || die "no library folder given"
fi
if [[ -n "$LIBRARY" ]]; then
  LIBRARY="${LIBRARY/#\~/$HOME}"
  "$PY" "$SCRIPT" --config "$CONFIG" --set-library "$LIBRARY"
fi
LIBRARY="$(saved_library)"
say "Library: $LIBRARY"
[[ -d "$LIBRARY" ]] || die "library folder not found: $LIBRARY (is the drive mounted?)"
[[ -w "$LIBRARY" ]] || die "no write permission on $LIBRARY for user $(whoami)"

# Folders the sorter uses, read from the config
mapfile -t DIRS < <("$PY" - "$CONFIG" <<'EOF'
import sys
try: import tomllib
except ModuleNotFoundError: import tomli as tomllib
c = tomllib.load(open(sys.argv[1], "rb"))
print((c.get("inboxes") or ["_Inbox"])[0]); print(c.get("review_dir", "_Review")); print(c.get("duplicates_dir", "_Duplicates"))
EOF
)
for d in "${DIRS[@]}"; do
  if [[ ! -d "$LIBRARY/$d" ]]; then mkdir -p "$LIBRARY/$d"; echo "    created $d/"; fi
done
chmod +x "$SCRIPT"
LOGDIR="$HERE/logs"; mkdir -p "$LOGDIR"

probe="$LIBRARY/${DIRS[1]}/.sfx_sort_probe_$$"
touch "$probe" && mv "$probe" "$probe.moved" && rm -f "$probe.moved" \
  || die "could not create/move files in $LIBRARY (read-only mount?)"

# --- 3. Dry run ---------------------------------------------------------------
say "Dry run (nothing is moved) - full output in $LOGDIR/setup-dryrun.txt"
"$PY" "$SCRIPT" --config "$CONFIG" --dry-run > "$LOGDIR/setup-dryrun.txt" 2>&1 \
  || { cat "$LOGDIR/setup-dryrun.txt"; die "dry run failed"; }
grep -E '^(sfx_sort|  +[0-9]+  -> )' "$LOGDIR/setup-dryrun.txt" || true
echo "    (review the full list before the first real run)"

# --- 4. Weekly job ------------------------------------------------------------
command -v crontab >/dev/null || die "crontab not found (e.g. sudo apt install cron)"
q() { printf '%q' "$1"; }   # shell-quote paths for the crontab line
RUN="$(q "$PY") $(q "$SCRIPT") --config $(q "$CONFIG") >> $(q "$LOGDIR/cron.log") 2>&1"
command -v flock >/dev/null && RUN="$(command -v flock) -n $(q "$LOGDIR/.cron.lock") $RUN"
CRON_LINE="$CRON_SCHEDULE cd $(q "$HERE") && $RUN $CRON_TAG"

if [[ $ASSUME_YES -eq 0 ]]; then
  ans=""
  [[ -t 0 ]] && read -r -p "Install weekly job ($CRON_SCHEDULE) for user $(whoami)? [y/N] " ans || true
  [[ "$ans" =~ ^[Yy] ]] || { say "Skipped the weekly job. Run manually with: $SCRIPT"; exit 0; }
fi
{ crontab -l 2>/dev/null | grep -vF "$CRON_TAG" || true; echo "$CRON_LINE"; } | crontab -
say "Installed:"
crontab -l | grep -F "$CRON_TAG"

cat <<EOF

Done.
  Sort now:        $SCRIPT
  Preview:         $SCRIPT --dry-run
  Other library:   $SCRIPT --library /path/to/other_library --dry-run
  Undo a run:      $SCRIPT --undo $LOGDIR/moves-<timestamp>.tsv
  Run log:         $LOGDIR/cron.log
  Needs review:    $LOGDIR/review-latest.txt
  Remove the job:  $HERE/setup.sh --uninstall
EOF
