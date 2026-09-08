#!/usr/bin/env bash
# Sauvegarde des notes Amphi.
#
# Une seule copie sur une seule machine, c'est une panne de disque loin de tout
# perdre. Ce script fait une copie datée, garde les quatorze dernières et
# n'écrase jamais une copie existante.
#
#   ./backup.sh                       # vers ~/Backups/amphi
#   ./backup.sh /Volumes/Cle/amphi    # vers un disque externe
#   ./backup.sh user@carte:/srv/bk    # vers une autre machine (rsync/ssh)
set -euo pipefail

SRC="${AMPHI_DATA_DIR:-$(cd "$(dirname "$0")/.." && pwd)/data/studio}"
DEST="${1:-$HOME/Backups/amphi}"
KEEP=14
STAMP=$(date +%Y-%m-%d_%H%M)

[ -d "$SRC" ] || { echo "Source introuvable : $SRC"; exit 1; }
notes=$(find "$SRC" -maxdepth 1 -name '*.json' | wc -l | tr -d ' ')
echo "Source : $SRC ($notes séance(s), $(du -sh "$SRC" | cut -f1))"

if [[ "$DEST" == *:* ]]; then
  echo "Destination distante : $DEST/$STAMP"
  rsync -az --delete "$SRC/" "$DEST/$STAMP/"
else
  mkdir -p "$DEST"
  # --link-dest : les fichiers inchangés sont des liens durs vers la copie
  # précédente. Quatorze copies coûtent donc à peine plus qu'une seule.
  PREV=$(ls -1d "$DEST"/*/ 2>/dev/null | tail -1 || true)
  if [ -n "$PREV" ]; then
    rsync -a --delete --link-dest="$PREV" "$SRC/" "$DEST/$STAMP/"
  else
    rsync -a "$SRC/" "$DEST/$STAMP/"
  fi
  cd "$DEST"
  # `head -n -N` n'existe pas sur BSD/macOS : on compte puis on coupe.
  total=$(ls -1d */ 2>/dev/null | wc -l | tr -d ' ')
  if [ "$total" -gt "$KEEP" ]; then
    ls -1d */ | sort | head -n $((total - KEEP)) | while read -r old; do rm -rf "$old"; done
  fi
  echo "Copies conservées : $(ls -1d */ 2>/dev/null | wc -l | tr -d ' ') (max $KEEP)"
  echo "Poids total       : $(du -sh "$DEST" | cut -f1)"
fi
echo "Sauvegarde $STAMP terminée."
