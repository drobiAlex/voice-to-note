# The floating recorder: how it is built, and the code that proves each choice

> **Where this stands.** Part one designed a radial timer dial; it shipped, and was
> then replaced by the recorder puck — a record/stop button with the Microphone,
> Sound source and Project pickers — because a control kept in a corner should be the
> recorder, not a timer. Part one's window, drawing and energy decisions (1, 2, 5–8)
> carry over to the puck unchanged; its dial-specific ones (3, 4) are history. Part two
> — carrying the window anywhere and tucking it into an edge — is what the puck rides
> on today, verbatim.

The target is the floating radial timer seen hanging off the menu bar: a borderless disc
under the status item, a ring of ticks, a lit arc that grows as the pointer is dragged
around it, a centred readout ("8 MIN"). This note fixes the design before any of it is
written, and every choice points at shipped code or an Apple document — nothing here is
invented. Snippets were fetched from the cited files, not recalled.

## What `menubar.swift` already has

The recording panel already does the panel half of this, and the dial reuses it as-is:
a `.borderless, .nonactivatingPanel` `NSPanel` at `.statusBar` level, `isOpaque = false`,
clear background, `.canJoinAllSpaces, .fullScreenAuxiliary`, `orderFrontRegardless`,
placement under the status item clamped to the screen (`place(_:)`), outside-click
closing through a global + local `NSEvent` monitor pair (`watchForClicks`), Reduce Motion
read once per showing, and a layer-backed Core Animation pulse (`RecordingDotView`).
What is new is the dial view, the drag, and animating only while somebody drags.

## Decisions

### 1. Window: keep the panel, `hasShadow = false`, shape is cosmetic

Nobody in the genre shapes the window; the panel is a transparent rectangle and the
disc is drawn (or masked) inside it. Loop's radial menu — the closest shipped thing to
this control — is exactly that
(`Loop/Window Action Indicators/Radial Menu/RadialMenuController.swift` L35–48):

```swift
let panel = ActivePanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
panel.collectionBehavior = .canJoinAllSpaces
panel.hasShadow = false
panel.backgroundColor = .clear
```

Every project checked turns the window shadow off (Loop, Ice `IceBar.swift` L36,
boring.notch `BoringNotchWindow.swift` L40, DynamicNotchKit `DynamicNotchPanel.swift`
L23). Apple's reason: on a transparent window the shadow shape is computed from the
pixels and "the window shadow is invalidated, forcing the window shadow to be
recomputed" whenever it changes (`NSWindow.hasShadow` docs). The dial gets a
`CALayer.shadowPath` on its own disc instead — "for layers whose shape never changes
or rarely changes, this greatly improves performance" (Core Animation guide,
*Improving Animation Performance*). The existing panel sets `hasShadow = true`; the
dial's panel will not.

Ice's forgiveness inset is worth copying for the hover/close test
(`Ice/Events/EventManager.swift` L524–536): `panel.frame.insetBy(dx: -10, dy: -10)`.

### 2. Drawing: `CAShapeLayer`s, paths built once, no `draw(_:)` per frame

Three ways were found in the wild:

| approach | seen in | verdict |
| --- | --- | --- |
| `draw(_:)` + Core Graphics, everything per change | MSCircularSlider, HGCircularSlider | simplest, redraws the whole disc on every drag event |
| one `CAShapeLayer` per element, paths rebuilt | SHMultiSlider `SHKnobRing.swift` L58–62 | fine, but rebuilds the arc path each event |
| two `CAShapeLayer`s, path built once, animate `strokeStart`/`strokeEnd` | DuckDuckGo `macOS/DuckDuckGo/Common/View/AppKit/CircularProgressView.swift` L31–32 | chosen |

Apple's rule behind the choice: "Every time your app updates (or 'draws') content to
screen, it requires the CPU, GPU, and screen to be active" and "Draw to smaller portions
of the screen — only the portions that are changing" (*Energy Efficiency Guide*, Using
Efficient Graphics). `strokeEnd`, `transform` and `opacity` are composited by the render
server without running any drawing code; the lit arc therefore costs zero redraws.

The ring of ticks is one `CAReplicatorLayer` — one tick sublayer, `instanceCount = N`,
`instanceTransform` a rotation — as What-Time does for a clock face
(`What Time/ClockView/Layers/ClockTicksLayer.swift`):

```swift
instanceCount = 12
instanceTransform = CATransform3DMakeRotation(30 * .pi / 180, 0, 0, 1)
```

A replicator cannot light *some* instances, so the lit ticks are a second replicator in
the accent colour, masked by the `strokeEnd` arc. That is the same "gradient fill masked
by an animatable arc" composition Loop uses for its highlighted wedge
(`RadialMenuView.swift` L96–115, `DirectionSelectorCircleSegment.swift` L19–38).

Set `layerContentsRedrawPolicy = .onSetNeedsDisplay` on the view: the AppKit default
`.duringViewResize` "produces correct but not optimal performance results" (NSView docs).
`drawsAsynchronously` and `shouldRasterize` stay off — Apple says to measure before the
first, and the second re-rasterises whenever content changes, a loss for a dial that
moves.

### 3. Drag math: apply the shortest signed delta, never the absolute angle

The naive form — `atan2` of the pointer, mapped straight to a value — is what Nook's
`AngleDial.swift` does, and it is correct only for a 0–360° picker. For a bounded value
(minutes), it teleports when the drag crosses the seam. Two fixes ship:

- HGCircularSlider `CircularSliderHelper.swift` L235–261 computes the shortest angular
  distance from the *previous* angle (sign = direction) and adds only that to the value:

  ```swift
  private static func angle(from alpha: CGFloat, to beta: CGFloat) -> CGFloat {
      let halfValue = circleMaxValue/2
      let offset = alpha >= halfValue ? circleMaxValue - alpha : -alpha
      let offsetBeta = beta + offset
      if offsetBeta > halfValue { return offsetBeta - circleMaxValue } else { return offsetBeta }
  }
  ```

- JOCircularSlider `CircularSlider.swift` L296–316 does the same with a stored
  `lastTouchAngle` and `guard abs(delta) < 1 else { return }` to drop the one bogus
  frame at the seam.

The AppKit event path is SHMultiSlider `SHKnobRing.swift` L408–446:
`event.locationInWindow` → `convert(_:from: nil)` → `atan2(dy, dx)` about
`bounds.mid`, and a bare `return` when the pointer is in the dead gap so the value
does not jump. Detents fall out of `round()` on the mapped value; MSCircularSlider
`MSCircularSlider.swift` L882–924 shows snapping to the nearest marker on release.

During the drag the layer must follow the pointer with implicit animation *off*, or it
lags a quarter-second behind (SHMultiSlider L190–196):

```swift
CATransaction.begin(); CATransaction.setDisableActions(true)
toPointerLayer.setAffineTransform(CGAffineTransform(rotationAngle: newAngle))
CATransaction.commit()
```

A drag that leaves the panel keeps working: AppKit delivers `mouseDragged` to the view
that got `mouseDown` regardless, and Loop's `MouseInteractionObserver.swift` L110–125
documents the one edge case — the cursor pinning at a screen edge — which does not apply
to a dial that sits well inside the screen.

### 4. Detents: haptic on the step change, not on every event

Luminare `LuminareSlider.swift` L332–344 is the pattern: compute the stepped value,
and only if it *changed* —

```swift
if step != nil, didChange {
    NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .drawCompleted)
}
```

Loop gates the same call behind a user setting (`LoopManager.swift` L527–531). One
tick of the dial = one minute = one `.alignment` pulse. No sound: no project found
plays one.

### 5. Motion: animate only while something moves, and prefer implicit animations

No display link for the drag itself — the pointer is the clock, and each event moves
the layers with actions disabled (above). A display link exists only for the *settle*
after release, and if one is used it is `NSView.displayLink(target:selector:)`
(macOS 14+): `CVDisplayLink` is deprecated in 15.0 with that exact replacement named,
and a view-derived link "will not be invoked" when "the view is hidden, or not on any
display" (NSView docs), so a closed panel costs nothing. SwiftTerm's `FrameDriver`
(`Sources/SwiftTerm/Apple/FrameDriver.swift`) is the shipped shape: one link created
`isPaused = true`, unpaused for the animation, re-paused after a few idle ticks,
`invalidate()` only on teardown. Set `preferredFrameRateRange` explicitly — the default
is the display's maximum, 120 Hz on ProMotion.

In practice the settle should not need a link at all: a `CABasicAnimation` on
`strokeEnd` (DuckDuckGo L240–280) or a `CASpringAnimation` on `transform`
(Notchmeister `RadarEffect.swift` L296–302: `damping = 5, mass = 0.25`) runs on the
render server. Shipped spring values for the panel's arrival: boring.notch
`ContentView.swift` L122–129 — open `spring(response: 0.42, dampingFraction: 0.8)`,
close `spring(response: 0.45, dampingFraction: 1.0)` — bouncy out, critically damped
back so nothing overshoots into the menu bar. DynamicNotchKit starts the animation
*before* ordering the window front (`DynamicNotch.swift` L185–195, "this eliminates
stutter") and fades out over 0.15 s with `NSAnimationContext` (L317–331) — the same
technique `arrive`/`vanish` already use.

Reduce Motion: Apple's macOS wording is "avoid large animations, especially those that
simulate the third dimension" — the drag tracking stays (it is direct manipulation),
the spring settle and arrival growth go, as `arrive` already does.

### 6. Readout timer: one fire per minute boundary, not a 1 Hz ticker

"Forgetting to stop timers probably wastes more energy than anything else in OS X"
and "set the tolerance to at least 10 percent of the interval" (*Energy Efficiency
Guide*, Timers). A minutes readout schedules one `Timer` to the next boundary with a
few seconds' tolerance and reschedules on fire; it is invalidated the moment the panel
closes. The existing 1 Hz elapsed-time ticker shows seconds, so it is right as it is —
but it should gain `tolerance = 0.1`, which it lacks today.

### 7. Material: no blur under the dial

"If you need to use opacity, avoid using it over content that changes frequently.
Otherwise, energy cost is magnified, as both the background view and the translucent
view must be updated whenever content changes" (Using Efficient Graphics). The notch
genre agrees for its own reason — DynamicNotchKit's attached panel is literally
`.black` (`NotchView.swift` L56–77) and only its *floating* variant uses
`VisualEffectView(material: .popover, blendingMode: .behindWindow)` with a
`.quaternary` 1 pt border (`NotchlessView.swift` L27–37). The dial is an opaque
near-black disc with a hairline border; the glow on the lit ticks is a `CALayer`
shadow with an explicit `shadowPath`, or Notchmeister's radial `CAGradientLayer` with
cosine falloff (`GlowEffect.swift` L83–100) if a shadow reads too soft. Reduce
Transparency changes nothing because nothing is transparent.

### 8. Accessibility

Apple's own sample (`AccessibilityUIExamples/Slider/CustomSliderView.swift`): conform
to `NSAccessibilitySlider`, override `accessibilityValue()`, `accessibilityLabel()`,
`accessibilityPerformIncrement()` / `Decrement()`, and have arrow keys and VoiceOver
call the same `increment()` / `decrement()`. `acceptsFirstResponder = true`.

## What the HIG says, and what is knowingly traded

"Display a menu — not a popover — when people click your menu bar extra" (HIG, *The
menu bar*). Every app in this genre breaks that rule on purpose, and vtn already does
with the recording panel; the dial is the same trade. Obeyed: "Aim for brevity and
precision in feedback animations", "Let people cancel motion", "Avoid relying on the
presence of menu bar extras".

## How it is proven, on the Mac

- `sudo powermetrics --samplers tasks --show-process-energy -i 1000` — VTN Recorder's
  idle wakeups and energy impact must sit at zero with the dial open and untouched,
  and drop back to zero within a second of a drag ending.
- Activity Monitor's Energy and App Nap columns: the helper must nap when nothing moves.
- Instruments *Animation Hitches* during a drag: at or below 10 ms/s.
- Xcode Energy gauge's own acceptance line: "When users aren't interacting with your
  app, it should have zero energy impact."

None of this can run over the remote-build key (the recorder is never launched
remotely — `docs/remote-build.md`); it is a person's step at the Mac.

## Sources

Code: MrKai77/Loop · jordanbaird/Ice · TheBoredTeam/boring.notch · MrKai77/DynamicNotchKit ·
exelban/stats · chockenberry/Notchmeister · duckduckgo/apple-browsers ·
HamzaGhazouani/HGCircularSlider · ouraigua/JOCircularSlider · ThunderStruct/MSCircularSlider ·
Rexhits/SHMultiSlider · chiahsien/What-Time · mrkai77/Luminare · migueldeicaza/SwiftTerm ·
nook-browser/Nook · Apple AccessibilityUIExamples (mirror: Lax/Learn-iOS-Swift-by-Examples).

Apple: Energy Efficiency Guide for Mac Apps (Timers, Using Efficient Graphics, App Nap,
Monitoring Energy Usage) · Core Animation Programming Guide (Improving Animation
Performance) · `NSView.displayLink(target:selector:)` · `CADisplayLink.preferredFrameRateRange` ·
`NSWindow.hasShadow` · `NSView.layerContentsRedrawPolicy` · `NSWorkspace.accessibilityDisplayShouldReduceMotion` ·
WWDC23 10054, WWDC21 10147, WWDC22 10083 · HIG: The menu bar, Materials, Motion.

---

# Part two: a dial that goes anywhere and tucks into the edge

The second ask: drag the dial anywhere, drop it against a screen edge and it tucks in,
leaving a sliver; hover the sliver and it slides out. Two more researchers, the same rule
— shipped code or an Apple document behind every choice. **Loop's `Stashing/` module is
this feature end to end** (tuck, sliver, hover reveal, multi-display, persistence, ~800
lines across four files) and most choices below are its, verified in the source.

## Decisions

### 9. Dragging the window: `performDrag(with:)` from `mouseDown`, chosen by where the press landed

Two shipped ways. Maccy sets `isMovableByWindowBackground = true` on the panel and turns
it *off* while the pointer is over a control (`Maccy/Views/ToolbarView.swift` L48–52,
`.onHover { window.isMovableByWindowBackground = !inside }`). mini-player does it by hand
(`Mini Player/WindowMovingView.swift` L32–40):

```swift
override func mouseDown(with event: NSEvent) { window?.performDrag(with: event) }
override func acceptsFirstMouse(for event: NSEvent?) -> Bool { return true }
```

The dial already decides in `mouseDown` whether a press is on the ring, so the second
form is the natural one: press on the ring turns, press on the ground or margin calls
`performDrag`. `acceptsFirstMouse` is the load-bearing half on a non-activating panel
(already set). Maccy notes macOS 26 dropped gestures on views with no background and
paints `Color.white.opacity(0.001)` to keep hit-testing — the dial's disc is opaque, so
not an issue, but the margin around it is clear and must not be relied on for hits.

### 10. Edge detection on release: `screen.frame`, not `visibleFrame`, small threshold

Rectangle (`Snapping/SnappingManager.swift` L441–482) tests the *pointer* against
`screen.frame` — the snap must fire under the menu bar and Dock, which `visibleFrame`
excludes — with per-edge margins that default to **5 pt** (`Defaults.swift` L21–24). Loop
(`Core/WindowDragManager.swift` L245–265) insets the screen by a threshold that defaults
to **2 pt** and widens the top edge to half the menu bar's height. Both use a pointer
position, not the window frame. The dial does the same: on mouse-up after a move, if the
pointer is within **8 pt** of a screen edge, tuck to that edge. Apple confirms the
trigger is a band, not a line: `NSScreen.visibleFrame` docs — "The system uses a small
boundary area to determine when it displays the dock."

Loop documents the multi-display trap: `CGRect.contains` is half-open, so a pointer on
`maxX`/`maxY` is in no screen; use the screen the pointer is on with an inclusive test
(`NSScreen+Extensions.swift` L28–40).

### 11. Tucked = the full window moved off-screen, a sliver left; never shrunk

Loop `WindowFrameResolver.swift` L100–124, `getStashedFrame`:

```swift
case .left, .right:
    let maxPeekSize = frame.width * maxPeekPercent            // 20 %
    let clampedPeekSize = max(minPeekSize, min(peekSize, maxPeekSize))   // min 1 pt
    if action.stashEdge == .left {
        frame.origin.x = bounds.minX - frame.width + clampedPeekSize
    } else {
        frame.origin.x = bounds.maxX - clampedPeekSize
    }
```

Frame keeps its size; only the origin moves; the peek defaults to **20 pt** and is never
0, because a window with no on-screen pixels gets no events. AppKit will pull a titled
window back on screen (`constrainFrameRect(_:to:)` docs: "invoked automatically…
whenever a titled NSWindow object is placed onscreen") — the dial's panel is borderless,
and alt-tab-macos overrides it anyway to be safe (`src/switcher/PreviewPanel.swift`
L9–12: `override func constrainFrameRect(...) -> NSRect { frameRect }`). The dial
overrides it too.

The visible sliver is the disc's rim plus a few lit ticks: **22 pt** peek, so the round
edge and the green read as a tab. It is drawn at reduced alpha while tucked (window
`alphaValue`, one property, composited).

### 12. Hover reveal: a tracking area on the sliver, dwell then re-check, never `.mouseMoved`

Apple, *Event Architecture*: tracking rectangles "are a less expensive way of following
the mouse's location"; *Handling Mouse Events*: mouse-moved events "occur so frequently
that they can quickly flood the event-dispatch machinery, an NSWindow object by default
does not receive them". So: one `NSTrackingArea` with `[.mouseEnteredAndExited,
.activeAlways]` and **not** `.mouseMoved`, on the dial view (the sliver is part of it).
That is what boring.notch and DynamicNotchKit do for hover (SwiftUI `.onHover`,
tracking-area backed; neither has a `.mouseMoved` monitor or a hand-made tracking area —
boring.notch's three global monitors in `observers/DragDetector.swift` watch for a file
being dragged at the notch, not for the pointer). Ice uses a global monitor and stops
it when not needed; Loop uses a listen-only CGEvent tap that exists *only while something
is stashed* (`StashManager.swift` L228, L689) — the structural answer to idle cost. The
dial needs neither: the tracking area costs nothing between crossings.

Timings that shipped: boring.notch enter dwell **0.3 s**, leave grace **100 ms**
(`ContentView.swift` L513–558, one cancellable task slot); Ice `showOnHoverDelay` **0.2 s**
with a re-check "Make sure the mouse is still inside" after the sleep
(`Events/EventManager.swift`); the Dock's observed (undocumented) dwell is 0.2 s. Loop
hides only when the pointer is outside the revealed frame inset by **−15 pt**
(`StashManager.swift` L552–562) — the slop ring that stops flicker at the boundary. The
dial: enter dwell 0.2 s, re-check on fire; leave grace 0.4 s, re-check on fire, never
while a drag is in progress; the tracking rect is the disc inset by −15 pt.

### 13. The slide: `NSAnimationContext`, fixed duration, `animationBehavior = .none`

`setFrame(_:display:animate:)` scales its time by distance (`animationResizeTime` docs:
"time in seconds to resize by 150 pixels… 0.20 seconds") — wrong for a peek that should
take the same time from every edge. `NSAnimationContext` with `animator().setFrame` and a
fixed **0.22 s** ease-out, and `panel.animationBehavior = .none` so AppKit's own
order-front animation does not fight it (Maccy, Ice both set it). Reduce Motion: snap.
Apple's wording — "avoid large animations" — a window sliding 200 pt is one.

### 14. Remembering where it was: a screen-relative point, clamped on restore

`setFrameAutosaveName` is the built-in (stats `Settings.swift` L83–86, with a fallback
when `setFrameUsingName` returns false). Maccy stores a *fraction of the screen*
(`FloatingPanel.swift` L113–122) so a resolution change keeps the place, and clamps the
restored origin into `visibleFrame` (`PopupPosition.swift` L67–75). Loop stores only the
edge and recomputes geometry. The dial stores `{edge | free}` plus a fraction of the
screen's frame; on restore, a screen that is gone puts it back under the status item, and
`NSApplication.didChangeScreenParametersNotification` re-anchors a tucked dial (Ice closes
its bar on the same notification).

### 15. Hover is a shortcut, not the only way in

WWDC21 *Discoverable design*: "Use gestures as a shortcut, not a replacement… You should
still have a primary way to perform the same action that is clearly legible." Apple's
accessibility guide: "a user should be able to perform all your app's functions using the
keyboard alone." So the menu keeps *Record for…* (⌘T), which reveals a tucked dial
rather than opening a second one, and *Tuck Dial* / *Show Dial* appear in the menu while
one exists. Clicking the sliver also reveals — a click is legible where a hover is not.

## The state machine, as it will be coded

```
hanging ──drag off the status item──▶ free ──released within 8 pt of an edge──▶ tucked
   │                                    ▲                                        │ ▲
   │ click elsewhere closes             │ dragged away from the edge             │ │ leave + 0.4 s
   ▼                                    │                                        ▼ │
 closed ◀── start / ⌘T again / Quit ── free ◀──────────── peeking ◀── hover 0.2 s / click / ⌘T
```

- *hanging* is today's behaviour. Leaving it ends the outside-click closing and the
  status item highlight: a placed window stays until it is told to go.
- *tucked*: alpha 0.6, 22 pt showing, one tracking area, no timers.
- *peeking*: full disc over the edge, full alpha; a drag on the ring while peeking works
  and cancels the leave grace; a drag on the ground moves it (and un-tucks it).
- Pressing the centre starts the tape and closes the dial from any state.

## Cost, idle

Tucked or free and untouched: zero timers, zero monitors, one tracking area; the window
is ordered in (memory, not energy). The dwell/grace timers exist only between a crossing
and its re-check. Proof is the same `powermetrics --samplers tasks` line as part one.

## Sources, part two

Code: MrKai77/Loop (`Stashing/StashManager.swift`, `WindowFrameResolver.swift`,
`WindowDragManager.swift`) · rxhanson/Rectangle · p0deje/Maccy · TheBoredTeam/boring.notch ·
MrKai77/DynamicNotchKit · jordanbaird/Ice · lwouis/alt-tab-macos · exelban/stats ·
thompsonate/mini-player · iina/iina · mpv-player/mpv.

Apple: Event Architecture · Handling Mouse Events · Event Objects and Types · NSTrackingArea ·
`addGlobalMonitorForEvents` · Energy Efficiency Guide (Timers, Best Practices) ·
`constrainFrameRect(_:to:)` · Sizing and Placing Windows · `NSScreen.visibleFrame` ·
`didChangeScreenParametersNotification` · `animationResizeTime` · NSAnimationContext ·
`NSWindow.animationBehavior` · WWDC21 10126 Discoverable design · Accessibility
Programming Guide for OS X · `accessibilityDisplayShouldReduceMotion`.

One researcher wrote its hover-timing section before its own verification returned and
corrected itself afterwards; the numbers above (0.3 s / 100 ms, `ContentView.swift`
L513–558) were then read from the file and stand.

Not sourceable to Apple, flagged: the Dock's `autohide-delay` 0.2 s / `autohide-time-modifier`
0.5 (community-measured), current HIG hover wording (the HIG site is client-rendered), and
"a pure window move is cheaper than a resize" (sound inference, no Apple sentence).

---

# Part three: where the three choosers sit on the disc

The first cut ranged the choosers up the left arc — an angle-convention bug — and even
corrected, an arc of three needed grounding. Two more researchers; the numbers below are
theirs, and the shipped layout follows them exactly.

- **One middle, said with scale.** Apple never ranges peer choosers around a record
  button: QuickTime and ⇧⌘5 put one prominent Record beside one Options pop-up; Voice
  Memos shows Record alone (Apple support pages, HIG *Buttons*: "keep the number of
  prominent buttons to one or two"; "use style — not size — to distinguish" among peers,
  scale to subordinate). The record button grew to 80 pt; the choosers stay one size.
- **The lower arc, 55° apart, middle straight down.** Blender's pie menus place a third
  item south ("even with three items, the menu seems to still be 'in order' reading left
  to right"); Kurtenbach: on-axis directions are hit faster and more surely, so the
  most-used chooser (project) is at 270°. Three at 120° is rotational symmetry — four
  peers on a wheel (NN/g: "radial balance leads the eye to the center"), rejected.
- **38 pt drawn inside a 44 pt hit circle.** HIG: "a button needs a hit region of at
  least 44×44 pt" — with no exception for a pointer. Drawn-vs-hit decoupling gives the
  minimum without visual bulk; hit circles keep ~20 pt of air (WCAG 2.5.8's spacing rule
  and the HIG's 12 pt padding both cleared). Glyphs 18 pt SF Symbols.
- **State above, options below.** The caption (Record / elapsed / Processing …) moved
  over the button, leaving the lower hemisphere to the choosers — the grouping is done
  by proximity, not by borders (HIG *Layout*: group with negative space).
- Flagged for later: Hopkins found even counts beat odd in radial layouts, and Apple,
  Teams and Loom all fold microphone + sound source into one "audio" options surface. If
  a fourth chooser ever arrives, go to the compass (W/E/S, top reserved); if the disc
  ever feels busy, two satellites (audio · project) at 225°/315° is the researched
  fallback.

Sources: Callahan/Hopkins/Weiser/Shneiderman CHI '88 · Hopkins DDJ 1991 · Kurtenbach
1993 · Blender `interface.cc` + manual · MrKai77/Loop `RadialLayout.swift` · Apple AVCam
`MainToolbar.swift` · HIG Buttons / Accessibility / Layout / Pop-up buttons / Toolbars /
SF Symbols / Offering help · Apple support: QuickTime, ⇧⌘5, Voice Memos, Camera ·
NN/g visual-design principles & icon usability · WCAG 2.5.8 · asktog on Fitts.

---

# Part four: what Eney actually is, and what of it we can have

MacPaw's Eney was raised as the animation to aspire to. Two researchers went at it; the
first thing they found is that the premise needs correcting, and the second is that half
of what makes it good is already built here.

**It is a character, not an effect.** A pink circle with eyes, drawn by an engine MacPaw
wrote themselves — Maksym Mova, engineering manager: "developing a custom 3D engine that
powers our animated character — a core part of the user experience". Their senior
character producer, writing in Smashing Magazine: "we settled on a circular figure, as it
felt the most approachable … Its eyes are the main emotional connector — a key feature in
showing emotions without being cartoonish." Nothing gooey, no particles, no shader toy.

**It moves less than people remember.** From the same piece: "we worked to reduce Eney's
on-screen movement to make sure it wasn't distracting or excessive", and "when working on
a task, Eney's figure resembles a loading icon". The feel is a calm near-still idle, a
small vocabulary of expressions, and one rotational working state. A perpetually churning
blob is further from Eney, not closer.

**Its interaction model is the one this app already has.** 9to5Mac: "It lives on the side
of your Mac display (think something like Dynamic Island on iPhone — it integrates
seamlessly with the side bezel)", activating "when the cursor approaches". That is the
dock in part two — tuck to an edge, peek on hover — arrived at independently from Loop,
Ice and Apple's own event guidance.

## What transfers, given how this app is built

There is no package manifest here: `capture.swift` and `menubar.swift` are compiled by
plain `swiftc`, one file each. FluidGradient, Orb and CocoaSprings are therefore off as
*dependencies* — but each one's technique is system API underneath, and that is the part
worth taking.

1. **Springs instead of ease-out, on the window.** MacPaw open-sourced the motion half of
   Eney as CocoaSprings (MIT): damped springs whose whole trajectory is precomputed and
   handed to Core Animation — `CAKeyframeAnimation` on `position`, run by the render
   server at no main-thread cost. Their shipped defaults are `angularFrequency` 7.5 and
   `dampingRatio` 0.5. `CASpringAnimation` has been system API since 10.11, so the feel
   costs an import of nothing. This is the highest-value change: what people recognise as
   "the Eney feel" is settling, not decoration.
2. **A working state that turns.** "Resembles a loading icon" is one `CABasicAnimation`
   rotating a `CAShapeLayer` arc — the puck already has the disc and the rim to hang it on,
   and the menu bar mark already shimmers through processing.
3. **Idle stays still.** Not a compromise, the actual design: MacPaw's own instruction to
   themselves. It is also the only honest choice here — Apple: "if your app creates a
   status item that's present in the menu bar, your app is considered visible … as long as
   the menu bar is visible", so App Nap never engages and nothing pauses this app but this
   app.
4. **Optional, later: a fluid sheen while working.** FluidGradient's approach is radial
   `CAGradientLayer` blobs moved by `CASpringAnimation` — again render-server, and again
   no dependency needed to copy. Only while the tape rolls or notes are being made; never
   at rest, and never under a translucent layer (Apple: opacity "over content that changes
   frequently … energy cost is magnified").

What is *not* proposed: eyes, a face, a character. Eney is a companion that talks; this is
a recorder that gets out of the way. Borrowing its motion is sense; borrowing its
personality would be a costume.

Could not be verified, and so is not designed against: Eney's exact timings, easing,
palette, or what its listening and thinking states look like as distinct from working.

Sources: Smashing Magazine, "Digital design in the AI era" (O. Hrzhehorzhevskyi, MacPaw) ·
Setapp, "What is Eney" (M. Mova) · 9to5Mac 2025-05-22 and 2025-01-08 · AlternativeTo ·
MacPaw/CocoaSprings · Cindori/FluidGradient · metasidd/Orb · hackenbacker/Metaball ·
Apple: Energy Efficiency Guide, `Shader`, `TimelineView`, `NSView.displayLink`.
