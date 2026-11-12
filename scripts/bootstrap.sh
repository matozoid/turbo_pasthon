#!/bin/sh
# Builds the compiler, compiler/Pst.pst, into build/pst, with the standard units in
# build/units.
#
# The seed that builds it is $PST_SEED if set, else the latest release on GitHub. The seed
# builds stage 1, stage 1 builds stage 2 and stage 2 builds stage 3. Stages 2 and 3 are
# both this checkout's code generation, so they must be identical byte for byte; stage 1
# isn't when the code generation changed since the seed.
#
# The compiler may only use the language features that the latest release supports:
# a feature the compiler needs goes into one release before the compiler uses it.

set -eu
cd "$(dirname "$0")/.."

RELEASE=https://github.com/matozoid/turbo_pasthon/releases/latest/download/pst-linux-x86_64.tar.gz

rm -rf build/seed build/stage1 build/stage2 build/stage3 build/pst build/units build/docs

# Puts the units link next to an executable in build/$1.
stage() {
    mkdir -p "build/$1"
    ln -s ../../std "build/$1/units"
}

# The seed runs from build/seed, with this checkout's standard units, not its own.
stage seed
if [ -n "${PST_SEED:-}" ]; then
    cp "$PST_SEED" build/seed/pst
    echo "seed: $PST_SEED"
else
    curl -fsSL "$RELEASE" | tar -xz -C build/seed pst
    echo "seed: the latest release"
fi

stage stage1
build/seed/pst build compiler/Pst.pst -o build/stage1/pst
stage stage2
build/stage1/pst build compiler/Pst.pst -o build/stage2/pst
stage stage3
build/stage2/pst build compiler/Pst.pst -o build/stage3/pst
if ! cmp -s build/stage2/pst build/stage3/pst; then
    echo "error: stage 2 and stage 3 differ" >&2
    exit 1
fi

cp build/stage3/pst build/pst
ln -s ../std build/units
ln -s ../docs build/docs
echo "built build/pst"
