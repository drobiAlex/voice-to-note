# Remote build box

`capture.swift` and `menubar.swift` only compile on a Mac; the coding agent runs on a
Linux VPS. So, exactly like finity (`~/repos/finity/docs/remote-build.md`), the VPS edits
and commits, and the Mac executes — through one pinned SSH key that cannot open a shell.

```
VPS (agent)                              Mac (build box, Tailscale)
  voice-to-note ──git push mac-vtn──▶ ~/build/voice-to-note.git (bare)
                                              │ sync
                                              ▼
                                       ~/build/voice-to-note  (VTN_HOME = this checkout)
                                              │ scripts/mac-ci.sh
                                              │   swiftc -typecheck → swiftc + codesign → pytest
                          ◀── tar (logs) ─────┘
```

Everything goes through one command:

```bash
scripts/remote-build.sh status              # branch, swiftc/macOS versions, scratch VTN_HOME state
scripts/remote-build.sh check               # swiftc -typecheck both helpers — seconds, the inner loop
scripts/remote-build.sh build               # compile + codesign into the scratch VTN_HOME
scripts/remote-build.sh test [tests/x.py::name]   # uv run pytest on macOS, optionally one node
scripts/remote-build.sh verify              # check + build + test, pulls build logs back
```

It refuses to run with uncommitted changes: the Mac builds what is *pushed*, so a green
result always names a commit, never somebody's working tree.

## What is different from finity

| finity | voice-to-note |
| --- | --- |
| `xcodebuild`, simulators, XCUITest | two `swiftc` calls and `codesign --sign -`; no Xcode project, no simulator |
| tests only run on the Mac | pytest runs on Linux already; the Mac run adds the macOS-only paths (`sys.platform`, `taskpolicy`, real `swiftc`) |
| evidence = screenshots | evidence = compiler output and the test log — the helpers cannot be *run* remotely, see below |

**The recorder is never run over SSH.** `vtn-capture` and `VTN Recorder.app` ask for
microphone and screen-recording permission through TCC, which only a person at the Mac
can grant; a headless launch would either hang on the prompt or be denied and prove
nothing. So the remote loop ends at "it compiles, signs, and the tests pass", and a real
recording is checked by the user, in `vtn tui` on the Mac, before merge. That stays a
manual step on purpose.

**The user's own `vtn` is never touched.** The scratch checkout is its own `VTN_HOME`
(what `./run.sh` already does), so `data/`, `models/`, `vendor/` and `vtn.toml` land in
`~/build/voice-to-note`. No verb runs `uv tool install`, and nothing reads or writes
`~/Library/Application Support/vtn`. Upgrading the installed tool remains the user's
deliberate `install.sh` / `uv tool install --reinstall`.

`vendor/` and `models/` are gitignored, and `git reset --hard` leaves ignored files alone,
so the one-time whisper.cpp build and model download survive every `sync`. The first
`build` on a fresh scratch checkout is the slow one (`vtn setup` clones and builds
whisper.cpp); every later one is two `swiftc` invocations.

## Why the Mac cannot be told to do anything else

A **second** keypair, separate from finity's, so the two gatekeepers stay independent and
either can be revoked alone:

```
# VPS
ssh-keygen -t ed25519 -f ~/.ssh/vtn_mac_ed25519 -C vps-vtn -N ''

# ~/.ssh/config on the VPS
Host mac-vtn
    HostName <tailscale ip of the Mac>
    User <you>
    IdentityFile ~/.ssh/vtn_mac_ed25519
    IdentitiesOnly yes
    BatchMode yes
```

On the Mac, the key is pinned in `~/.ssh/authorized_keys` to a forced command — one more
line beside finity's, each key its own gatekeeper:

```
restrict,command="/Users/<you>/bin/vtn-remote" ssh-ed25519 AAAA... vps-vtn
```

`~/bin/vtn-remote` is `scripts/mac-remote-shell.sh`. sshd hands it whatever the client
asked for in `SSH_ORIGINAL_COMMAND`, and it runs only:

| Allowed | |
| --- | --- |
| `status` | commit, working tree, `swiftc --version`, `sw_vers`, whether `vendor/` and `models/` exist |
| `sync <branch>` | fetch + hard-reset the scratch checkout |
| `check` | `swiftc -typecheck` on both helpers |
| `build` | `scripts/mac-ci.sh build` — `uv sync`, `vtn setup` into the scratch `VTN_HOME` |
| `test [node]` | `uv run pytest -q`, optionally one `tests/file.py::name` |
| `verify` | check + build + test |
| `pull-artifacts` | streams `artifacts/` (logs) back as a tar |
| `git push` | to `build/voice-to-note.git` **only** |

Anything else — a shell, `rm`, `sudo`, `open`, a path outside `~/build/voice-to-note`, a
second argument that isn't a valid branch or test node — exits 126 and is logged to
`~/build/vtn-remote.log`. `restrict` also turns off the pty and all forwarding. Arguments
are matched against strict patterns and never passed through a shell.

**The installed copy lives at `~/bin`, deliberately outside the checkout.** The checkout is
reset from whatever the VPS pushes; a gatekeeper living inside it could be rewritten by a
push and would enforce nothing. The same is *not* true of `scripts/mac-ci.sh`, which does
live in the checkout — it is the build, and the build is arbitrary code by nature (below).

### Installing (done by a person, at the Mac — the VPS key cannot do this by design)

```bash
mkdir -p ~/build ~/bin
git init --bare ~/build/voice-to-note.git
git clone ~/build/voice-to-note.git ~/build/voice-to-note      # scratch checkout; origin = the bare repo
cat > ~/bin/vtn-remote < scripts/mac-remote-shell.sh && chmod 700 ~/bin/vtn-remote
cp ~/.ssh/authorized_keys ~/.ssh/authorized_keys.bak.$(date +%F)   # before editing it
echo 'restrict,command="/Users/<you>/bin/vtn-remote" ssh-ed25519 AAAA... vps-vtn' >> ~/.ssh/authorized_keys
```

First push from the VPS: `git remote add mac-vtn mac-vtn:build/voice-to-note.git`, then
`scripts/remote-build.sh status`. Updating the gatekeeper later is the same `cat >` line,
run by the person at the Mac, from a reviewed commit.

Emergency: `cp ~/.ssh/authorized_keys.bak.<date> ~/.ssh/authorized_keys` on the Mac.

### What this does not protect against

The Mac compiles and runs the Swift the VPS pushes, and pytest runs the Python the VPS
pushes: both are arbitrary code, executed as the ordinary user, never root. The gatekeeper
stops the agent from *typing* a dangerous command; it cannot stop a dangerous commit from
doing the same thing during `build` or `test`. So: the agent shows its diff before
`remote-build.sh` is run on it, and `verify` runs on reviewed commits. Two habits keep
that cheap: `check` (typecheck only, nothing executed) is the inner loop, and `build` /
`test` are asked for explicitly.

## Working agreement for the agent

1. Edit `src/voice_to_note/native/*.swift` here; run `uv run pytest -q`, `mypy`, `ruff` here as usual.
2. Commit. `scripts/remote-build.sh check` for a compile answer — the only remote verb to run freely.
3. `build` / `test` / `verify` only after the diff has been shown, and never in a loop —
   each is a push plus arbitrary code on the user's Mac.
4. Never work around a refusal. `126` means the verb is not allowed; say so rather than
   look for another way in (no `ssh mac`, no finity's key, no scp).
5. Never ask the Mac to run the recorder, open an app, or touch `~/Library/Application Support/vtn`.
6. Anything that needs a person — granting TCC, a real recording, `uv tool install` — is
   written down as the next step for the user, not attempted.

## Operational notes

- The Mac must be awake: it drops off Tailscale on sleep and every command then times out.
- `build` needs Xcode command line tools (`swiftc`), `uv`, `cmake` and `ffmpeg` on the Mac —
  the same things `install.sh` needs. `status` reports the first two.
- Two gatekeepers on one Mac never collide: finity's owns `~/build/finity`, this one owns
  `~/build/voice-to-note`; neither knows the other's path.
