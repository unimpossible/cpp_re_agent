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

# Every case is built twice: with symbols and stripped. Stripped is the honest
# case — a shipped binary has no symbol table, so Ghidra names everything
# FUN_<addr> and nothing downstream can lean on a name. Keeping both lets the
# eval measure the gap between the two regimes rather than assume it.
build() {  # build <case> <opt> <sources...>
    local case=$1 opt=$2
    shift 2
    local out="bin/${case}-${opt}"
    echo "${CXX} -${opt} -g0 $* -> ${out}{,-stripped}"
    "$CXX" "-${opt}" -g0 -std=c++17 -o "$out" "$@"
    cp "$out" "${out}-stripped"
    strip --strip-all "${out}-stripped"
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

    # A shared object too, built from every unit that does NOT define main.
    # A .so has no entry point — its roots are the exported functions — so it
    # exercises a different shape of call graph than an executable, and its
    # export table survives `strip` (the dynamic linker needs it).
    lib_units=()
    for u in "${units[@]}"; do
        grep -qE '^[[:alnum:]_:<>* ]*\bmain[[:space:]]*\(' "$u" || lib_units+=("$u")
    done
    if [ ${#lib_units[@]} -gt 0 ]; then
        for opt in O0 O2; do
            out="bin/lib${case}-${opt}.so"
            echo "${CXX} -${opt} -fPIC -shared ${lib_units[*]} -> ${out}{,-stripped}"
            "$CXX" "-${opt}" -g0 -std=c++17 -fPIC -shared -o "$out" "${lib_units[@]}"
            cp "$out" "${out}-stripped"
            strip --strip-all "${out}-stripped"
        done
    fi
done

echo "Done. Binaries in $(pwd)/bin"
