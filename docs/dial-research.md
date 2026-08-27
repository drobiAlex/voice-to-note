# The dial: how it will be built, and the code that proves each choice

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
