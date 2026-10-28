#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUT=${1:-"$HERE/dist"}
mkdir -p "$OUT"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
install -d -m 0755 "$TMP/usr/lib/serverpanel/serverpanel" "$TMP/usr/bin" \
    "$TMP/etc/update-motd.d" "$TMP/usr/share/doc/serverpanel" "$TMP/usr/share/man/man1" "$TMP/DEBIAN"
for file in "$HERE"/serverpanel/*.py; do
    install -m 0644 "$file" "$TMP/usr/lib/serverpanel/serverpanel/"
done
install -m 0755 "$HERE/packaging/panel" "$TMP/usr/bin/panel"
install -m 0755 "$HERE/packaging/85-serverpanel" "$TMP/etc/update-motd.d/85-serverpanel"
install -m 0644 "$HERE/README.md" "$TMP/usr/share/doc/serverpanel/README.md"
gzip -n -9 -c "$HERE/packaging/man/panel.1" > "$TMP/usr/share/man/man1/panel.1.gz"
install -m 0644 "$HERE/SECURITY.md" "$TMP/usr/share/doc/serverpanel/SECURITY.md"
install -m 0644 "$HERE/VALIDATION.md" "$TMP/usr/share/doc/serverpanel/VALIDATION.md"
install -m 0644 "$HERE/LICENSE" "$TMP/usr/share/doc/serverpanel/copyright"
gzip -n -9 -c "$HERE/packaging/changelog" > "$TMP/usr/share/doc/serverpanel/changelog.Debian.gz"
install -m 0644 "$HERE/packaging/control" "$TMP/DEBIAN/control"
install -m 0644 "$HERE/packaging/conffiles" "$TMP/DEBIAN/conffiles"
install -m 0755 "$HERE/packaging/postinst" "$TMP/DEBIAN/postinst"
dpkg-deb --root-owner-group --build "$TMP" "$OUT/serverpanel_1.0.0~beta1-1_all.deb"
echo "$OUT/serverpanel_1.0.0~beta1-1_all.deb"
