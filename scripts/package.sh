#!/bin/sh
# Packs build/pst and the standard units into build/pst-linux-x86_64.tar.gz, which
# unpacks to `pst`, `units/` and `docs/writing-pasthon.md`.

set -eu
cd "$(dirname "$0")/.."

rm -rf build/package build/pst-linux-x86_64.tar.gz
mkdir -p build/package/units build/package/docs
cp build/pst build/package/pst
cp std/*.pst build/package/units/
cp docs/writing-pasthon.md build/package/docs/
tar -czf build/pst-linux-x86_64.tar.gz -C build/package pst units docs
echo "packed build/pst-linux-x86_64.tar.gz"
