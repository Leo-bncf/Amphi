#!/usr/bin/env bash
# Installation d'Amphi Studio sur une carte ARM (Tinker Board RK3288, armhf).
#
# La carte NE transcrit PAS : elle sert l'interface, garde les notes et appelle
# les API. Whisper tourne sur le Mac de celui qui enregistre. C'est ce qui rend
# l'installation triviale — aucune dépendance Python à compiler.
#
#   sudo bash install.sh
set -euo pipefail

USER_NAME="${SUDO_USER:-$(id -un)}"
APP_DIR="/opt/amphi"
DATA_DIR="/var/lib/amphi"

echo "==> Vérifications"
command -v python3 >/dev/null || { echo "python3 manquant : apt install -y python3"; exit 1; }
python3 - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit(f"Python {sys.version_info.major}.{sys.version_info.minor} trop ancien, il faut 3.9+")
print(f"    Python {sys.version_info.major}.{sys.version_info.minor} OK")
PY
echo "    Architecture : $(dpkg --print-architecture 2>/dev/null || uname -m)"

echo "==> Hygiène carte SD"
# Les journaux systemd sont la première cause d'usure d'une carte SD. On les
# garde en RAM et on les borne : la carte ne subit plus qu'une écriture par
# sauvegarde de note, soit quelques centaines de Ko par séance.
install -d /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/amphi.conf <<'CONF'
[Journal]
Storage=volatile
RuntimeMaxUse=32M
CONF
systemctl restart systemd-journald || true
# Le swap sur carte SD la détruit et n'apporte rien avec 2 Go de RAM pour un
# serveur qui manipule des fichiers JSON.
if [ -f /etc/dphys-swapfile ]; then
  dphys-swapfile swapoff 2>/dev/null || true
  systemctl disable --now dphys-swapfile 2>/dev/null || true
  echo "    swap désactivé"
fi
echo "    journaux en RAM, 32 Mo max"

echo "==> Arborescence"
install -d -o "$USER_NAME" -g "$USER_NAME" "$APP_DIR" "$DATA_DIR"
echo "    code   : $APP_DIR"
echo "    donnees: $DATA_DIR"

echo "==> Service systemd"
cat > /etc/systemd/system/amphi-studio.service <<CONF
[Unit]
Description=Amphi Studio — notes de cours
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=AMPHI_DATA_DIR=$DATA_DIR
EnvironmentFile=-/etc/amphi.env
ExecStart=/usr/bin/python3 $APP_DIR/apps/studio/server.py
Restart=always
RestartSec=5
# La carte n'a que 2 Go : on borne pour qu'un emballement ne fasse pas tomber
# le reste du système.
MemoryMax=768M
# Durcissement : le service n'a besoin d'écrire que dans ses données.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
CONF
systemctl daemon-reload
echo "    unité écrite"

echo "==> Configuration"
if [ ! -f /etc/amphi.env ]; then
  cat > /etc/amphi.env <<'CONF'
# Clé Mistral — sans elle, ni notes ni lecture des photos.
MISTRAL_API_KEY=

# 127.0.0.1 tant qu'il n'y a pas d'authentification.
# Le tunnel Cloudflare se connecte en local, il n'a pas besoin d'exposition réseau.
AMPHI_HOST=127.0.0.1
PORT=8765
CONF
  chmod 600 /etc/amphi.env
  echo "    /etc/amphi.env créé — renseigne MISTRAL_API_KEY"
else
  echo "    /etc/amphi.env déjà présent, conservé"
fi

cat <<EOM

Installé. Il reste à :

  1. Copier le code depuis le Mac :
       rsync -av --exclude data --exclude .git --exclude bench/.venv \\
             ~/amphi/ $USER_NAME@$(hostname -I | awk '{print $1}'):$APP_DIR/

  2. Renseigner la clé :
       sudo nano /etc/amphi.env

  3. Démarrer :
       sudo systemctl enable --now amphi-studio
       systemctl status amphi-studio

  4. Vérifier :
       curl -s localhost:8765/health

N'EXPOSE PAS ce service sur Internet pour l'instant : il n'a aucune
authentification. Tant qu'elle n'existe pas, reste sur Tailscale.
EOM
