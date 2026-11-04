#!/bin/sh
# Build whyslow_<version>_all.deb from the working tree (Debian, Ubuntu, Mint, Pop!_OS...). Needs dpkg-deb.
set -eu
cd "$(dirname "$0")/../.."
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' whyslow/__init__.py)
ROOT=$(mktemp -d)
PKG=$ROOT/whyslow_${VER}_all
install -d "$PKG/DEBIAN" "$PKG/usr/lib/python3/dist-packages"
cp -r whyslow "$PKG/usr/lib/python3/dist-packages/"
find "$PKG" -name __pycache__ -prune -exec rm -rf {} +
install -Dm755 packaging/whyslow-launcher "$PKG/usr/bin/whyslow"
install -Dm644 packaging/whyslow-user.service "$PKG/usr/lib/systemd/user/whyslow.service"
install -Dm644 data/io.github.metalmike17.WhySlow.desktop -t "$PKG/usr/share/applications/"
install -Dm644 data/io.github.metalmike17.WhySlow.metainfo.xml -t "$PKG/usr/share/metainfo/"
install -Dm644 data/io.github.metalmike17.WhySlow.svg -t "$PKG/usr/share/icons/hicolor/scalable/apps/"
install -Dm644 LICENSE "$PKG/usr/share/doc/whyslow/copyright"
cat > "$PKG/DEBIAN/control" <<CTRL
Package: whyslow
Version: $VER
Architecture: all
Maintainer: metalmike17 <michaeljg0717@gmail.com>
Depends: python3 (>= 3.11)
Recommends: python3-pyside6.qtcore, python3-pyside6.qtgui, python3-pyside6.qtwidgets, python3-pyside6.qtnetwork, mangohud
Section: utils
Priority: optional
Homepage: https://github.com/metalmike17/whyslow
Description: Find out why your Linux system just stuttered, froze or crashed
 WhySlow records a few seconds of system health every second and, when
 something feels slow, explains in plain English which program caused it and
 what to do: a black box for freezes and crashes, a known-offender database,
 game stutter analysis from MangoHud logs, a hardware-gotcha check, laptop
 battery culprits, a slow-boot explainer and an anonymized shareable report.
 .
 Recording is opt-in: run "whyslow enable" after installing.
CTRL
cat > "$PKG/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
    echo "WhySlow installed. Start recording with:  whyslow enable" >&2
    if ! python3 -c "import PySide6.QtWidgets" >/dev/null 2>&1; then
        echo "The window needs PySide6, which this release does not package: pip install --user PySide6" >&2
        echo "(the command-line features work without it)" >&2
    fi
fi
POST
chmod 755 "$PKG/DEBIAN/postinst"
dpkg-deb --root-owner-group --build "$PKG" "${OUT:-.}/whyslow_${VER}_all.deb"
