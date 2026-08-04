#!/usr/bin/env bash
# Compile an eval corpus into binaries for a real end-to-end run:
#   <corpus>/*.cpp   -> bin/<case>-O0, bin/<case>-O2   (single-file program)
#   <corpus>/<name>/ -> bin/<name>-O0, bin/<name>-O2   (multi-file program: all
#                                                       .cpp in the dir linked
#                                                       into one binary)
# Feed the binaries to the app (or ghidrecomp directly), collect the raw and
# improved output per case, then score with run_eval.py.
#
#   ./build_binaries.sh                     # this tree's corpus/
#   ./build_binaries.sh ../../eval/corpus   # the tiered lab corpus, incl. tier4
set -euo pipefail

# Compiler is overridable so CI can matrix over g++ / clang++.
CXX="${CXX:-g++}"

cd "$(dirname "$0")"
CORPUS="${1:-corpus}"
mkdir -p bin

if [ ! -d "$CORPUS" ]; then
    echo "no such corpus dir: $CORPUS" >&2
    exit 1
fi

build() {  # build <case> <opt> <sources...>
    local case=$1 opt=$2
    shift 2
    local out="bin/${case}-${opt}"
    echo "${CXX} -${opt} -g0 $* -> ${out}"
    "$CXX" "-${opt}" -g0 -std=c++17 -o "$out" "$@"
}

shopt -s nullglob

for src in "$CORPUS"/*.cpp; do
    case=$(basename "$src" .cpp)
    for opt in O0 O2; do
        build "$case" "$opt" "$src"
    done
done

for dir in "$CORPUS"/*/; do
    case=$(basename "$dir")
    units=("$dir"*.cpp)
    if [ ${#units[@]} -eq 0 ]; then
        continue
    fi
    for opt in O0 O2; do
        build "$case" "$opt" "${units[@]}"
    done
done

echo "Done. Binaries in $(pwd)/bin"
