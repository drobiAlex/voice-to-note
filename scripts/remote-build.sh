#!/bin/bash
# Push the current branch to the Mac build box and compile/test it there.
#
#   scripts/remote-build.sh status              # what the Mac is on right now
#   scripts/remote-build.sh check               # swiftc -typecheck both helpers (inner loop)
#   scripts/remote-build.sh build               # compile + codesign into the scratch VTN_HOME
#   scripts/remote-build.sh test [node]         # pytest on macOS, or one tests/file.py::name
#   scripts/remote-build.sh verify              # check + build + test, pulls logs back
#
# The Mac key is pinned to scripts/mac-remote-shell.sh (installed there as
# ~/bin/vtn-remote), so these verbs are the ONLY things this machine can run
# on it — there is no shell on the other end and no path outside
# ~/build/voice-to-note. See docs/remote-build.md.
set -euo pipefail

action="${1:-check}"; shift || true
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

run_remote() { ssh -o BatchMode=yes mac-vtn "$*"; }

if [[ "$action" == "status" ]]; then
    run_remote status
    exit 0
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "Uncommitted changes on $branch — commit first, the Mac builds what is pushed." >&2
    exit 1
fi

git push -q --force-with-lease mac-vtn "$branch"
run_remote sync "$branch"

case "$action" in
    check|build|test)
        run_remote "$action" "$@"
        ;;
    verify)
        status=0
        run_remote verify || status=$?
        # Logs come back as a tar: allowing `rsync --server` on the Mac would need
        # a far more permissive rule than "read this one directory".
        tmp="$(mktemp)"
        if run_remote pull-artifacts > "$tmp" 2>/dev/null; then
            tar xzf "$tmp" -C "$repo_root"
            echo "Logs pulled to $repo_root/artifacts/"
        else
            echo "No artifacts to pull." >&2
        fi
        rm -f "$tmp"
        exit "$status"
        ;;
    *)
        echo "Usage: scripts/remote-build.sh status|check|build|test [node]|verify" >&2
        exit 64
        ;;
esac
