#!/bin/sh
# Create whyslow-<version>.tar.gz from the working tree (what the rpm/AUR packages build from).
set -eu
cd "$(dirname "$0")/.."
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' whyslow/__init__.py)
OUT=${OUT:-.}
tar --exclude=__pycache__ --exclude='*.pyc' --exclude=dist --exclude=build --exclude='*.egg-info' \
    --transform "s,^\./,whyslow-$VER/," -czf "$OUT/whyslow-$VER.tar.gz" \
    ./whyslow ./data ./packaging ./tests ./README.md ./LICENSE ./pyproject.toml
echo "$OUT/whyslow-$VER.tar.gz"
