#!/bin/sh
# Builds the compiler, compiler/Pst.pst, into build/pst, with the standard units in
# build/units.
#
# The seed that builds it is $PST_SEED if set, else the latest release on GitHub, else
# (when there is no release yet) the Python compiler. The seed builds stage 1, stage 1
# builds stage 2, and the two must be identical byte for byte.
#
# The compiler may only use the language features that the latest release supports:
# a feature the compiler needs goes into one release before the compiler uses it.

set -eu
cd "$(dirname "$0")/.."

RELEASE=https://github.com/matozoid/turbo_pasthon/releases/latest/download/pst-linux-x86_64.tar.gz

rm -rf build/seed build/stage1 build/stage2 build/pst build/units
mkdir -p build/seed

# Puts the units link next to an executable in build/$1.
stage() {
    mkdir -p "build/$1"
    ln -s ../../std "build/$1/units"
}

seed=${PST_SEED:-}
if [ -z "$seed" ]; then
    if curl -fsL "$RELEASE" | tar -xz -C build/seed 2>/dev/null; then
        seed=build/seed/pst
        echo "seed: the latest release"
    else
        echo "seed: the Python compiler (no release found)"
    fi
else
    echo "seed: $seed"
fi

stage stage1
if [ -n "$seed" ]; then
    "$seed" build compiler/Pst.pst -o build/stage1/pst
else
    python3 -m pst build compiler/Pst.pst -o build/stage1/pst
fi
stage stage2
build/stage1/pst build compiler/Pst.pst -o build/stage2/pst
if ! cmp -s build/stage1/pst build/stage2/pst; then
    echo "error: stage 1 and stage 2 differ" >&2
    exit 1
fi

cp build/stage2/pst build/pst
ln -s ../std build/units
echo "built build/pst"
