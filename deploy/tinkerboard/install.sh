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

# pypdf est du Python pur : il s'installe sur la carte ARM sans rien compiler.
# Sans lui le serveur démarre et tout marche, sauf le dépôt d'un PDF.
if python3 -c "import pypdf" 2>/dev/null; then
  echo "    pypdf OK (lecture des PDF)"
else
  echo "    pypdf absent — installation"
  python3 -m pip install --quiet pypdf 2>/dev/null \
    || apt-get install -y python3-pypdf 2>/dev/null \
    || echo "    ATTENTION : pypdf non installé, le dépôt de PDF renverra une erreur explicite"
fi

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

# Mot de passe partagé par les apps. Le serveur refuse une écoute publique sans
# cette valeur ; le tunnel Cloudflare conserve en plus HTTPS jusqu'au client.
AMPHI_PASSWORD=
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

  2. Renseigner MISTRAL_API_KEY et AMPHI_PASSWORD :
       sudo nano /etc/amphi.env

  3. Démarrer :
       sudo systemctl enable --now amphi-studio
       systemctl status amphi-studio

  4. Vérifier :
       curl -s localhost:8765/health

Le tunnel Cloudflare est l'unique accès Internet prévu. Ne publie jamais le
port 8765 directement sur le routeur et ne laisse pas AMPHI_PASSWORD vide.
EOM
