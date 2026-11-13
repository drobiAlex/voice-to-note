# Menu bar recorder

`vtn menubar` opens the menu bar recorder: one click to record, pickers for
project and devices. `vtn record` is the same recorder from the terminal.

## `vtn record`

Records this Mac's meeting (system audio plus mic), transcribing it as it
goes, then finishes it. A live level meter per side shows a muted mic while it
can still be fixed (`--levels` for the raw numbers); `--output-device` and
`--input-device` pin devices.

## `vtn menubar`

Opens the menu bar recorder: one click to record, pickers for project and
devices. `--preview` clicks through every state it can be in on made-up sound,
to judge the look without recording anything.

## Previewing and using it

On a Mac, `vtn menubar --preview` checks the recorder without recording or
opening a note: choose Processing to see its elapsed stage, Cancelling
processing to inspect the queued cancellation state, and Note ready to inspect
the direct Open Note affordance. During a real recording, Stop changes to a
named processing stage and clock; Cancel keeps the captured audio and any
completed work. A puck tucked into an outer screen edge opens from a deliberate
tab click or a short dwell, while a press-and-drag still moves it.
