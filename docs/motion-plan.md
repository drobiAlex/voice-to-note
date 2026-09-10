# Motion: giving the floating recorder a settle

A plan for taking MacPaw's spring physics into this app, end to end — what is copied, what
is deliberately not, what changes in the build, and how anyone can tell afterwards whether
it worked. Written before any of it is done, so the acceptance is agreed in advance rather
than argued about at the end.

## Why this is worth doing, and what it is not

It adds no capability. Every window this changes already goes where it goes; what changes
is how it arrives. A window that glides to a stop reads as a rectangle being repositioned;
a window that overshoots a little and settles reads as a thing with weight. That is the
whole of it, and it is also most of what people mean when they call an app well made.

MacPaw's Eney is the reference, and the half of it that matters here is open source: the
damped-spring physics that moves their companion. Their character — the face, the eyes, the
personality — is not being copied and would not suit a recorder that exists to get out of
the way.

## What is copied, and what is not

**Copied**, from [MacPaw/CocoaSprings](https://github.com/MacPaw/CocoaSprings), MIT,
© 2023 MacPaw Inc. — three files, about 4 KB, no platform code in any of them:

| File | What it is |
| --- | --- |
| `SpringConfiguration.swift` | two numbers: `angularFrequency`, `dampingRatio` |
| `SpringMotionState.swift` | a position and a velocity |
| `SpringMotionPhysics.swift` | the closed-form damped-spring step, after Ryan Juckett |

**Not copied**: `SpringMotionWindow`, `SpringMotionPanel`, `SpringMotionView`,
`SpringMotionLayer`. They are the plumbing, and this app wants different plumbing for two
reasons. They drive themselves with `CVDisplayLink`, which Apple deprecated in macOS 15 and
documents as replaced by `NSView.displayLink(target:selector:)`. And they advance the
physics by one fixed 0.008 s step per display frame, which means the same animation runs
twice as fast on a 120 Hz display as on a 60 Hz one. Ours accumulates real elapsed
time and steps a fixed timestep, so a settle takes the same wall-clock time on any display.

**Licence hygiene**, and it is not optional: the copied file keeps its upstream header and
carries the full MIT text; `README.md` gains a third-party notice naming the project, the
copyright holder and the licence; the file is *not* reformatted into this repo's house
style and is exempt from its prose-docstring rule, because the only way to check it against
upstream later is for it to still look like upstream.

## The inventory of motion

Everything that moves in the menu bar app, what it does today, and what it will do. A plan
that only says "add springs" invites springs where they do not belong.

| Motion | Today | After | Why |
| --- | --- | --- | --- |
| Puck arrives from the status item | `CABasicAnimation` on `transform`, ease-out, 0.30 s | `CASpringAnimation`, same numbers as the window | it is a layer; Core Animation springs it in the render server for free |
| Island arrives | the same | **unchanged** | the island stands in for a menu, and a menu is on screen when it is asked for, not a moment later |
| Puck tucks into an edge | `NSAnimationContext` `setFrame`, ease-out, 0.22 s | spring on the origin | the settle is the entire point |
| Puck peeks back out | the same | spring on the origin | as above |
| Either window fades away | `alphaValue` to 0 over 0.12 s | **unchanged** | a window that takes as long to leave is a window in the way |
| Puck while transcribing | says "Processing …" and nothing moves | a slow arc turning on the rim | Eney's own working state: "resembles a loading icon" |
| Everything at rest | still | **still** | MacPaw's own rule, and here also a necessity — see the energy step |

## The numbers, in one place

The starting reference was `angularFrequency 7.5`, `dampingRatio 0.5`, MacPaw's shipped
defaults. The implemented puck uses `40` / `0.7`, centralized in `PuckMotion` in
`menubar.swift`: the original slow spring cannot meet the strict velocity threshold and
700 ms acceptance together. The vendored defaults remain untouched.

The layer animations use Core Animation's own spring, which is parameterised differently;
the implemented spring in its terms is `mass 1`, `stiffness ω² = 1600`, `damping 2ζω = 56`.
Written once as a constant with that derivation in a comment, so the window and the layers
are demonstrably the same spring rather than two springs that look similar.

## The steps

### Step 0 — Record what it looks like now

A screen capture of each motion in the inventory, on the Mac, before anything changes.

*Why:* "better" is a comparison. Without a before, the only available verdict is whether
the person who wrote it likes it, which is the verdict this step exists to avoid.

*Done when:* five short clips exist, and the tuck is captured on both a 60 Hz and a 120 Hz
display if both are to hand.

### Step 1 — Vendor the physics

`src/voice_to_note/native/springs.swift`: the three files above, concatenated in
dependency order, with their upstream headers intact, the full MIT licence text at the top
of the file, and a short provenance note saying which commit they came from and what was
deliberately left behind. The two `CGPoint` operators the physics needs come with them.

*Why one file rather than three:* the build compiles a list of sources by name, and a
directory of third-party files is a directory somebody will eventually add a fourth file
to. One file is one decision to revisit.

*Done when:* the file compiles alone (`swiftc -typecheck springs.swift`) and contains no
AppKit, no `CVDisplayLink`, and nothing that touches a window.

### Step 2 — Teach the build about a second source

`config.MENUBAR_SPRINGS` names the new file. `bootstrap.build_menubar` takes a list of
sources instead of one path and passes them all to `swiftc`. `services.setup` adds it to
`menubar_sources`, which is already a list and already hashed as one — so a change to the
vendored file rebuilds the app exactly as a change to `menubar.swift` does.

*Why it matters that the stamp covers it:* a rebuild that skips because only the vendored
file changed would leave a binary that no longer matches its source, and the next person to
debug the motion would be debugging code that is not running.

*Touches:* `config.py`, `gateways/bootstrap.py`, `services.py` (the `World` signature and
the mock world), and four call sites in `tests/`.

*Done when:* `uv run pytest -q` is green, and `./run.sh setup` on the Mac rebuilds the app
when only `springs.swift` has changed.

### Step 3 — The driver

`SpringMotion` in `menubar.swift`: owns a `SpringMotionPhysics`, a current
`SpringMotionState`, a destination, and a display link taken from the view inside the
window (`NSView.displayLink(target:selector:)`, macOS 14+, which the app already requires).

Each tick: add the frame's real elapsed time to an accumulator, step the physics at a fixed
1/120 s while the accumulator allows, and set the window's origin to the resulting
position. Stop when the velocity is under 0.001 on both axes *and* the position is within
half a point of the destination — then snap to the destination exactly and invalidate the
link, so a window never comes to rest a third of a pixel from where it was told to be.

*The three behaviours this is for:*
- **Redirect**: a new destination while running keeps the current position *and velocity*.
  That is the continuity a re-grabbed window needs, and the reason to use real physics
  rather than a curve.
- **Interrupt**: taking hold of the puck stops the motion where it is.
- **Lifetime**: the link is invalidated when the window closes, when Reduce Motion is on,
  and on settling. It never exists while nothing is moving.

*Done when:* a tuck settles in the same wall-clock time on a 60 Hz and a 120 Hz display, to
within five per cent.

### Step 4 — The window motions

`Dock.slide(to:alpha:)` springs the origin through the driver and animates the alpha
alongside it with the short fade it already uses. The frame's *size* never changes in a
tuck or a peek, so only the origin is sprung — no resizing, and nothing for the two
animations to fight over.

*The one design decision to make explicitly:* a spring overshoots, and a tuck's destination
is already off-screen, so the overshoot briefly hides more of the sliver than the rest
position does. Bound it — the destination is clamped so that no overshoot can take the
body's visible edge past half the peek — rather than removing the overshoot, which is the
thing being built.

*Done when:* tucking, peeking, and grabbing the puck mid-slide all behave, and the sliver
is never entirely invisible at any frame.

### Step 5 — The arrival

The puck's `appear()` swaps its `CABasicAnimation` for a `CASpringAnimation` using the
derived constants. The island keeps its ease-out.

*Why not the driver:* this is a layer transform, which Core Animation animates in the
render server without a display link or a main-thread tick. Using the driver here would be
strictly more expensive and no more faithful.

### Step 6 — The working state

A `CAShapeLayer` arc on the puck's rim, rotated by one `CABasicAnimation` with
`repeatCount: .infinity`, added when the state becomes `.processing` and removed on every
other state. The rim is already drawn and already changes colour by state, so this is the
same rim wearing one more thing.

*Why it earns its keep:* transcribing takes minutes, and the puck currently says
"Processing …" with nothing to show that anything is still happening. A stopped app and a
working one should not look identical.

### Step 7 — Reduce Motion

Every path above already asks `NSWorkspace.accessibilityDisplayShouldReduceMotion` once,
when the window is built, and the springs must answer to the same question: no spring, no
rotation, no overshoot — the window is set to its frame and the arc is drawn static.

*Why it is a step and not a footnote:* Apple's wording is to avoid large animations, and a
window sliding two hundred points across the screen is the largest thing this app does.

### Step 8 — Energy

A display link exists only between the start of a motion and its settle. Nothing repeats,
nothing polls, and the rim's rotation is a render-server animation that is removed with the
state it belongs to.

*The trap to name out loud:* a status item keeps this app classed as visible, so App Nap
never engages. Nothing will pause an animation left running by mistake except the code that
started it.

*Done when:* `sudo powermetrics --samplers tasks --show-process-energy` shows the recorder
at zero wakeups attributable to motion while the puck sits untouched, tucked or free.

### Step 9 — Tests and previews

Python covers the build change. Swift has no test harness here, so the preview app carries
the motions: the existing scenarios open and tuck the puck, and *Replay Arrival* already
exists for watching an arrival more than once. A processing scenario shows the turning rim.

*What cannot be automated, and so must be listed:* the feel. Step 10 is that list.

### Step 10 — Verify on the Mac, against the criteria below

The recorder cannot run over the remote build key — its permissions need a person — so this
step is a person at the Mac with the acceptance criteria in front of them.

### Step 11 — Write it down

The third-party notice in `README.md`; a line in `CLAUDE.md` saying where the motion
constants live and that the vendored file is not to be reformatted; this plan updated to
say what was actually built.

## Acceptance criteria

Observable, in order of how easily they are checked:

1. Dragging the puck to an edge and letting go: it moves past its resting place once, by
   somewhere between four and twelve points, and comes back. Not twice, not visibly
   bouncing.
2. It is at rest within 700 ms of release, and nothing moves after that.
3. The same motion takes the same time on a 60 Hz and a 120 Hz display, within five per
   cent.
4. Grabbing the puck while it is sliding never jumps: it continues from where it is, at the
   speed it was going.
5. The sliver is visible in every frame of a tuck, including the overshoot.
6. With Reduce Motion on, nothing springs and nothing turns; every window is simply where
   it should be.
7. While transcribing, the rim turns; in every other state it does not exist.
8. Untouched — tucked, free, or hidden — the recorder shows no wakeups from motion in
   `powermetrics`, and the main thread is idle.
9. `springs.swift` still diffs cleanly against upstream apart from its provenance header.

## What could go wrong

- **A display link on a hidden view never fires.** Apple documents exactly that: "if the
  view is hidden, or not on any display, the callback will not be invoked". A motion begun
  on a window that is then ordered out would never settle. The guard is that the settle is
  also reachable without a tick — if no frame arrives within a few hundred milliseconds,
  the window is set to its destination and the link is invalidated.
- **Main-thread stutter.** Window origins can only be set on the main thread, so a busy
  main thread shows up as a stuttering slide. The tick must stay trivial: arithmetic and
  one `setFrameOrigin`, no drawing, no layout, no logging.
- **Two motions at once.** A tuck that starts while a peek is still running must redirect
  the existing motion, never start a second one. One driver per window, and it owns its
  link.
- **Vendored code and `-O`.** The build compiles with optimisation and no package manager;
  the vendored file has to be plain enough to survive that, which is why only the physics
  is taken.
- **A spring that feels wrong at the numbers given.** MacPaw tuned theirs for a character
  that follows a cursor; if 7.5 / 0.5 reads as too loose for a 200-point disc, the
  constants are one place and the criteria above are the test — change the numbers, not the
  design.

## Done means

The puck is dragged into a corner and let go, and it settles there like something with
weight; it is grabbed again mid-slide and does not flinch; it turns quietly while the
meeting is being transcribed and is perfectly still the rest of the time; and none of it
costs anything measurable when nobody is touching it.

## Implementation record

The physics is vendored at CocoaSprings commit
`54ecdecad92c447d977a6623a03113f441b880e3`, preserving the three model files, their
headers, the two point operators, and MIT text. Both Swift inputs and the app plist
participate in the rebuild stamp. The executable now has an explicit `@main` entry point
so Swift can compile multiple sources; the remote typecheck includes the physics alone
and the combined app.

The puck uses one display-link driver per dock, accumulating monotonic elapsed time at
1/120 s. Redirects retain velocity; a grip interrupts before measuring its pointer offset.
An initially offscreen grip has enough drag room to avoid jumping into the screen.
Tuck safety predicts the trajectory with incoming velocity, adjusts the destination if
needed, and bounds individual steps so at least half the sliver stays visible. Missing
display frames trigger a one-shot deadline; settling, hiding and teardown invalidate
motion callbacks. Alpha still fades separately, the island keeps its ease-out, and exit
fades remain unchanged.

The processing rim is a Core Animation arc, static under Reduce Motion and removed outside
processing or when hidden. Preview's Processing scenario opens the puck directly, and
Replay Arrival can replay the puck's spring.

### Edge-tab morph

At a dock edge the puck no longer reads as a circle disappearing behind the display. Its
eight-cubic body contour turns into a tab: two concave shoulders leave the visible-frame
boundary tangentially, meet a rounded inward tip, and join a rounded lobe that remains
offscreen. The terminal contour is shown in [`puck-edge-morph.svg`](puck-edge-morph.svg).

The dock derives the contour's amount and its boundary anchors from the frame actually
drawn on each existing spring display-link tick. A spring overshoot therefore keeps both
shoulders joined to the edge, and a mid-slide grab reverses from the outline that was
visible under the pointer. The fill, shadow path, and processing rim use that one path.
The ordinary controls fade during the morph and are hidden before the tab is reached, so
an invisible button cannot take a click; the remaining sliver still belongs to the puck's
drag and hover view. Reduce Motion applies the appropriate end outline with the existing
direct frame placement and starts no animation or timer.

### Automated verification

At commits `81d0b7b` through `d4a2cbe`, the remote Mac typechecked `capture.swift`,
`springs.swift` alone, and the combined recorder sources. Its optimized full-app compile
test passed without launching the app. The production-solver harness also passed in 3.68 s:
it checks 180, 184, and 200 point slides at 60 and 120 Hz against the 700 ms, one-visible-
overshoot, 4–12 point, and five-percent criteria, plus momentum-preserving redirects and
all four bounded-edge predictions at 1,800 pt/s incoming velocity. The remote runner uses
quiet pytest output and captures the harness's per-case numeric lines, so those individual
settle times are not available in its log; this is automated constraint evidence, not a
replacement for the visual measurement below.

### Human acceptance still required

Step 0 was not performed: this checkout is on Linux, and no before-motion captures were
provided. Neither remote compilation nor a physics test supplies a visual comparison.
The remote workflow never launches the recorder or grants recording permissions.

On the Mac, use `VTN_SETUP_LAUNCH=off ./run.sh menubar --preview` from the updated
development checkout to check the
original nine criteria: tuck/peek on all four edges and corners, grab during both motions,
compare 60/120 Hz captures, open Processing directly, switch away, replay arrivals, and
repeat with Reduce Motion enabled before opening the window. Check the sliver throughout
redirects, not only ordinary tucks. Use `powermetrics` on untouched free, tucked and hidden
windows to establish the energy result; callback teardown alone is not that measurement.
An actual setup after changing only `springs.swift` must still be observed on the Mac to
confirm the source-stamp rebuild end to end.

For the edge-tab addition, also check that the shoulders visibly meet each of the four
screen edges rather than looking like a clipped disc, that the processing rim follows the
tab boundary, and that grabbing during either direction reverses the contour without a
shape jump. The remote compiler can establish source validity only; this visual join still
needs the preview on a real display.
