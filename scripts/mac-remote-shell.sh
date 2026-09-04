#!/bin/bash
#
# Gatekeeper for the build-box SSH key.
#
# Installed on the Mac as ~/bin/vtn-remote and pinned as a forced command in
# ~/.ssh/authorized_keys, so the VPS key CANNOT open a shell: sshd hands whatever
# the client asked for to this script in SSH_ORIGINAL_COMMAND, and anything not
# on the allowlist below is refused.
#
#   restrict,command="/Users/<you>/bin/vtn-remote" ssh-ed25519 AAAA... vps-vtn
#
# IMPORTANT: this file is the *source*. The installed copy lives at ~/bin, which
# is deliberately OUTSIDE ~/build/voice-to-note — the checkout is reset from
# whatever the VPS pushes, so a wrapper living inside it could be rewritten by a
# push and would enforce nothing.
#
# Allowed:  status, sync <branch>, check, build, test [node], verify,
#           pull-artifacts, git push (that one bare repo)
# Refused:  everything else — no shell, no rm, no arbitrary paths, no sudo,
#           and nothing that launches the recorder or touches the installed vtn.

set -euo pipefail

readonly REPO_DIR="$HOME/build/voice-to-note"
readonly BARE_REPO="build/voice-to-note.git"
readonly LOG_FILE="$HOME/build/vtn-remote.log"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

request="${SSH_ORIGINAL_COMMAND:-}"

log() {
    printf '%s\t%s\t%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" "${2//$'\n'/ }" >>"$LOG_FILE" 2>/dev/null || true
}

refuse() {
    log REFUSED "$request"
    {
        echo "vtn-remote: refused."
        echo "This key may only run: status | sync <branch> | check | build | test [node] |"
        echo "verify | pull-artifacts, plus git push to $BARE_REPO."
        echo "Requested: ${request:-<interactive shell>}"
    } >&2
    exit 126
}

# An empty SSH_ORIGINAL_COMMAND means someone asked for an interactive shell.
[[ -n "$request" ]] || refuse

# --- git push -------------------------------------------------------------
# The only write path into the Mac. Scoped to the one bare repo; the checkout is
# rebuilt from it by `sync`, so pushing is how code updates arrive.
if [[ "$request" == "git-receive-pack "* || "$request" == "git-upload-pack "* ]]; then
    target="${request#* }"
    target="${target#[\'\"]}"
    target="${target%[\'\"]}"
    case "$target" in
        "$BARE_REPO"|"/$BARE_REPO"|"~/$BARE_REPO"|"$HOME/$BARE_REPO")
            log ALLOWED "$request"
            # sshd starts a forced command in $HOME; git resolves the quoted
            # relative path from there, so make that true here too.
            cd "$HOME" && exec git shell -c "$request"
            ;;
    esac
    refuse
fi

# --- everything else must be one of our verbs -----------------------------
# Split on whitespace only. No eval, no globbing, no shell metacharacters survive:
# each argument is validated against a strict pattern before it is used.
read -r -a parts <<<"$request"
verb="${parts[0]:-}"
arg1="${parts[1]:-}"
[[ ${#parts[@]} -le 2 ]] || refuse

is_branch()   { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,80}$ && "$1" != *".."* ]]; }
# tests/test_x.py, optionally ::test_name — nothing that could leave tests/.
is_testnode() { [[ "$1" =~ ^tests/[A-Za-z0-9_]+\.py(::[A-Za-z0-9_]+){0,2}$ ]]; }

cd "$REPO_DIR" || { echo "vtn-remote: $REPO_DIR is missing." >&2; exit 1; }

case "$verb" in
    status)
        [[ -z "$arg1" ]] || refuse
        log ALLOWED "$request"
        git log --oneline -1
        git status -sb
        echo "macOS $(sw_vers -productVersion 2>/dev/null || echo '?')"
        echo "$(swiftc --version 2>/dev/null | head -1 || true)" | grep . || echo "swiftc: missing"
        uv --version 2>/dev/null || echo "uv: missing"
        for d in vendor models bin; do [[ -e "$d" ]] && echo "$d/: present" || echo "$d/: absent"; done
        ;;

    sync)
        # Reset the build checkout onto a pushed branch. --hard is bounded to this
        # one checkout; vendor/, models/ and data/ are ignored files and survive.
        is_branch "$arg1" || refuse
        log ALLOWED "$request"
        git fetch -q origin
        git checkout -q -f "$arg1"
        git reset -q --hard "origin/$arg1"
        git log --oneline -1
        ;;

    check|build|verify)
        [[ -z "$arg1" ]] || refuse
        log ALLOWED "$request"
        exec scripts/mac-ci.sh "$verb"
        ;;

    test)
        if [[ -n "$arg1" ]]; then
            is_testnode "$arg1" || refuse
            log ALLOWED "$request"
            VTN_ONLY_TEST="$arg1" exec scripts/mac-ci.sh test
        fi
        log ALLOWED "$request"
        exec scripts/mac-ci.sh test
        ;;

    pull-artifacts)
        # Stream the logs back as a tar. Read-only, and it replaces having to
        # allow `rsync --server`, whose argument list is far too open to constrain.
        [[ -z "$arg1" ]] || refuse
        log ALLOWED "$request"
        [[ -d artifacts ]] || { echo "vtn-remote: no artifacts/ yet." >&2; exit 1; }
        exec tar czf - artifacts
        ;;

    *)
        refuse
        ;;
esac
