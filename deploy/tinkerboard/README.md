# Amphi sur Tinker Board

Héberger le studio sur la carte pour que la promo y accède. La carte sert
l'interface et garde les notes ; **elle ne transcrit pas** — le RK3288 fait
tourner Whisper à un cinquième du temps réel, la file ne se viderait jamais.
La transcription reste sur le Mac de celui qui enregistre.

## Matériel

| | |
|---|---|
| Carte | Asus Tinker Board, Rockchip RK3288, 4× Cortex-A17, 2 Go |
| Architecture | **armhf, 32 bits** — pas d'arm64 |
| Stockage | microSD (pas d'eMMC sur ce modèle) |
| Réseau | Ethernet gigabit natif |

**La carte SD de 64 Go suffit.** L'app écrit ~100 Mo par mois : ce qui tue les
cartes SD, ce sont les journaux système et le swap, que le script désactive.
Un SSD n'apporterait rien ici — l'USB est en 2.0 de toute façon.

## Système

**Armbian**, pas Raspberry Pi OS — Pi OS est spécifique aux puces Broadcom et
ne bootera pas. Prendre l'image *minimal / CLI* sur armbian.com/tinkerboard
(Debian 13 ou Ubuntu 26.04, noyau 6.18, builds maintenus).

```bash
# Depuis le Mac, carte insérée
diskutil list                      # repérer le disque, ex. /dev/disk4
diskutil unmountDisk /dev/disk4
sudo dd if=Armbian_*_Tinkerboard_*.img of=/dev/rdisk4 bs=4m status=progress
```

Premier démarrage en Ethernet, puis :

```bash
ssh root@<ip>                      # mot de passe initial : 1234
apt update && apt full-upgrade -y
apt install -y python3 rsync curl
```

## Dépendances

**Aucune.** Le serveur en mode hébergement n'utilise que la bibliothèque
standard de Python. Rien à compiler, ce qui compte sur un A17 en 32 bits.

## Installation

```bash
# Sur la carte
sudo bash install.sh

# Depuis le Mac
rsync -av --exclude data --exclude .git --exclude 'bench/.venv' \
      ~/amphi/ user@<ip>:/opt/amphi/

# Sur la carte
sudo nano /etc/amphi.env           # MISTRAL_API_KEY=...
sudo systemctl enable --now amphi-studio
curl -s localhost:8765/health
```

## Accès

**Tailscale d'abord** — la ligne est en CGNAT, aucun port-forwarding n'est
possible, et le service n'a pas d'authentification.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
```

Chaque camarade installe Tailscale et rejoint le tailnet : l'accès est alors
chiffré et limité aux appareils autorisés, ce qui remplace l'authentification
absente.

**Tunnel Cloudflare ensuite**, quand l'authentification existera :

```bash
# cloudflared en armhf 32 bits
curl -L -o cloudflared \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm
sudo install -m755 cloudflared /usr/local/bin/
cloudflared tunnel login
cloudflared tunnel create amphi
```

## Ce qui bloque encore

Le service **n'a aucune authentification**. Quiconque atteint l'URL peut lire
et supprimer toutes les notes. Tant que ce n'est pas fait :

- accès par Tailscale uniquement, jamais par tunnel public ;
- `AMPHI_HOST=127.0.0.1` dans `/etc/amphi.env`.

## Sauvegardes

Les notes vivent dans `/var/lib/amphi`, hors du code, pour qu'un `rsync` de
mise à jour ne les écrase pas. Une carte SD meurt sans prévenir :

```bash
# Sur le Mac, dans une tâche quotidienne
rsync -az user@<ip>:/var/lib/amphi/ ~/Backups/amphi/$(date +%F)/
```
