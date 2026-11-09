#!/bin/sh
# Packs build/pst and the standard units into build/pst-linux-x86_64.tar.gz, which
# unpacks to `pst` and `units/`.

set -eu
cd "$(dirname "$0")/.."

rm -rf build/package build/pst-linux-x86_64.tar.gz
mkdir -p build/package/units
cp build/pst build/package/pst
cp std/*.pst build/package/units/
tar -czf build/pst-linux-x86_64.tar.gz -C build/package pst units
echo "packed build/pst-linux-x86_64.tar.gz"
