#!/usr/bin/env bash
# Install the editable Python source tree on Debian 13 (no .deb).
set -euo pipefail

# Contrôle root : doit rester la toute première instruction exécutable du
# script, avant toute autre vérification (os-release, dpkg-query, ...), afin
# qu'aucune étape d'installation ne s'affiche ni ne s'exécute sans sudo.
# Compatible bash et sh : test POSIX, repli si EUID ou id ne sont pas dispos.
install_uid=${EUID:-$(id -u 2>/dev/null || printf 'inconnu')}
if [ "$install_uid" != '0' ]; then
    install_user=$(id -un 2>/dev/null || printf 'inconnu')
    printf 'Erreur : installation sans sudo.\n\n' >&2
    printf 'Le script doit être lancé en root (utilisateur détecté : %s, UID : %s).\n\n' \
        "$install_user" "$install_uid" >&2
    printf 'Relance avec :\n\n    sudo bash %s\n' "$0" >&2
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
for file in "$SOURCE/serverpanel/cli.py" "$SOURCE/serverpanel/ui.py"; do
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
apt-get install -y python3 apache2 certbot python3-certbot-apache cron fastfetch
systemctl enable --now cron >/dev/null 2>&1 || true

install -d -m 0755 "$TARGET/serverpanel" /etc/serverpanel /usr/local/bin /etc/update-motd.d /etc/cron.d
for file in "$SOURCE"/serverpanel/*.py; do
    install -m 0644 "$file" "$TARGET/serverpanel/$(basename "$file")"
done
for file in README.md SECURITY.md VALIDATION.md LICENSE; do
    if [[ -f "$SOURCE/$file" ]]; then
        install -m 0644 "$SOURCE/$file" "$TARGET/$file"
    fi
done
# Configuration : créée une seule fois (favoris des services réellement
# installés) ; un fichier existant n'est jamais modifié.
PYTHONPATH="$TARGET" /usr/bin/python3 -c 'from serverpanel.common import ensure_config; ensure_config()'
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
printf '\nServerPanel installé depuis les sources.\nCommande : sudo panel\nSources Python : %s/serverpanel/\nConfiguration : /etc/serverpanel/config.json\nCron : paquet cron installé, jobs gérés dans /etc/cron.d/serverpanel\n' "$TARGET"
