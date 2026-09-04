#!/bin/bash
# What the Mac runs for one remote-build verb, inside the scratch checkout.
#
#   scripts/mac-ci.sh check    # swiftc -typecheck both helpers: nothing is executed
#   scripts/mac-ci.sh build    # uv sync + vtn setup into VTN_HOME=$PWD (compiles + signs)
#   scripts/mac-ci.sh test     # uv run pytest -q, or $VTN_ONLY_TEST
#   scripts/mac-ci.sh verify   # all three
#
# VTN_HOME is this checkout, the same as ./run.sh: the user's installed vtn and
# its database under ~/Library/Application Support/vtn are never touched, and
# no verb here ever runs `uv tool install`. The recorder is deliberately never
# launched — its TCC prompts need a person at the Mac.
set -euo pipefail
cd "$(dirname "$0")/.."
export VTN_HOME="$PWD"
# prove the recorder compiles; never put the scratch checkout's recorder in
# the menu bar of whoever owns this Mac
export VTN_SETUP_LAUNCH=off
mkdir -p artifacts
native=src/voice_to_note/native

check() {
    for f in capture menubar; do
        echo "typecheck $f.swift"
        swiftc -typecheck "$native/$f.swift" 2>&1 | tee "artifacts/typecheck-$f.log"
        [[ ${PIPESTATUS[0]} -eq 0 ]]
    done
}

build() {
    uv sync --quiet
    uv run vtn setup 2>&1 | tee artifacts/setup.log
    [[ ${PIPESTATUS[0]} -eq 0 ]]
    codesign --verify --verbose=2 bin/vtn-capture 2>&1 | tee artifacts/codesign.log
}

test_() {
    if [[ -n "${VTN_ONLY_TEST:-}" ]]; then
        uv run pytest -q "$VTN_ONLY_TEST" 2>&1 | tee artifacts/pytest.log
    else
        uv run pytest -q 2>&1 | tee artifacts/pytest.log
    fi
    [[ ${PIPESTATUS[0]} -eq 0 ]]
}

case "${1:-check}" in
    check)  check ;;
    build)  build ;;
    test)   test_ ;;
    verify) check && build && test_ ;;
    *) echo "usage: scripts/mac-ci.sh check|build|test|verify" >&2; exit 64 ;;
esac
