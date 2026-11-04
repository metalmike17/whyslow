#!/bin/bash
# Run INSIDE ubuntu:24.04 (glibc 2.39) so the AppImage runs on Ubuntu 24.04+, Debian 13+, Fedora 40+, Arch...
# /src = repo (read-only), /out = output dir. Produces /out/AppDir with Python + PySide6 + whyslow bundled.
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-pip binutils libxcb-cursor0 libxkbcommon-x11-0 libxkbcommon0 libxcb-icccm4 \
  libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 libxcb-xkb1 libxcb-shape0 libxcb-randr0 \
  libxcb-sync1 libxcb-xfixes0 libfontconfig1 libfreetype6 libdbus-1-3 libwayland-client0 libwayland-cursor0 \
  libwayland-egl1 libglib2.0-0t64 libexpat1 libffi8 zlib1g libssl3t64 libsqlite3-0 liblzma5 libbz2-1.0 libgssapi-krb5-2 >/dev/null
A=/out/AppDir
rm -rf "${A:?}"; mkdir -p "$A/usr/bin" "$A/usr/lib" "$A/usr/share"
PYV=$(python3 -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
cp "/usr/bin/python$PYV" "$A/usr/bin/python3"
mkdir -p "$A/usr/lib/python$PYV"
cp -a "/usr/lib/python$PYV/." "$A/usr/lib/python$PYV/"
PL="$A/usr/lib/python$PYV"
for d in test idlelib tkinter turtledemo lib2to3 ensurepip EXTERNALLY-MANAGED; do rm -rf "${PL:?}/$d"; done
SP="$PL/site-packages"; mkdir -p "$SP"
pip install --quiet --no-cache-dir --break-system-packages --target "$SP" PySide6-Essentials
cp -a /src/whyslow "$SP/"
# trim what a widgets app never loads
Q="$SP/PySide6"
for d in Qt/qml Qt/translations Qt/metatypes include typesystems glue scripts support designer assistant linguist lupdate Qt/libexec examples; do
  rm -rf "${Q:?}/$d"
done
find "${Q:?}" -maxdepth 1 -name '*.pyi' -delete
find "${A:?}" -name __pycache__ -type d -prune -exec rm -rf {} +
# shared-library closure; the host provides glibc, GL, X11 core, audio, systemd
EX='^(linux-vdso|ld-linux|libc\.|libm\.|libdl\.|libpthread|librt\.|libutil|libresolv|libnsl|libGL|libEGL|libGLX|libOpenGL|libgbm|libdrm|libX11\.|libX11-xcb|libxcb\.|libasound|libpulse|libudev|libsystemd|libstdc\+\+|libgcc_s|libnss)'
TARGETS="$A/usr/bin/python3 $(find "$A/usr/lib" -name '*.so*' -type f)"
for f in $TARGETS; do
  ldd "$f" 2>/dev/null | awk '/=> \//{print $3}' | while read -r l; do
    b=$(basename "$l"); echo "$b" | grep -Eq "$EX" && continue
    cp -nL "$l" "$A/usr/lib/"
  done
done
# libraries Qt's platform plugins load at run time
for l in libxcb-cursor.so.0 libxkbcommon-x11.so.0 libxkbcommon.so.0 libxcb-icccm.so.4 libxcb-image.so.0 libxcb-keysyms.so.1 \
         libxcb-render-util.so.0 libxcb-xinerama.so.0 libxcb-xkb.so.1 libxcb-shape.so.0 libxcb-randr.so.0 libxcb-sync.so.1 \
         libxcb-xfixes.so.0 libwayland-client.so.0 libwayland-cursor.so.0 libwayland-egl.so.1 libfontconfig.so.1 libfreetype.so.6 libdbus-1.so.3; do
  p=$(ldconfig -p | awk -v n="$l" '$1==n{print $NF; exit}'); if [ -n "$p" ]; then cp -nL "$p" "$A/usr/lib/"; fi
done
du -sh "$A"; echo "py $PYV"
# anything still unresolved (not bundled and not an excluded host library) would break on a bare system: list it
echo "--- unresolved libraries:"
for f in "$A/usr/bin/python3" $(find "$A/usr/lib" -name '*.so*' -type f); do
  LD_LIBRARY_PATH="$A/usr/lib" ldd "$f" 2>/dev/null | awk '/not found/{print $1}'
done | sort -u
