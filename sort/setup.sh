#!/usr/bin/env bash
# setup.sh - one-time setup for sfx_sort on a Linux server.
#
#   ./setup.sh                 check deps, create folders, show a dry run, offer to install the weekly job
#   ./setup.sh --yes           same, but install the weekly job without asking
#   ./setup.sh --uninstall     remove the weekly job
#
# Schedule (default Sunday 03:30) can be changed:  SFX_CRON="15 4 * * 1" ./setup.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/sfx_sort.py"
CONFIG="$HERE/sfx_sort.toml"
LOGDIR="$HERE/logs"
CRON_SCHEDULE="${SFX_CRON:-30 3 * * 0}"
CRON_TAG="# sfx_sort weekly"
ASSUME_YES=0

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

case "${1:-}" in
  --yes|-y) ASSUME_YES=1 ;;
  --uninstall)
    crontab -l 2>/dev/null | grep -vF "$CRON_TAG" | crontab - || true
    say "Weekly job removed."; exit 0 ;;
  "") ;;
  *) die "unknown option: $1" ;;
esac

[[ -f "$SCRIPT" ]] || die "sfx_sort.py not found next to setup.sh"
[[ -f "$CONFIG" ]] || die "sfx_sort.toml not found next to setup.sh"

# --- 1. Python ---------------------------------------------------------------
say "Checking Python"
PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || die "python3 is not installed (e.g. sudo apt install python3)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || die "Python 3.8+ required, found $("$PY" -V)"
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

# --- 2. Library and folders ---------------------------------------------------
LIBRARY="$("$PY" - "$CONFIG" <<'EOF'
import sys, os
try: import tomllib
except ModuleNotFoundError: import tomli as tomllib
cfg = tomllib.load(open(sys.argv[1], "rb"))
lib = cfg.get("library", "../sound_library")
print(os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), lib)))
for k in ("review_dir", "duplicates_dir"):
    print(cfg.get(k, ""))
print(cfg.get("inboxes", ["_Inbox"])[0])
EOF
)"
mapfile -t CFG_LINES <<<"$LIBRARY"
LIBRARY="${CFG_LINES[0]}"; REVIEW="${CFG_LINES[1]}"; DUPES="${CFG_LINES[2]}"; INBOX="${CFG_LINES[3]}"

say "Library: $LIBRARY"
[[ -d "$LIBRARY" ]] || die "library folder not found. Is the drive mounted? Edit 'library' in sfx_sort.toml if it lives elsewhere."
[[ -w "$LIBRARY" ]] || die "no write permission on $LIBRARY for user $(whoami)"

for d in "$INBOX" "$REVIEW" "$DUPES"; do
  if [[ ! -d "$LIBRARY/$d" ]]; then mkdir -p "$LIBRARY/$d"; echo "    created $d/"; fi
done
mkdir -p "$LOGDIR"
chmod +x "$SCRIPT"

# A quick sanity check that the filesystem supports moves the way we need
probe="$LIBRARY/$REVIEW/.sfx_sort_probe_$$"
touch "$probe" && mv "$probe" "$probe.moved" && rm -f "$probe.moved" \
  || die "could not create/move files in $LIBRARY (read-only mount?)"

# --- 3. Dry run ---------------------------------------------------------------
say "Dry run (nothing is moved) - full output in $LOGDIR/setup-dryrun.txt"
"$PY" "$SCRIPT" --dry-run > "$LOGDIR/setup-dryrun.txt" 2>&1 || { cat "$LOGDIR/setup-dryrun.txt"; die "dry run failed"; }
grep -E '^(sfx_sort|  +[0-9]+  -> )' "$LOGDIR/setup-dryrun.txt" || true
echo "    (review the full list before the first real run)"

# --- 4. Weekly job ------------------------------------------------------------
command -v crontab >/dev/null || die "crontab not found (e.g. sudo apt install cron)"
CRON_LINE="$CRON_SCHEDULE cd \"$HERE\" && /usr/bin/flock -n \"$HERE/.cron.lock\" \"$PY\" \"$SCRIPT\" >> \"$LOGDIR/cron.log\" 2>&1 $CRON_TAG"
command -v flock >/dev/null || CRON_LINE="${CRON_LINE//\/usr\/bin\/flock -n \"$HERE\/.cron.lock\" /}"

if [[ $ASSUME_YES -eq 0 ]]; then
  read -r -p "Install weekly job ($CRON_SCHEDULE) for user $(whoami)? [y/N] " ans || ans=""
  [[ "$ans" =~ ^[Yy] ]] || { say "Skipped. Run manually with: $SCRIPT"; exit 0; }
fi
{ crontab -l 2>/dev/null | grep -vF "$CRON_TAG" || true; echo "$CRON_LINE"; } | crontab -
say "Installed:"
crontab -l | grep -F "$CRON_TAG"

cat <<EOF

Done.
  Sort now:        $SCRIPT
  Preview:         $SCRIPT --dry-run
  Undo a run:      $SCRIPT --undo $LOGDIR/moves-<timestamp>.tsv
  Run log:         $LOGDIR/cron.log
  Needs review:    $LOGDIR/review-latest.txt
  Remove the job:  $HERE/setup.sh --uninstall
EOF
