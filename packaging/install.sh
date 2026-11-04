#!/bin/sh
# Install WhySlow on any distro without a package.   ./install.sh [--prefix /usr/local] [--uninstall]
# Needs python3 >= 3.11 and (for the window) PySide6:  Fedora python3-pyside6 · Arch pyside6 ·
# Debian/Ubuntu python3-pyside6.qtwidgets (+qtcore, qtgui, qtnetwork) · otherwise `pip install PySide6`.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
prefix=/usr/local
action=install
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) prefix=$2; shift 2;;
        --uninstall) action=uninstall; shift;;
        -h|--help) sed -n '2,5p' "$0"; exit 0;;
        *) echo "unknown option: $1" >&2; exit 2;;
    esac
done
share=$prefix/share/whyslow
if [ "$action" = uninstall ]; then
    rm -rf "$share" "$prefix/bin/whyslow" "$prefix/lib/systemd/user/whyslow.service" \
        "$prefix/share/applications/io.github.metalmike17.WhySlow.desktop" \
        "$prefix/share/metainfo/io.github.metalmike17.WhySlow.metainfo.xml" \
        "$prefix/share/icons/hicolor/scalable/apps/io.github.metalmike17.WhySlow.svg"
    echo "removed. Run 'whyslow disable' first if you enabled recording."
    exit 0
fi
python3 - <<'PY' || { echo "python3 >= 3.11 is required" >&2; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
install -d "$share" "$prefix/bin"
cp -r "$here/whyslow" "$share/"
find "$share" -name __pycache__ -prune -exec rm -rf {} +
cat > "$prefix/bin/whyslow" <<LAUNCH
#!/usr/bin/python3
import sys
sys.path.insert(0, "$share")
from whyslow.cli import main
sys.exit(main())
LAUNCH
chmod 755 "$prefix/bin/whyslow"
install -Dm644 "$here/packaging/whyslow-user.service" "$prefix/lib/systemd/user/whyslow.service"
sed -i "s,/usr/bin/whyslow,$prefix/bin/whyslow," "$prefix/lib/systemd/user/whyslow.service"
install -Dm644 "$here/data/io.github.metalmike17.WhySlow.desktop" -t "$prefix/share/applications/"
install -Dm644 "$here/data/io.github.metalmike17.WhySlow.metainfo.xml" -t "$prefix/share/metainfo/"
install -Dm644 "$here/data/io.github.metalmike17.WhySlow.svg" -t "$prefix/share/icons/hicolor/scalable/apps/"
echo "installed to $prefix. Start recording with:  whyslow enable"
