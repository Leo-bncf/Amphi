#!/usr/bin/env bash
# Restore an Amphi data snapshot without deleting destination data.
#
#   ./restore.sh [snapshot-dir]                 # dry-run (safe)
#   ./restore.sh --apply [snapshot-dir]         # copy into an empty/new target
#   ./restore.sh --apply --force [snapshot-dir] # merge, never delete files
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
DEFAULT_ROOT="${AMPHI_BACKUP_DIR:-$HOME/Backups/amphi}"
TARGET="${AMPHI_DATA_DIR:-$SCRIPT_DIR/../data/studio}"
APPLY=0
FORCE=0
SOURCE=""
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --force) FORCE=1 ;;
    -h|--help) printf '%s\n' "Usage: $0 [--apply] [--force] [snapshot-dir]"; exit 0 ;;
    *) SOURCE="$arg" ;;
  esac
done
if [ -z "$SOURCE" ]; then
  SOURCE=$(find "$DEFAULT_ROOT" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null | sort | tail -1 || true)
fi
[ -n "$SOURCE" ] && [ -d "$SOURCE" ] || { echo "Snapshot introuvable : $SOURCE" >&2; exit 1; }
[ -d "$SOURCE/audio" ] || [ -f "$SOURCE/schedule.json" ] || { echo "Snapshot Amphi invalide" >&2; exit 1; }
if [ "$APPLY" -eq 0 ]; then
  echo "Mode simulation : aucune donnée modifiée. Relancer avec --apply pour restaurer."
  rsync -a --dry-run --itemize-changes "$SOURCE/" "$TARGET/"
  exit 0
fi
if [ "$FORCE" -ne 1 ] && [ -d "$TARGET" ] && [ "$(find "$TARGET" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
  echo "Destination non vide : refus par sécurité (ajouter --force pour une fusion additive)." >&2
  exit 2
fi
mkdir -p "$TARGET"
# No --delete: files absent from the snapshot are deliberately preserved.
rsync -a --itemize-changes "$SOURCE/" "$TARGET/"
echo "Restauration terminée dans $TARGET (aucune suppression)."
