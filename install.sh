#!/usr/bin/env bash
# Install the editable Python source tree on Debian 13 (no .deb).
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    printf 'Droits root requis.\nRelance avec : sudo bash %s\n' "$0" >&2
    exit 1
fi
if [[ ! -f /etc/os-release ]]; then
    echo 'Erreur : distribution Linux non identifiée.' >&2
    exit 1
fi
# The installer is for Debian; use on Debian 13 in production.
. /etc/os-release
if [[ "${ID:-}" != 'debian' ]]; then
    echo 'Erreur : cette installation cible Debian.' >&2
    exit 1
fi
if dpkg-query -W -f='${Status}' serverpanel 2>/dev/null | grep -qx 'install ok installed'; then
    echo 'Erreur : le paquet .deb ServerPanel est installé. Ne mélangez pas les installations.' >&2
    exit 1
fi
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
for file in "$SOURCE/serverpanel/cli.py" "$SOURCE/serverpanel/ui.py" "$SOURCE/packaging/config.json"; do
    [[ -f "$file" ]] || { echo "Fichier manquant : $file" >&2; exit 1; }
done
TARGET=/opt/serverpanel
LAUNCHER=/usr/local/bin/panel
MOTD=/etc/update-motd.d/85-serverpanel

if [[ -e "$LAUNCHER" ]] && ! grep -qF '# ServerPanel: source install' "$LAUNCHER"; then
    echo "Erreur : $LAUNCHER existe déjà et n'appartient pas à cette installation." >&2
    exit 1
fi
if [[ -e "$MOTD" ]] && ! grep -qF '# ServerPanel: source install' "$MOTD"; then
    echo "Erreur : $MOTD existe déjà et n'appartient pas à cette installation." >&2
    exit 1
fi
printf '\nInstallation des dépendances via APT...\n'
export DEBIAN_FRONTEND=noninteractive
apt-get install -y python3 apache2 certbot python3-certbot-apache

install -d -m 0755 "$TARGET/serverpanel" /etc/serverpanel /usr/local/bin /etc/update-motd.d
for file in "$SOURCE"/serverpanel/*.py; do
    install -m 0644 "$file" "$TARGET/serverpanel/$(basename "$file")"
done
for file in README.md SECURITY.md VALIDATION.md LICENSE; do
    if [[ -f "$SOURCE/$file" ]]; then
        install -m 0644 "$SOURCE/$file" "$TARGET/$file"
    fi
done
if [[ ! -e /etc/serverpanel/config.json ]]; then
    install -m 0644 "$SOURCE/packaging/config.json" /etc/serverpanel/config.json
fi
cat > "$LAUNCHER" <<'EOLAUNCH'
#!/bin/sh
# ServerPanel: source install
PYTHONPATH=/opt/serverpanel exec /usr/bin/python3 -m serverpanel "$@"
EOLAUNCH
chmod 0755 "$LAUNCHER"
cat > "$MOTD" <<'EOMOTD'
#!/bin/sh
# ServerPanel: source install
[ -x /usr/local/bin/panel ] || exit 0
exec /usr/local/bin/panel motd
EOMOTD
chmod 0755 "$MOTD"
printf '\nServerPanel installé depuis les sources.\nCommande : sudo panel\nSources Python : %s/serverpanel/\nConfiguration : /etc/serverpanel/config.json\n' "$TARGET"
