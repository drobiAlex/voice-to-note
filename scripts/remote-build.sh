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

# The same Mac and user as the finity build box, so its `mac` host entry is
# reused; only the key differs, and it is pinned there to vtn-remote rather
# than finity-remote. Overridable for a Mac that is configured some other way.
host="${VTN_MAC_HOST:-mac}"
key="${VTN_MAC_KEY:-$HOME/.ssh/vtn_mac_ed25519}"
ssh_opts=(-o BatchMode=yes)
if [[ -f "$key" ]]; then
    ssh_opts+=(-i "$key" -o IdentitiesOnly=yes)
else
    echo "note: $key not found, letting ssh pick the key for host '$host'" >&2
fi
run_remote() { ssh "${ssh_opts[@]}" "$host" "$*"; }

if [[ "$action" == "status" ]]; then
    run_remote status
    exit 0
fi

# One-time setup, run while the key still has a shell: bare repo, scratch
# checkout, the gatekeeper at ~/bin, and then the authorized_keys line for this
# key rewritten so that from this moment on only the gatekeeper answers it.
# Idempotent; refuses to touch any line but the one holding this key.
if [[ "$action" == "install" ]]; then
    [[ -f "$key.pub" ]] || { echo "install needs $key.pub to know which line to pin" >&2; exit 1; }
    pub="$(awk '{print $1" "$2}' "$key.pub")"
    branch="$(git rev-parse --abbrev-ref HEAD)"
    run_remote 'mkdir -p ~/build ~/bin && cd ~/build && { [ -d voice-to-note.git ] || git init -q --bare voice-to-note.git; }'
    GIT_SSH_COMMAND="ssh ${ssh_opts[*]}" git push -q "$host:build/voice-to-note.git" "$branch"
    run_remote "cd ~/build && { [ -d voice-to-note ] || git clone -q voice-to-note.git voice-to-note; } && cd voice-to-note && git checkout -q -f $branch"
    ssh "${ssh_opts[@]}" "$host" 'cat > ~/bin/vtn-remote && chmod 700 ~/bin/vtn-remote' < scripts/mac-remote-shell.sh
    # pin the key: the line is found by its key material, everything else in the
    # file is copied through untouched, and a line already pinned stays as it is
    ssh "${ssh_opts[@]}" "$host" 'pub="$(cat)"; f=~/.ssh/authorized_keys
        cp "$f" "$f.bak.$(date +%Y%m%d%H%M%S)"
        grep -qF "$pub" "$f" || { echo "key not present in authorized_keys" >&2; exit 1; }
        awk -v pub="$pub" -v cmd="restrict,command=\"$HOME/bin/vtn-remote\"" '"'"'
            index($0, pub) && index($0, "vtn-remote") == 0 { print cmd " " $0; next } { print }
        '"'"' "$f" > "$f.new" && cat "$f.new" > "$f" && rm "$f.new"
        grep -F "$pub" "$f" | cut -c1-60' <<<"$pub"
    echo "--- proving the restriction: a shell must be refused, status must answer"
    if ssh "${ssh_opts[@]}" "$host" true 2>/dev/null; then
        echo "STILL UNRESTRICTED: a plain command ran. Check ~/.ssh/authorized_keys on the Mac." >&2
        exit 1
    fi
    run_remote status
    exit 0
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "Uncommitted changes on $branch — commit first, the Mac builds what is pushed." >&2
    exit 1
fi

GIT_SSH_COMMAND="ssh ${ssh_opts[*]}" git push -q --force "$host:build/voice-to-note.git" "$branch"   # scratch mirror, only this script writes it
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
        echo "Usage: scripts/remote-build.sh install|status|check|build|test [node]|verify" >&2
        exit 64
        ;;
esac
