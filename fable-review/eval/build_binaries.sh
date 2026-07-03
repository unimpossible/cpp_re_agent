#!/usr/bin/env bash
# Compile the eval corpus into binaries for a real end-to-end run:
#   corpus/*.cpp -> bin/<case>-O0, bin/<case>-O2
# Feed the binaries to the app (or ghidrecomp directly), collect the raw and
# improved output per case, then score with run_eval.py.
set -euo pipefail

# Compiler is overridable so CI can matrix over g++ / clang++.
CXX="${CXX:-g++}"

cd "$(dirname "$0")"
mkdir -p bin

for src in corpus/*.cpp; do
    case=$(basename "$src" .cpp)
    for opt in O0 O2; do
        out="bin/${case}-${opt}"
        echo "${CXX} -${opt} -g0 ${src} -> ${out}"
        "$CXX" "-${opt}" -g0 -o "$out" "$src"
    done
done

echo "Done. Binaries in $(pwd)/bin"
