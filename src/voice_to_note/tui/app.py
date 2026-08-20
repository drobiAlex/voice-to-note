from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path, PurePath

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.validation import Function, Integer, Number, Validator
from textual.widgets import (
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    MarkdownViewer,
    Select,
    SelectionList,
    Static,
    Switch,
    TabbedContent,
    TabPane,
    TextArea,
)

from .. import config, services
from ..domain import Memo
from ..gateways import GatewayError
from ..storage.repository import Repository


class ConfirmDiscard(ModalScreen[bool]):
    """Stands between a stray Escape and somebody's unsaved writing."""

    BINDINGS = [("y,enter", "discard", "Discard"), ("n,escape", "keep", "Keep editing")]

    def compose(self) -> ComposeResult:
        """Asks the one question, in the one place it matters."""
        yield Static("Throw away your changes?  (y / n)", id="confirm-discard")

    def action_discard(self) -> None:
        """Lets the edit go."""
        self.dismiss(True)

    def action_keep(self) -> None:
        """Puts them back in the editor with their writing intact."""
        self.dismiss(False)


class NoteEditor(ModalScreen[None]):
    """A memo's notes, open for rewriting in the reader's own words."""

    BINDINGS = [("ctrl+s", "save", "Save"), ("escape", "cancel", "Cancel")]

    def __init__(self, markdown: str, store: Callable[[str], None]) -> None:
        """Opens on what the notes say now, saved or generated, holding the way
        to store them so a refused note can stay on screen to be fixed."""
        super().__init__()
        self.original = markdown
        self.store = store

    def compose(self) -> ComposeResult:
        """The note, and nothing else to be distracted by."""
        yield TextArea(self.original, id="editor")

    def on_mount(self) -> None:
        """Puts the cursor where the typing goes."""
        self.query_one("#editor", TextArea).focus()

    def action_save(self) -> None:
        """Stores the note, closing only once it has actually been stored: a
        refused note stays here with the writing still in it."""
        try:
            self.store(self.query_one("#editor", TextArea).text)
        except services.InvalidInput as refused:
            self.notify(str(refused), severity="warning")
            return
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Leaves, asking first if there is anything to lose."""
        if self.query_one("#editor", TextArea).text == self.original:
            self.dismiss(None)
        else:
            self.app.push_screen(ConfirmDiscard(), self._discarded)

    def _discarded(self, discard: bool | None) -> None:
        """Closes the editor only if they said to throw the writing away."""
        if discard:
            self.dismiss(None)


class MoveMemo(ModalScreen[None]):
    """Where a recording belongs. Projects are free text, so the screen offers
    the ones already in use without insisting on them."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, projects: list[tuple[str, int]], store: Callable[[str], None]) -> None:
        """Opens on the projects that exist and the way to file into one."""
        super().__init__()
        self.projects = projects
        self.store = store

    def compose(self) -> ComposeResult:
        """A line to type a project into, over the ones already going."""
        yield Input(placeholder="project", id="project-name")
        yield ListView(id="project-choices")

    def on_mount(self) -> None:
        """Ready to type, with the existing projects a keypress away."""
        choices = self.query_one("#project-choices", ListView)
        for name, _count in self.projects:
            choices.append(ListItem(Label(name), name=name))
        choices.index = 0
        self.query_one("#project-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Files the memo under whatever they typed."""
        self._move(event.value)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Files the memo under a project already in use."""
        # the screen behind this modal listens for list selections of its own
        event.stop()
        self._move(event.item.name or "")

    def _move(self, project: str) -> None:
        """Closes only once the memo has actually moved."""
        try:
            self.store(project)
        except services.InvalidInput as refused:
            self.notify(str(refused), severity="warning")
            return
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Leaves the memo where it was. A project name is one word, so there is
        nothing here worth interrupting somebody to confirm."""
        self.dismiss(None)


class RenameSpeaker(ModalScreen[None]):
    """Puts a real name to one of the voices in a recording."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self, speakers: dict[str, str], store: Callable[[str, str], None]
    ) -> None:
        """Opens on the voices this memo has and the way to name one."""
        super().__init__()
        self.speakers = speakers
        self.store = store

    def compose(self) -> ComposeResult:
        """The voices, and a line to name the highlighted one."""
        yield ListView(id="speaker-choices")
        yield Input(placeholder="name", id="speaker-name")

    def on_mount(self) -> None:
        """First voice picked out, cursor where the name goes."""
        choices = self.query_one("#speaker-choices", ListView)
        for label, name in self.speakers.items():
            choices.append(ListItem(Label(f"{label} — {name}"), name=label))
        choices.index = 0
        self.query_one("#speaker-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Names whichever voice is picked out."""
        chosen = self.query_one("#speaker-choices", ListView).highlighted_child
        if chosen is None:
            return
        self._rename(chosen.name or "", event.value)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Settles which voice is being named and moves on to typing the name,
        which is the half of the job the list cannot do."""
        # the screen behind this modal listens for list selections of its own
        event.stop()
        self.query_one("#speaker-name", Input).focus()

    def _rename(self, label: str, name: str) -> None:
        """Closes only once the voice has actually been named."""
        try:
            self.store(label, name)
        except services.InvalidInput as refused:
            self.notify(str(refused), severity="warning")
            return
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Leaves every voice named as it was."""
        self.dismiss(None)


class AudioTree(DirectoryTree):
    """A file tree with the noise taken out: folders to walk into, and the
    recordings worth walking to. Filtering is for reading only — what may
    actually be processed is settled when the path is submitted, so a format
    missing from this list can still be typed in by hand."""

    AUDIO_SUFFIXES = frozenset({".m4a", ".wav", ".mp3", ".qta", ".opus", ".caf", ".aac", ".flac", ".ogg"})

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        """Keeps the folders and the recordings, drops everything else."""
        return [
            path
            for path in paths
            if path.is_dir() or path.suffix.lower() in self.AUDIO_SUFFIXES
        ]


@dataclass(frozen=True)
class ImportOptions:
    """How one recording is brought in, past the file and the project it is
    filed under: how many voices to look for — left to a guess when it is not
    known — which note template shapes its extraction, and which of the
    optional pipeline stages actually run over it."""

    num_speakers: int | None
    template: str
    steps: frozenset[str]


# the steps a form left untouched runs, reproducing `vtn process`'s own
# default exactly: speakers guessed and detected, notes extracted, no repair
# pass unless it is asked for
_DEFAULT_STEPS = frozenset({"speakers", "notes"})


class ConfirmDuplicate(ModalScreen[bool]):
    """Stands between a recording already stored and the minutes of converting,
    transcribing and extracting it a second time would cost. What matched is
    when the recording was taped, not what the file is called, so the question
    names the memo it matched: the file being brought in is under a different
    name — which is exactly why nobody noticed — and only the stored one says
    what would be doubled. Bringing a second copy in is still allowed, since a
    re-import is sometimes the point."""

    BINDINGS = [
        ("y,enter", "process", "Process anyway"),
        ("n,escape", "skip", "Skip it"),
    ]

    def __init__(self, memo_id: int, filename: str) -> None:
        """Opens on the memo this recording is already stored as."""
        super().__init__()
        self.memo_id = memo_id
        self.filename = filename

    def compose(self) -> ComposeResult:
        """Says what it is already here as, then asks."""
        yield Static(
            f"memo {self.memo_id} — {self.filename} was recorded at the same moment;"
            " process this file anyway?  (y / n)",
            id="confirm-duplicate",
        )

    def action_process(self) -> None:
        """Brings the second copy in regardless."""
        self.dismiss(True)

    def action_skip(self) -> None:
        """Leaves the memo already stored as the only copy."""
        self.dismiss(False)


class ProcessMemo(ModalScreen[None]):
    """Everything about bringing one recording in, decided in one place: where
    the file is, what to file it under, how many voices to look for, which
    note template shapes its extraction, and which of the optional stages —
    speaker detection, transcript repair, note extraction — actually run.
    Every control past the two Inputs opens on the default that reproduces
    `vtn process`'s own, so pressing straight through, path and project typed
    and Enter struck, means the standard pipeline and nothing more has to be
    decided to bring a recording in.

    There are two ways to name the file because there are two ways people
    have it: pasted from a file manager, or somewhere they would have to go
    and find. Both end up in the same line, which is what the modal reads
    when it submits, alongside every other control on screen."""

    BINDINGS = [
        # a Select consumes its own enter to open its menu and a SelectionList
        # consumes its own space to toggle the option under the cursor, so
        # neither passes a bare enter through to on_input_submitted the way an
        # Input does. Priority is what lets ctrl+s start the run regardless of
        # which control the cursor is actually resting on.
        Binding("ctrl+s", "start", "Start", priority=True),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        root: Path,
        project: str,
        store: Callable[[str, str, ImportOptions], None],
        duplicate: Callable[[str], Memo | None],
    ) -> None:
        """Opens on the folder to browse from, the project being browsed, the
        way to bring a file in, and the way to ask whether it is already in."""
        super().__init__()
        self.root = root
        self.project = project
        self.store = store
        self.duplicate = duplicate

    def compose(self) -> ComposeResult:
        """Where the recording is, what to file it under, how it should be run
        through the pipeline — every one of those controls opening on the
        choice that reproduces today's default — and somewhere to go looking
        if the path is not already to hand."""
        yield Input(placeholder="path to a recording", id="source-path")
        yield Input(self.project, id="source-project")
        with Horizontal(id="speakers-row"):
            yield Switch(value=True, id="speakers-auto")
            yield Label("auto-detect speakers", id="speakers-auto-label")
            yield Input(placeholder="3", id="speakers-count", disabled=True)
        yield Select(
            [(name, name) for name in services.note_templates()],
            value="notes",
            id="note-template",
        )
        with Horizontal(id="steps-row"):
            yield Switch(value=False, id="custom-steps")
            yield Label("custom steps", id="custom-steps-label")
        step_list = SelectionList(
            *[(step, step, step in _DEFAULT_STEPS) for step in services.PROCESS_STEPS],
            id="step-list",
        )
        step_list.display = False
        yield step_list
        yield AudioTree(self.root, id="source-tree")

    def on_mount(self) -> None:
        """Cursor on the path, the one part nobody can guess for them."""
        self.query_one("#source-path", Input).focus()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        """Writes the chosen recording into the line and hands the cursor back
        to it. Picking is only half of it — the project is still there to check
        — and going through the line means a browsed path meets exactly the same
        refusals as a typed one."""
        self.query_one("#source-path", Input).value = str(event.path)
        self.query_one("#source-path", Input).focus()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """Keeps each switch's own control in step with it: auto-detect
        disables the speaker count beside it, since a number typed there would
        mean nothing next to the guess the pipeline is about to make instead;
        custom steps reveals the list of stages to choose between, since the
        two defaults need no picking from at all."""
        if event.switch.id == "speakers-auto":
            count = self.query_one("#speakers-count", Input)
            count.disabled = event.value
            if not event.value:
                count.focus()
        elif event.switch.id == "custom-steps":
            self.query_one("#step-list", SelectionList).display = event.value

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter from any line brings the recording in."""
        self._process()

    def action_start(self) -> None:
        """The same run a bare Enter starts from a line, reachable from a
        control that keeps its own Enter, Space or arrow keys for itself."""
        self._process()

    def _options(self) -> ImportOptions:
        """Reads the run-shaping choices off their own widgets: the pinned
        speaker count when auto-detect is off, refused here rather than after
        the modal has already closed on the typing; the note template picked;
        and which optional stages run, defaulting to the same two `vtn
        process` runs unless the step list has been opened and its own
        selection made."""
        auto = self.query_one("#speakers-auto", Switch).value
        num_speakers = (
            None
            if auto
            else services.speaker_count(self.query_one("#speakers-count", Input).value)
        )
        template = self.query_one("#note-template", Select).value
        if template is Select.NULL:
            template = "notes"
        if self.query_one("#custom-steps", Switch).value:
            steps = frozenset(self.query_one("#step-list", SelectionList).selected)
        else:
            steps = _DEFAULT_STEPS
        return ImportOptions(num_speakers=num_speakers, template=str(template), steps=steps)

    def _process(self) -> None:
        """Puts a recording the database already holds back to the person before
        anything is converted, since bringing one in twice costs minutes and
        leaves two memos of the same conversation to keep apart afterwards. The
        question goes up over this form rather than in place of it: saying no to
        a second copy is not saying to throw away everything typed here, and the
        next thing somebody does is usually to pick a different file."""
        stored = self.duplicate(self.query_one("#source-path", Input).value)
        if stored is None:
            self._send()
        else:
            self.app.push_screen(
                ConfirmDuplicate(stored.id, stored.filename), self._duplicate_answered
            )

    def _duplicate_answered(self, agreed: bool | None) -> None:
        """Sends the recording off only once a second copy is what they said
        they wanted."""
        if agreed:
            self._send()

    def _send(self) -> None:
        """Closes only once the recording has actually been sent off, so a
        path with a typo in it — or a speaker count that isn't one — can be
        corrected where it was typed."""
        try:
            options = self._options()
            self.store(
                self.query_one("#source-path", Input).value,
                self.query_one("#source-project", Input).value,
                options,
            )
        except services.InvalidInput as refused:
            self.notify(str(refused), severity="warning")
            return
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Brings nothing in."""
        self.dismiss(None)


class AskQuestion(ModalScreen[None]):
    """A question to put to one recording. The answer is read once and kept
    nowhere, the same as `vtn ask` at the command line."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, store: Callable[[str], None]) -> None:
        """Opens on the way to put a question."""
        super().__init__()
        self.store = store

    def compose(self) -> ComposeResult:
        """A line to type a question into."""
        yield Input(placeholder="question", id="ask-question")

    def on_mount(self) -> None:
        """Puts the cursor where the question goes."""
        self.query_one("#ask-question", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Puts whatever they typed to the recording."""
        self._ask(event.value)

    def _ask(self, asked: str) -> None:
        """Closes only once the question has actually gone off. A blank one is
        refused here, while the modal is still open with the typing in it."""
        try:
            self.store(asked)
        except services.InvalidInput as refused:
            self.notify(str(refused), severity="warning")
            return
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Asks nothing at all."""
        self.dismiss(None)


class DiarizeSpeakers(ModalScreen[None]):
    """How many voices to look for in a redo of speaker detection, left to a
    guess when that count is not known."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, store: Callable[[str], None]) -> None:
        """Opens on the way to run the pass with a count pinned or left loose."""
        super().__init__()
        self.store = store

    def compose(self) -> ComposeResult:
        """A line to type a speaker count into, guessed when left blank."""
        yield Input(placeholder="auto", id="speaker-count")

    def on_mount(self) -> None:
        """Puts the cursor where the count goes."""
        self.query_one("#speaker-count", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Runs the pass with whatever they typed."""
        self._submit(event.value)

    def _submit(self, typed: str) -> None:
        """Closes only once the pass has actually started."""
        try:
            self.store(typed)
        except services.InvalidInput as refused:
            self.notify(str(refused), severity="warning")
            return
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Leaves the memo diarized as it was, running nothing."""
        self.dismiss(None)


class Answer(ModalScreen[None]):
    """What one recording had to say to one question."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, memo_id: int, asked: str, answer: str) -> None:
        """Opens on the answer, carrying the question it answers: an answer
        arrives after a wait and outlives the modal that asked for it, so an
        unlabelled paragraph is one nobody can place."""
        super().__init__()
        self.text = f"**memo {memo_id} — {asked}**\n\n{answer}"

    def compose(self) -> ComposeResult:
        """The question and its answer, with room to scroll a long one."""
        with VerticalScroll():
            yield Markdown(self.text, id="answer")

    def action_close(self) -> None:
        """Puts the memo back in front of them."""
        self.dismiss(None)


class ConfirmForce(ModalScreen[bool]):
    """Stands between one keypress and an afternoon of somebody's writing. The
    model's notes would go straight over a note written by hand."""

    BINDINGS = [("y,enter", "force", "Overwrite"), ("n,escape", "keep", "Keep the note")]

    def compose(self) -> ComposeResult:
        """Says what would be lost, then asks."""
        yield Static(
            "Extracting again replaces the note you wrote.  (y / n)", id="confirm-force"
        )

    def action_force(self) -> None:
        """Lets the extraction go over the edit."""
        self.dismiss(True)

    def action_keep(self) -> None:
        """Leaves the note as they wrote it, and runs nothing."""
        self.dismiss(False)


class TagSearch(ModalScreen[None]):
    """A tag to go looking for. Tags come from the notes an extraction wrote, so
    this reaches across every project at once, which the sidebar cannot."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, store: Callable[[str], None]) -> None:
        """Opens on the way to run a search."""
        super().__init__()
        self.store = store

    def compose(self) -> ComposeResult:
        """A line to type a tag into."""
        yield Input(placeholder="tag", id="tag-search")

    def on_mount(self) -> None:
        """Puts the cursor where the tag goes."""
        self.query_one("#tag-search", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Looks for whatever they typed."""
        self._search(event.value)

    def _search(self, tag: str) -> None:
        """Closes only once the search has actually run."""
        try:
            self.store(tag)
        except services.InvalidInput as refused:
            self.notify(str(refused), severity="warning")
            return
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Leaves the list showing whatever it was showing."""
        self.dismiss(None)


class MemoDetails(ModalScreen[None]):
    """What state one memo is in, for the questions the list and the panes do
    not answer: how long it is, when it last changed, whether it has been
    repaired or written over."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, facts: str) -> None:
        """Opens on the lines services laid out. The screen adds no labels of its
        own, so what it calls a memo's state cannot drift from the command line."""
        super().__init__()
        self.facts = facts

    def compose(self) -> ComposeResult:
        """The lines, and nothing to do to them."""
        yield Static(self.facts, id="memo-info")

    def action_close(self) -> None:
        """Puts the memo back in front of them."""
        self.dismiss(None)


class RenameMemo(ModalScreen[None]):
    """A memo's name, open for correcting. It opens on the name in use, which
    for most memos is whatever the recorder called the file: renaming one is
    putting something readable where a date and a number were."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, filename: str, store: Callable[[str], None]) -> None:
        """Opens on the name the memo carries and the way to change it."""
        super().__init__()
        self.filename = filename
        self.store = store

    def compose(self) -> ComposeResult:
        """The name, ready to be typed over."""
        yield Input(self.filename, id="memo-rename")

    def on_mount(self) -> None:
        """Puts the cursor in the name."""
        self.query_one("#memo-rename", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Renames the memo to whatever they left on the line."""
        self._rename(event.value)

    def _rename(self, new: str) -> None:
        """Closes only once the memo has actually been renamed: a name that was
        refused stays on the line to be fixed rather than being thrown away."""
        try:
            self.store(new)
        except services.InvalidInput as refused:
            self.notify(str(refused), severity="warning")
            return
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Leaves the memo named as it was."""
        self.dismiss(None)


class ConfirmDelete(ModalScreen[bool]):
    """Stands between a stray keypress and a memo nobody can get back. The
    transcript, the notes and the named voices go with it, so the question
    names the memo it is about: the row under the cursor is not always the one
    somebody thinks they are pointing at."""

    BINDINGS = [("y,enter", "delete", "Delete it"), ("n,escape", "keep", "Keep it")]

    def __init__(self, filename: str) -> None:
        """Opens on the memo about to go."""
        super().__init__()
        self.filename = filename

    def compose(self) -> ComposeResult:
        """Says what goes, then asks."""
        yield Static(
            f"Delete memo — {self.filename}? It cannot be brought back.  (y / n)",
            id="confirm-delete",
        )

    def action_delete(self) -> None:
        """Lets the memo go."""
        self.dismiss(True)

    def action_keep(self) -> None:
        """Leaves the memo where it is."""
        self.dismiss(False)


class RenameProject(ModalScreen[None]):
    """A project's name, open for correcting. It opens on the name the project
    has now, because renaming one is usually fixing it rather than replacing it."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, project: str, store: Callable[[str, str], None]) -> None:
        """Opens on the name in use and the way to change it."""
        super().__init__()
        self.project = project
        self.store = store

    def compose(self) -> ComposeResult:
        """The name, ready to be typed over."""
        yield Input(self.project, id="project-rename")

    def on_mount(self) -> None:
        """Puts the cursor in the name."""
        self.query_one("#project-rename", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Renames the project to whatever they left on the line."""
        self._rename(event.value)

    def _rename(self, new: str) -> None:
        """Closes only once the project has actually been renamed."""
        try:
            self.store(self.project, new)
        except services.InvalidInput as refused:
            self.notify(str(refused), severity="warning")
            return
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Leaves the project named as it was."""
        self.dismiss(None)


class ConfirmRemove(ModalScreen[bool]):
    """Stands between a stray keypress and every memo in a project moving at
    once. Nothing is deleted, but a bulk move is still a lot to undo by hand."""

    BINDINGS = [("y,enter", "remove", "Empty it"), ("n,escape", "keep", "Keep it")]

    def __init__(self, project: str) -> None:
        """Opens on the project about to be emptied."""
        super().__init__()
        self.project = project

    def compose(self) -> ComposeResult:
        """Says what moves and where to, then asks."""
        yield Static(
            f"Move every memo in {self.project} into other?  (y / n)",
            id="confirm-remove",
        )

    def action_remove(self) -> None:
        """Lets the refiling go ahead."""
        self.dismiss(True)

    def action_keep(self) -> None:
        """Leaves the project with its memos in it."""
        self.dismiss(False)


class ConfirmResetAll(ModalScreen[bool]):
    """Stands between a stray keypress and every setting in vtn.toml being
    cleared at once — the one settings-screen action that touches all of them
    together rather than the row under the cursor."""

    BINDINGS = [("y,enter", "reset", "Reset all"), ("n,escape", "keep", "Keep them")]

    def compose(self) -> ComposeResult:
        """Says what is about to be cleared, then asks."""
        yield Static(
            "Reset every setting to its default?  (y / n)", id="confirm-reset-all"
        )

    def action_reset(self) -> None:
        """Lets the reset go ahead."""
        self.dismiss(True)

    def action_keep(self) -> None:
        """Leaves every setting as it was."""
        self.dismiss(False)


# the check run against a value as it is typed, keyed by the registry's kind
# for that setting. Kinds with no entry here — choice, multichoice, and a
# "text" setting whose choices come from this machine rather than the
# registry — take a picker instead: a Select or a SelectionList cannot hold a
# value there would ever be anything here to reject.
_KIND_VALIDATORS: dict[str, Callable[[], list[Validator]]] = {
    "int": lambda: [Integer()],
    "float01": lambda: [Number(minimum=0, maximum=1)],
    "url": lambda: [
        Function(lambda v: v.startswith(("http://", "https://")), "must be an http(s) URL")
    ],
}


def _validators(kind: str) -> list[Validator]:
    """The checks a value must pass before this kind of setting will let it be
    written, empty for a kind whose shape a typed widget will enforce instead."""
    build = _KIND_VALIDATORS.get(kind)
    return build() if build else []


class EditSetting(ModalScreen[None]):
    """One setting's value, open to be changed: typed over for a setting whose
    shape is any text, picked from a menu for one whose values are known ahead
    of time or read off this machine, or turned auto on and off for the one
    setting a guess can stand in for. Opens on what is actually in effect now,
    what it does, its default, and where the value came from — vtn.toml, the
    environment, or the setting's own default — checking what is typed against
    the shape the registry says this setting takes.

    A Select and a SelectionList both consume their own enter — one to open
    its menu, the other to toggle the highlighted option — so neither can
    submit the way a typed line does through on_input_submitted: a Select
    saves the moment it changes, since picking one value out of a menu is
    already the whole decision, while a SelectionList and the num_speakers
    switch save through ctrl+s, the same key NoteEditor already uses to
    confirm something a bare enter cannot."""

    # a bare "d" would never reach this screen: the Input focused below
    # consumes every printable key itself, the same reason NoteEditor binds
    # save to ctrl+s rather than a bare "s". Priority is what lets it pre-empt
    # the Input's own binding on the same chord, ctrl+d for delete-right.
    BINDINGS = [
        Binding("ctrl+d", "restore_default", "Restore default", priority=True),
        ("ctrl+s", "save", "Save"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        key: str,
        current: str,
        source: str,
        store: Callable[[str, str], None],
        restore: Callable[[str], None],
    ) -> None:
        """Opens on one setting's current value, where it came from, and the
        two ways to leave it changed: written over, or dropped to default."""
        super().__init__()
        self.key = key
        self.current = current
        self.source = source
        self.store = store
        self.restore = restore
        self.setting = services.setting_info(key)
        # fetched once on open rather than on every redraw: the two dynamic
        # keys cost a network call or a directory listing, and a picker
        # already open has nothing left to gain from asking again
        self.choices = services.setting_choices(key)

    def compose(self) -> ComposeResult:
        """What the setting is called, what it does, its default and where the
        value in effect now came from, the control it is actually edited
        through, and how to leave. An env override is called out on its own
        line: without it, an edit here would look like it took hold when
        nothing downstream will ever read it."""
        yield Static(self.key, id="setting-title")
        yield Static(self.setting.doc, id="setting-doc")
        yield Static(
            f"default: {self.setting.default}   current source: {self.source}",
            id="setting-meta",
        )
        if self.source == "env":
            yield Static(
                f"env override active — VTN_{self.key.upper()} is set and this edit"
                " won't take effect until it's unset",
                id="setting-env-warning",
            )
        yield from self._value_widgets()
        yield Static(self._hint(), id="setting-hint")

    def _value_widgets(self) -> ComposeResult:
        """The one control this setting is actually edited through: a menu
        when its values are fixed by the registry or read off this machine, a
        switch beside a count for the one setting a guess can stand in for,
        and a typed line for everything else."""
        if self.setting.kind == "multichoice":
            chosen = {c.strip() for c in self.current.split(",") if c.strip()}
            yield SelectionList(
                *[(c, c, c in chosen) for c in self.choices], id="setting-value"
            )
            return
        if self.key == "num_speakers":
            auto = self.current == "-1"
            with Horizontal(id="setting-auto-row"):
                yield Switch(value=auto, id="setting-auto")
                yield Label("auto-detect", id="setting-auto-label")
            yield Input(
                "" if auto else self.current,
                id="setting-value",
                disabled=auto,
                validators=[Integer(minimum=1)],
            )
            return
        if self.setting.kind == "choice" or self.choices:
            yield Select(
                [(c, c) for c in self.choices],
                value=self.current if self.current in self.choices else Select.NULL,
                allow_blank=self.current not in self.choices,
                id="setting-value",
            )
            return
        yield Input(
            self.current, id="setting-value", validators=_validators(self.setting.kind)
        )

    def _hint(self) -> str:
        """What this control is actually confirmed with, since a menu, a
        switch and a typed line are none of them driven by the same key."""
        if self.setting.kind == "multichoice":
            return "space toggle · ctrl+s apply · ctrl+d restore default · esc cancel"
        if self.key == "num_speakers":
            return "enter save · ctrl+s save · ctrl+d restore default · esc cancel"
        if self.setting.kind == "choice" or self.choices:
            return "enter pick · ctrl+d restore default · esc cancel"
        return "enter save · ctrl+d restore default · esc cancel"

    def on_mount(self) -> None:
        """Puts the cursor on whichever control actually takes typing: the
        value itself for most kinds, or — when num_speakers opens already on
        auto — the switch that turns the count back on, since the line beside
        it is disabled and cannot be focused at all."""
        if self.key == "num_speakers" and self.query_one("#setting-value", Input).disabled:
            self.query_one("#setting-auto", Switch).focus()
        else:
            self.query_one("#setting-value").focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Writes whatever they left on the line, unless it fails the check
        for what this setting's kind can even hold — refused here, the same
        as config_set would refuse it, but before it ever costs a write."""
        result = event.validation_result
        if result is not None and not result.is_valid:
            self.notify("; ".join(result.failure_descriptions), severity="warning")
            return
        self._submit(event.value)

    def on_select_changed(self, event: Select.Changed) -> None:
        """Saves the moment a menu is picked from: choosing one value out of
        a fixed list is already the whole decision, with nothing left to
        confirm. Guarded against the setting's own opening value, since a
        Select posts one of these the instant it mounts on a value that is
        not blank — without the guard every one of these modals would save
        and close itself before anyone had touched anything — and against the
        blank entry itself, which means nothing was actually chosen."""
        if event.select.id != "setting-value":
            return
        if event.value is Select.NULL or event.value == self.current:
            return
        self._submit(str(event.value))

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """Keeps the count in step with the switch: turned to auto it is
        disabled, since a number typed there would mean nothing next to the
        guess the pipeline is about to make instead."""
        if event.switch.id != "setting-auto":
            return
        count = self.query_one("#setting-value", Input)
        count.disabled = event.value
        if not event.value:
            count.focus()

    def action_save(self) -> None:
        """Confirms a SelectionList or the num_speakers switch, the two
        controls that consume their own enter — one to toggle the highlighted
        option, the other to flip itself — and so cannot submit the way a
        typed line or a Select already does on their own."""
        if self.setting.kind == "multichoice":
            selected = set(self.query_one("#setting-value", SelectionList).selected)
            # the registry's own order, not the order things were picked in:
            # a comma list this app writes is read back in this order, and a
            # picker's business is only ever which of them made it in
            ordered = [c for c in self.choices if c in selected]
            self._submit(",".join(ordered))
        elif self.key == "num_speakers":
            if self.query_one("#setting-auto", Switch).value:
                self._submit("-1")
            else:
                self._submit(self.query_one("#setting-value", Input).value)

    def _submit(self, typed: str) -> None:
        """Closes only once the setting has actually been written."""
        try:
            self.store(self.key, typed)
        except services.InvalidInput as refused:
            self.notify(str(refused), severity="warning")
            return
        self.dismiss(None)

    def action_restore_default(self) -> None:
        """Drops this setting back to its default, leaving whatever was typed
        over it behind."""
        self.restore(self.key)
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Leaves the setting as it was."""
        self.dismiss(None)


# a template's row is keyed like this rather than by its bare name, so a
# selection or a restore on the settings table can tell the two kinds of row
# apart without a second lookup back into either registry
_TEMPLATE_KEY_PREFIX = "template:"

# how a row's key and source cells are picked out once they no longer match
# what a fresh install would show — a setting written over its default, or a
# template shadowed by a saved override
_DIVERGED_STYLE = "bold yellow"


class SettingsScreen(ModalScreen[None]):
    """Every setting `vtn config` lists and every prompt template `vtn
    template` lists, open to be read from inside the app: each one's key, the
    value actually in effect, where that value came from, and what it does. A
    setting can be changed here; a template can only be reset, since it lives
    on disk as a file meant to be edited outside the app."""

    BINDINGS = [
        ("u", "restore_default", "Restore default"),
        ("R", "reset_all", "Reset all"),
        ("escape", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        """A table of every setting and template, one row each."""
        yield DataTable(id="settings", cursor_type="row")

    def on_mount(self) -> None:
        """Lays out the columns once, since only the rows change from here on,
        and draws the settings and templates as they stand right now."""
        table = self.query_one("#settings", DataTable)
        for column in ("key", "value", "source", "doc"):
            table.add_column(column, key=column)
        self._refresh()
        table.focus()

    def _refresh(self) -> None:
        """Redraws every row from what is actually in effect now, each one
        keyed by its own name — a template's prefixed, so the two registries
        cannot collide — so the row under the cursor can still be read back
        once the table under it has changed."""
        table = self.query_one("#settings", DataTable)
        table.clear()
        for key, value, source, doc in services.config_rows():
            self._add_row(table, key, value, source, doc, diverged=source != "default")
        for name, state, source, doc in services.template_infos():
            self._add_row(
                table, _TEMPLATE_KEY_PREFIX + name, state, source, doc,
                diverged=state != "built-in",
            )

    def _add_row(
        self, table: DataTable, key: str, value: str, source: str, doc: str, *, diverged: bool
    ) -> None:
        """Draws one row, its key and source styled the moment they diverge
        from a fresh install so a glance says what has been touched without
        opening anything. Styled through a Text object rather than markup in
        the cell string, so a plain str(cell) read back off the table — the
        way a row is picked back apart once selected — still comes back
        clean."""
        key_cell: str | Text = Text(key, style=_DIVERGED_STYLE) if diverged else key
        source_cell: str | Text = Text(source, style=_DIVERGED_STYLE) if diverged else source
        table.add_row(key_cell, value, source_cell, doc, key=key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Opens the highlighted setting for editing, or — on a template row,
        which has no in-app editor — says where its override file would go."""
        # the app behind this modal listens for row selections of its own,
        # on the memo table this screen has nothing to do with
        event.stop()
        key = str(event.row_key.value)
        if key.startswith(_TEMPLATE_KEY_PREFIX):
            name = key.removeprefix(_TEMPLATE_KEY_PREFIX)
            path = services.template_override_path(name)
            self.notify(f"templates are edited as files — write one at {path}")
            return
        table = self.query_one("#settings", DataTable)
        current = str(table.get_cell(event.row_key, "value"))
        source = str(table.get_cell(event.row_key, "source"))
        self.app.push_screen(EditSetting(key, current, source, self._set, self._unset))

    def _set(self, key: str, value: str) -> None:
        """Writes the setting, then redraws the table so its row says where
        the value now comes from."""
        self.notify(services.config_set(key, value))
        self._refresh()

    def _unset(self, key: str) -> None:
        """Drops one setting back to its default, then redraws the table so
        its row says where the value now comes from."""
        self.notify(services.config_unset(key))
        self._refresh()

    def _reset_template(self, name: str) -> None:
        """Deletes one template's saved override, then redraws the table so
        its row says built-in again."""
        self.notify(services.template_reset(name))
        self._refresh()

    def action_restore_default(self) -> None:
        """Drops the highlighted row back to its default: a setting through
        config_unset, a template through template_reset, whichever the
        cursor is actually resting on."""
        table = self.query_one("#settings", DataTable)
        if table.row_count == 0:
            return
        key = str(table.ordered_rows[table.cursor_row].key.value)
        if key.startswith(_TEMPLATE_KEY_PREFIX):
            self._reset_template(key.removeprefix(_TEMPLATE_KEY_PREFIX))
        else:
            self._unset(key)

    def action_reset_all(self) -> None:
        """Asks before clearing every setting at once — a single keystroke
        undoing every override in vtn.toml is worth the same confirmation
        emptying a project already gets, and for the same reason: nothing
        this wide should go through on a stray keypress."""
        self.app.push_screen(ConfirmResetAll(), self._reset_all_confirmed)

    def _reset_all_confirmed(self, confirmed: bool | None) -> None:
        """Clears vtn.toml once they have said to; leaves it alone otherwise."""
        if not confirmed:
            return
        self.notify(services.config_reset())
        self._refresh()

    def action_close(self) -> None:
        """Puts the app back in front of them."""
        self.dismiss(None)


# what the board calls each of its columns and the key each one is found again
# by. The first has no heading because the box under it needs none, and its key
# is spelled out rather than left blank so that a cell lookup reads as English
_TODO_COLUMNS = (
    ("", "done"),
    ("task", "task"),
    ("owner", "owner"),
    ("due", "deadline"),
    ("project", "project"),
    ("memo", "memo"),
)

# how a row says whether the task is finished, in the same two boxes the command
# line's to-do list draws
_DONE_BOX = "[x]"
_OPEN_BOX = "[ ]"

# what stands where the board would be when it has nothing to show
_NOTHING_TO_DO = "nothing to do"

# what the board calls itself while it is showing everything, and how it says
# each narrowing that is on: a board showing less than everything has to say so,
# since nothing else on screen tells a reader what is being left out
_BOARD_TITLE = "to-dos"
_SCOPE_SEPARATOR = " — "


class _OwnerScope(Enum):
    """The two narrowings of the board that name nobody: the tasks that are
    yours, and the ones no owner was named for at all. They stand as values of
    their own rather than as owner strings because an owner is whatever a
    transcript called somebody, and a person actually called "unassigned" would
    otherwise take the narrowing's place."""

    MINE = auto()
    UNASSIGNED = auto()


class TodoBoard(ModalScreen[int | None]):
    """Everything the memos have committed to, gathered off them and onto one
    screen, to be worked down and checked off. Opened from the app it spans
    every project on purpose — what a person owes is one list however many piles
    the recordings behind it are filed in — and opened against one memo it is
    that memo's own commitments, for checking off beside the note that states
    them. Either way it can be narrowed to one owner — a named one, yourself,
    or the nobody an extraction leaves behind when it could not tell who took
    something on — so that a list of everybody's work can be read as one
    person's.

    Closing it hands back the memo a to-do was committed in, when one was asked
    for, since acting on a task usually means going and reading what was
    actually said about it."""

    BINDINGS = [
        ("space", "toggle_done", "Done/undone"),
        ("a", "show_done", "Show done"),
        ("o", "filter_owner", "Owner"),
        # ahead of the table, which answers enter itself with a binding of its
        # own that says nothing in the footer: leaving the table to it would
        # hide from a reader the one key that gets them off the board
        Binding("enter", "open_memo", "Open memo", priority=True),
        ("escape", "close", "Close"),
    ]

    def __init__(self, repo: Repository, memo_id: int | None = None) -> None:
        """Reads and writes through the database the app is already holding
        open, so that a task checked off here is checked off everywhere the app
        counts them. Naming a memo narrows the board to what that one recording
        committed to; naming none is the whole list."""
        super().__init__()
        self.repo = repo
        # the memo this board is confined to, if it is confined to one
        self.memo_id = memo_id
        # what that memo is called, read once at mount: the heading names it
        # even while it has nothing outstanding for the rows to name it by
        self.memo_name = ""
        # whether the finished ones are on the board too. Off to begin with: the
        # board is what is left to do, and a list that grows forever as work gets
        # done stops being that
        self.show_done = False
        # whose tasks are on the board — a name, one of the two scopes that name
        # nobody, or nobody in particular, which is how it opens: the board is
        # what is owed before it is who owes it
        self.owner: str | _OwnerScope | None = None
        # the owners there are to cycle through, taken from the board as it
        # stands before any owner filter narrows it — read off the filtered rows
        # they would collapse to the one being filtered to
        self.owners: list[str] = []
        # what each row is standing for, keyed the way the row is. The table
        # holds cells, and a checked-off row has to be turned back into the
        # to-do's id and the memo behind it
        self.listed: dict[str, services.TodoRow] = {}

    def compose(self) -> ComposeResult:
        """The board under the line saying what is on it, and the line that
        stands in for the board while it is empty. Only ever one of those two is
        on screen.

        A footer of its own, unlike the modals that fill the terminal: a board of
        three rows leaves the app's own footer showing underneath it, offering
        keys that do nothing while this screen has the keyboard."""
        yield Static(id="board-scope")
        yield DataTable(id="todos", cursor_type="row")
        yield Static(_NOTHING_TO_DO, id="no-todos")
        yield Footer()

    def on_mount(self) -> None:
        """Lays out the columns once, since only the rows change from here on,
        and draws the to-dos as they stand right now."""
        table = self.query_one("#todos", DataTable)
        for label, key in _TODO_COLUMNS:
            table.add_column(label, key=key)
        if self.memo_id is not None:
            self.memo_name = services.require_memo(self.repo, self.memo_id).filename
        self._refresh()

    def _refresh(self) -> None:
        """Redraws every row from what is outstanding now, leaving the cursor on
        the same line of the board it was on — clamped, because a row checked off
        while the finished ones are hidden takes its line with it, and the cursor
        should land on the next task rather than at the top again.

        Every cell goes in as a Text rather than a string: a table renders a
        string as markup, which would swallow the box in the first column whole
        and eat any square bracket a transcript happened to put in a task."""
        table = self.query_one("#todos", DataTable)
        at = table.cursor_row
        table.clear()
        self.listed = {}
        for row in self._rows():
            key = str(row.id)
            self.listed[key] = row
            table.add_row(
                Text(_DONE_BOX if row.done else _OPEN_BOX),
                Text(row.task),
                Text(row.owner),
                Text(row.deadline),
                Text(row.project),
                Text(row.memo),
                key=key,
            )
        self.query_one("#board-scope", Static).update(self._scope_line())
        empty = not self.listed
        table.display = not empty
        self.query_one("#no-todos", Static).display = empty
        if not empty:
            table.move_cursor(row=min(at, table.row_count - 1))
            table.focus()

    def _rows(self) -> list[services.TodoRow]:
        """The to-dos this board is showing: the memo's own or everybody's, the
        finished ones in or out, and one owner's alone where an owner is being
        filtered to — the tasks that came back with your name on them, the ones
        that came back with no name at all, or one named person's. The owners
        there are to cycle through are taken here, off the board before that
        last narrowing, so that filtering to one of them cannot leave the rest
        unreachable."""
        rows = services.todo_rows(
            self.repo, include_done=self.show_done, memo_id=self.memo_id
        )
        self.owners = sorted({row.owner for row in rows if row.owner})
        if self.owner is None:
            return rows
        if self.owner is _OwnerScope.MINE:
            return [row for row in rows if services.owned_by_me(row.owner)]
        if self.owner is _OwnerScope.UNASSIGNED:
            return [row for row in rows if not row.owner]
        return [row for row in rows if row.owner == self.owner]

    def _scope_line(self) -> str:
        """What the board says it is showing: the memo it is confined to when it
        is confined to one, and the owner it is filtered to when it is filtered
        to one. Everyone's tasks add nothing — a board saying only "to-dos" is
        the whole list, which is what the unnarrowed board has always been."""
        narrowed = []
        if self.memo_id is not None:
            narrowed.append(self.memo_name or f"memo {self.memo_id}")
        if self.owner is _OwnerScope.MINE:
            # the name as well as the word: which tasks count as yours is
            # whatever this machine has been told you are called, and a board
            # showing fewer than expected is answered by seeing that name
            narrowed.append(f"owner: me ({config.MY_NAME})")
        elif self.owner is _OwnerScope.UNASSIGNED:
            narrowed.append("owner: unassigned")
        elif self.owner is not None:
            narrowed.append(f"owner: {self.owner}")
        return _SCOPE_SEPARATOR.join([_BOARD_TITLE, *narrowed])

    def _highlighted(self) -> services.TodoRow | None:
        """The to-do the cursor is resting on, and nothing at all on an empty
        board — where the keys have to stay harmless rather than merely
        useless."""
        table = self.query_one("#todos", DataTable)
        if table.row_count == 0:
            return None
        return self.listed.get(str(table.ordered_rows[table.cursor_row].key.value))

    def action_toggle_done(self) -> None:
        """Checks the highlighted task off, or puts it back on the list."""
        row = self._highlighted()
        if row is None:
            return
        services.set_todo_status(self.repo, row.id, "open" if row.done else "done")
        self._refresh()

    def action_show_done(self) -> None:
        """Brings the finished tasks onto the board, or takes them back off."""
        self.show_done = not self.show_done
        self._refresh()

    def _owner_cycle(self) -> list[str | _OwnerScope | None]:
        """Every narrowing the owner key steps through, in the order it offers
        them: everyone, then your own tasks, then the ones nobody was named
        for, then a press per named owner. Yours come first because reading a
        shared list as your own is what the key is mostly pressed for, and they
        are left out altogether while this machine has no name to call you by,
        a narrowing to nobody being one press that can only ever empty the
        board. The unnamed are offered whether or not anyone is named, since a
        board can be nothing but tasks the extraction could not place."""
        mine = [_OwnerScope.MINE] if config.MY_NAME.strip() else []
        return [None, *mine, _OwnerScope.UNASSIGNED, *self.owners]

    def action_filter_owner(self) -> None:
        """Narrows the board a press at a time and round to everyone again —
        the way a shared list is read as your own without typing anybody's
        name. An owner whose last task has just left the board is no longer one
        of them, and lands back on everyone rather than on a filter matching
        nothing that a further press could not get out of."""
        cycle = self._owner_cycle()
        at = cycle.index(self.owner) if self.owner in cycle else -1
        self.owner = cycle[(at + 1) % len(cycle)]
        self._refresh()

    def action_open_memo(self) -> None:
        """Leaves the board for the memo the highlighted task was committed in."""
        row = self._highlighted()
        if row is not None:
            self.dismiss(row.memo_id)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row opens its memo. The table answers enter itself, so the
        key arrives as a selection rather than through this screen's binding,
        which is only there to say in the footer what enter does."""
        # the app behind this modal listens for row selections of its own, on the
        # memo table this screen has nothing to do with — and its rows are keyed
        # by memo id, which a to-do's id would be read as
        event.stop()
        self.action_open_memo()

    def action_close(self) -> None:
        """Puts the app back in front of them."""
        self.dismiss(None)


# every key the main screen answers about the memo being pointed at: the keys
# that run it, the action they name, and the words for it. Both the lines of the
# menu and the keys the menu itself answers are built from this, so it cannot
# offer a key it would then ignore. Both delete keys are named here, unlike in
# the footer where only one is worth the room: a menu is where somebody finds
# out that the other one works too
_MENU_ROWS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("e",), "edit_notes", "Edit notes"),
    (("i",), "memo_info", "Info"),
    (("m",), "move_memo", "Move"),
    (("n",), "title_memo", "Rename"),
    (("delete", "backspace"), "delete_memo", "Delete"),
    (("h",), "toggle_archive", "Archive / Unarchive"),
    (("r",), "rename_speaker", "Rename speaker"),
    (("t",), "toggle_raw", "Raw transcript"),
    (("x",), "extract", "Extract"),
    (("p",), "repair", "Repair"),
    (("d",), "diarize", "Diarize"),
    (("a",), "ask", "Ask"),
    (("c",), "check_tasks", "Tasks"),
)

# how the keys of one line are written when there is more than one of them, and
# how wide that column is: every label starts in the same place, or the list
# reads as prose rather than as a menu
_MENU_KEY_SEPARATOR = "/"
_MENU_KEY_WIDTH = max(
    len(_MENU_KEY_SEPARATOR.join(keys)) for keys, _action, _label in _MENU_ROWS
)


class ActionMenu(ModalScreen[str | None]):
    """Everything that can be done to one memo, as a list to read down rather
    than a row of keys to have memorised. The footer offers the same keys, but
    only as far as the terminal is wide, and a key whose word has been cut off
    the end of it is a key nobody finds.

    Every key still works from inside the menu, so looking one up and using it
    are the same keypress — which is how the menu stops being needed.

    It hands back the name of the action rather than running it: what these keys
    mean belongs to the app, and a menu that acted on the memo itself would be a
    second place for that to be decided."""

    BINDINGS = [
        *(
            Binding(key, f"choose('{action}')", label, show=False)
            for keys, action, label in _MENU_ROWS
            for key in keys
        ),
        Binding("escape", "close", "Close"),
    ]

    def __init__(self, filename: str) -> None:
        """Opens on the memo it is about. The name is on the screen because the
        row a memo key acts on is not always the row somebody thinks they are
        pointing at, and this is the last place to notice before one runs."""
        super().__init__()
        self.filename = filename

    def compose(self) -> ComposeResult:
        """The memo's name over what can be done to it."""
        yield Static(f"{self.filename} — actions", id="action-menu")
        yield ListView(id="action-choices")

    def on_mount(self) -> None:
        """Fills the list and picks the first line out, keyboard and all: a menu
        opening on nothing highlighted makes its first enter do nothing, and a
        menu is opened by people who are guessing."""
        choices = self.query_one("#action-choices", ListView)
        for keys, action, label in _MENU_ROWS:
            shown = _MENU_KEY_SEPARATOR.join(keys).ljust(_MENU_KEY_WIDTH)
            choices.append(ListItem(Label(f"{shown}  {label}"), name=action))
        choices.index = 0
        choices.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Runs whatever the cursor came to rest on."""
        # the screen behind this modal listens for list selections of its own
        event.stop()
        self.dismiss(event.item.name)

    def action_choose(self, action: str) -> None:
        """Runs an action off its own key, without walking down to its line."""
        self.dismiss(action)

    def action_close(self) -> None:
        """Leaves the memo as it was. Nothing has been done to it yet: the menu
        only ever names actions, so there is nothing here to confirm."""
        self.dismiss(None)


class NotesPane(MarkdownViewer):
    """One memo's notes, with their headings listed beside them so a long note
    can be jumped around rather than scrolled through.

    Links are settled here because the notes live in a database and there is no
    folder of documents around them: a bare anchor names a heading of the note
    being read, and everything else is somewhere off this screen entirely. Left
    to itself the viewer treats every link as a document to load and takes the
    whole app down on the first plain web address a note happens to carry."""

    def __init__(self, *, id: str) -> None:
        """Opens empty, with the document below leaving every link to this pane:
        two handlers on one click would send an anchor to the browser as well as
        to the heading it names."""
        super().__init__(id=id, open_links=False)

    async def go(self, location: str | PurePath) -> None:
        """Follows a link out of the notes: to the heading it names when it
        names one of this note's, and otherwise to whatever opens links on this
        machine, which is where every link went before there was a pane to
        navigate inside."""
        path, anchor = self.document.sanitize_location(str(location))
        if path == Path(".") and anchor:
            self.document.goto_anchor(anchor)
        else:
            self.app.open_url(str(location))


# what an action needs before it can mean anything, and so before the footer
# should be offering the key that runs it
MEMO_ACTIONS = frozenset(
    {
        # the menu over the rest of them, which needs the memo they need
        "action_menu",
        "edit_notes",
        "memo_info",
        "move_memo",
        "title_memo",
        "delete_memo",
        "toggle_archive",
        "rename_speaker",
        "toggle_raw",
        "extract",
        "repair",
        "diarize",
        "ask",
        "check_tasks",
    }
)
PROJECT_ACTIONS = frozenset({"rename_project", "remove_project"})

# what the sidebar row for the whole library is named behind its label. The
# empty name is the one no project can ever carry — services refuses a project
# whose name is really empty — so the row that means "narrowed by nothing"
# cannot be confused with a project somebody happened to call All
EVERYTHING = ""

# what the sidebar row for the archive is named behind its label. A name made of
# a single space is the other one no project can ever carry — services strips a
# project name before it stores it and refuses whatever is left empty — so the
# drawer memos are put away in cannot be confused with a project somebody
# happened to call Archived
ARCHIVED = " "

# how a row says work is running on it: a block travelling along a short bar,
# beside the word for what is being done. The bar is drawn out of characters
# rather than being a Rich progress bar, whose pulse is carried entirely in
# colour — and a table row under the cursor is repainted in the cursor's own
# colour, which would leave the memo being worked on with a bar frozen solid
JOB_BAR = 6
JOB_FRAME = 0.4

# how often the screen looks for writes made outside it — the menu bar recorder
# runs the pipeline in a process of its own, and a `vtn process` in another
# terminal writes the same database. Two seconds is short enough that a memo
# turns up while the person is still expecting it, and the look itself is one
# read of a number, so nothing is saved by looking less often
WATCH_INTERVAL = 2.0


def _running(doing: str, frame: int) -> str:
    """One frame of what a row shows while it is being worked on: the block a
    step further along the bar, and the word for the work."""
    at = frame % JOB_BAR
    return "".join("█" if cell == at else "░" for cell in range(JOB_BAR)) + f" {doing}"


# the stages a recording goes through on its way in: converting, transcribing,
# diarizing, and the extraction that follows them. A bar of one slot each says
# how much of the wait is behind you, which the travelling block of a job with
# no known end cannot
TOTAL_STAGES = 4


def _importing(stage: int, doing: str, frame: int) -> str:
    """One frame of what a recording being brought in shows: a slot per stage,
    the stages behind it filled in, the one it is on flickering, and the word
    for that stage."""
    cells = []
    for slot in range(1, TOTAL_STAGES + 1):
        if slot < stage:
            cells.append("█")
        elif slot == stage:
            cells.append("█" if frame % 2 else "▓")
        else:
            cells.append("░")
    return "".join(cells) + f" {doing}"


class MemoApp(App[None]):
    """One screen over the memo database: the projects down the side, the chosen
    project's recordings beside them, and what was said and made of the one
    recording you are looking at."""

    CSS = """
    #projects { width: 26; border-right: solid $panel; }
    #memos { height: 40%; border-bottom: solid $panel; }
    /* the tabs take the room the memo list leaves, and no more. Left to size
       itself to its contents a TabbedContent claims the whole column, memo list
       included, and hangs that far off the bottom of the terminal: the panes
       inside then scroll to a last row nobody can see, so the end of a long
       note or transcript is never reachable */
    TabbedContent { height: 1fr; }
    #source-tree { height: 1fr; }
    /* a Horizontal claims a fraction of the screen unless told otherwise,
       and a one-line switch row given a third of the terminal reads as a
       broken form; the file tree is what should soak up the leftover room */
    #speakers-row, #steps-row { height: auto; }
    #speakers-count { width: 12; }
    #step-list { height: auto; }
    """
    BINDINGS = [
        # every key below, laid out as a list to pick from. Space because it is
        # the one key that says "the thing I am pointing at" without saying what
        # to do with it, and because no memo key is spelled with it
        ("space", "action_menu", "Actions"),
        Binding("e", "edit_notes", "Edit notes", show=False),
        Binding("i", "memo_info", "Info", show=False),
        Binding("m", "move_memo", "Move", show=False),
        Binding("n", "title_memo", "Rename", show=False),
        # the two keys a file manager deletes with, since either is what a hand
        # reaches for; the action menu under space is where they are spelled out
        Binding("delete", "delete_memo", "Delete", show=False),
        Binding("backspace", "delete_memo", "Delete", show=False),
        # "h" for hide, which is what archiving does to a memo: it acts on the
        # one memo, so the case rule makes it lower case, and the first letter
        # of archive is already Ask
        Binding("h", "toggle_archive", "Archive / Unarchive", show=False),
        # "r" rather than "n": renaming the memo has n, and n means "no" in
        # every dialog that asks
        Binding("r", "rename_speaker", "Rename speaker", show=False),
        Binding("t", "toggle_raw", "Raw transcript", show=False),
        Binding("x", "extract", "Extract", show=False),
        # the domain calls refinement a repair pass, and r already renames
        Binding("p", "repair", "Repair", show=False),
        Binding("d", "diarize", "Diarize", show=False),
        Binding("a", "ask", "Ask", show=False),
        # the memo's own to-dos, beside the note that states them; the whole
        # board across every memo is upper case T
        Binding("c", "check_tasks", "Tasks", show=False),
        # neither half of the case rule: this acts on no memo and on no project,
        # it brings something new into the app
        ("o", "process", "Add recording"),
        # lower case acts on the one memo, upper case on the whole project the
        # sidebar cursor is resting on
        Binding("R", "rename_project", "Rename project", show=False),
        Binding("X", "remove_project", "Empty project", show=False),
        ("slash", "find_tag", "Find tag"),
        # the same exemption as o and / above: sort acts on no memo and no
        # project, only on how the list already on screen is ordered. Upper
        # case S is settings, which leaves the lower case free for this
        ("s", "toggle_sort", "Sort"),
        # upper case here means neither half of the case rule, only that the
        # lower case key is taken by the raw transcript. Like the settings under
        # S, this opens a screen of its own rather than acting on anything
        ("T", "todo_board", "To-dos"),
        ("S", "settings", "Settings"),
        ("escape", "step_back", "Back"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, repo: Repository) -> None:
        """Reads through the database the command line has already opened."""
        super().__init__()
        self.repo = repo
        self.memo_id: int | None = None
        self.project: str | None = None
        # the tag whose answers are in the memo list, when they are not a
        # project's: the two fill the same list and only one can be showing
        self.tag: str | None = None
        # how the memo list is ordered, newest first either way: by when a
        # memo was made, or by when it last changed. Its own state rather than
        # something read off the list on screen, since every redraw has to
        # keep asking for it the same way until a press of "s" says otherwise
        self.sort: str = "created"
        # whether the list is the whole library rather than one project's. Its
        # own flag rather than a third value of project above, because "no
        # project" already means "nothing has been browsed yet" — a state the
        # app opens in and comes back to, and one that must keep listing nothing
        self.everything = False
        # whether the list is the archive drawer rather than a live listing.
        # Exclusive with all three of the ways of narrowing above it: a memo put
        # away is meant to be gone from the list, so a view holding both at once
        # would leave the archiving no visible effect at all
        self.archived = False
        # what has work running on it, one job each: two passes over the same
        # thing would race each other's writes. A memo already stored is keyed by
        # its id, a recording still being brought in by the path it came from,
        # since its memo does not exist yet
        self.jobs: set[Hashable] = set()
        # what each running job is doing, in the word its row shows. Kept beside
        # the keys rather than in them so that what may run at once stays the one
        # question the set above answers
        self.running: dict[Hashable, str] = {}
        # the recordings on their way in and how far each has got, keyed by the
        # path it is coming from — which is also the key of the row standing in
        # for it, since it has no memo to be keyed by until the pipeline ends
        self.importing: dict[str, tuple[int, str]] = {}
        # where the block has got to along the bar, and the timer moving it: one
        # for the whole screen, since every row being worked on moves together
        self.frame = 0
        self.ticker: Timer | None = None
        # where the add-recording tree starts looking. Home is where recordings
        # tend to be; a VTN_BROWSE_ROOT setting would land here
        self.browse_root = Path.home()
        # a way of reading transcripts rather than a property of any one memo:
        # somebody checking a repair pass is checking all of it, memo after memo
        self.raw = False
        # what the database was numbered at when this screen last took it in,
        # so a look can tell "another process has written" from "nothing has
        # happened" without reading a single row. Read before the first draw
        # rather than after it: a write landing in between then costs one
        # redundant redraw, where the other order would lose it until the next
        # write came along
        self.seen_version = services.data_version(repo)
        # whether a theme change is now the reader's doing and worth storing.
        # Applying the stored theme at start-up moves the same reactive a
        # reader's own pick moves, and writing that back would be the app
        # answering itself — harmlessly on most launches, but destructively
        # when the theme in effect came from VTN_TUI_THEME, which would end up
        # frozen into vtn.toml as though it had been chosen
        self._theme_restored = False

    def compose(self) -> ComposeResult:
        """Projects beside the memo list, the memo list above its detail."""
        yield Header()
        with Horizontal():
            yield ListView(id="projects")
            with Vertical():
                # a table rather than a list of names: what state each memo is
                # in decides which one you want, and reading that one memo at a
                # time through the info key means opening every memo to find it
                yield DataTable(id="memos", cursor_type="row")
                with TabbedContent():
                    # the notes pane scrolls itself, headings and all
                    with TabPane("Notes"):
                        yield NotesPane(id="notes")
                    with TabPane("Transcript", id="transcript-tab"), VerticalScroll():
                        yield Static(id="transcript")
        yield Footer()

    def on_mount(self) -> None:
        """Opens on the projects that exist, with the first one already picked
        out: a list highlighting nothing makes the session's first Enter do
        nothing, which reads as the app being broken. The memo table gets its
        headings once, since only its rows change from here on, each column
        keyed by what it is called so that a single cell can be found again by
        name rather than by counting along the row.

        The timer that follows is what keeps the screen honest about a database
        it is not the only writer of."""
        self._restore_theme()
        memos = self.query_one("#memos", DataTable)
        for column in services.MEMO_COLUMNS:
            memos.add_column(column, key=column)
        self.load_projects()
        self.query_one("#projects", ListView).focus()
        self.set_interval(WATCH_INTERVAL, self._take_in_outside_writes)

    def _restore_theme(self) -> None:
        """Opens in the theme last picked, and in Textual's own default when
        that name means nothing to this Textual: setting an unregistered theme
        raises, so a name an upgrade has since dropped would take the whole app
        down at start-up rather than costing a reader their colours. From here
        on a theme change is somebody's choice and gets written down."""
        if config.TUI_THEME in self.available_themes:
            self.theme = config.TUI_THEME
        self._theme_restored = True

    def watch_theme(self, theme_name: str) -> None:
        """Stores a theme picked from the command palette, which otherwise
        lasts only as long as the process. Silently: the new colours are the
        confirmation, and config_set's "takes effect on next start" line would
        be untrue of a change already on screen."""
        if self._theme_restored:
            services.config_set("tui_theme", theme_name)

    def _cursor_key(self) -> str | None:
        """What the row under the memo list's cursor is keyed by — a memo id, or
        the path a recording is still arriving from — and nothing at all when the
        cursor is resting on no row, as it does over an empty list."""
        # checked while the screen is still being built, before there is a table
        table = self.query("#memos")
        if not table:
            return None
        memos = table.first(DataTable)
        rows = memos.ordered_rows
        if not 0 <= memos.cursor_row < len(rows):
            return None
        return str(rows[memos.cursor_row].key.value)

    def _target_memo(self) -> int | None:
        """The memo a memo key acts on: the one open below the list when there
        is one, and otherwise the row the list cursor is resting on. Pointing at
        a memo is enough, the way it is enough in a file manager — opening a
        recording to file it away or throw it out is a step that says nothing.

        Nothing at all when the cursor is on no memo: an empty list, or a row
        standing in for a recording still coming in, which is keyed by the path
        it is arriving from rather than by a memo id it does not have yet."""
        if self.memo_id is not None:
            return self.memo_id
        key = self._cursor_key()
        if key is None:
            return None
        try:
            return int(key)
        except ValueError:
            return None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Which keys the footer offers, so that it never advertises one that
        would do nothing. Returning False hides a binding and also stops the key
        reaching its action; the no-op guards inside the actions stay all the
        same, since a key that is merely hidden has to remain harmless for
        anything that reaches it another way.

        The two halves are gated differently on purpose. A memo action is offered
        whenever there is a memo to act on — open below the list, or merely
        pointed at in it — whatever holds focus, because a memo is on screen
        either way. A project action is offered only while the sidebar holds the
        cursor, because the thing it acts on is the row the cursor rests on —
        and only while that row is a project, since the row standing for the
        whole library has no name to rename and nothing of its own to empty."""
        if action in MEMO_ACTIONS:
            return self._target_memo() is not None
        if action in PROJECT_ACTIONS:
            # checked while the screen is still being built, before there is one
            sidebar = self.query("#projects")
            if not sidebar or self.focused is not sidebar.first():
                return False
            return self._pointed_project() is not None
        if action == "step_back":
            if self.memo_id is not None or self.tag is not None:
                return True
            sidebar = self.query("#projects")
            on_sidebar = bool(sidebar) and self.focused is sidebar.first()
            return self.focused is not None and not on_sidebar
        return True

    def load_projects(self) -> None:
        """Draws the sidebar from what is in the database now, counts and all,
        leaving the cursor on the row it was resting on. Every count on it moves
        whenever anything is stored, here or in another process, and a cursor
        thrown back to the top by each of those would keep changing which
        project the project keys act on under the hand pressing them.

        The whole library is offered first, above the projects and counted as
        all of them together: a sidebar of projects alone can only ever answer
        "what is in this one", and it is where the cursor rests on the first
        draw, so the session opens on everything rather than on whichever
        project happens to sort first.

        The archive comes last, under the projects, and is offered even while
        nothing has been put in it: it is where a memo goes, and a row that only
        appears once somebody has archived something is a row nobody discovers
        beforehand. Its count is its own, since none of the counts above it
        include an archived memo.

        A project that is no longer listed leaves the cursor at the top, which
        is also where the first draw of the session puts it: a sidebar
        highlighting nothing makes the session's first Enter do nothing, which
        reads as the app being broken."""
        sidebar = self.query_one("#projects", ListView)
        resting = sidebar.highlighted_child
        was_on = resting.name if resting else None
        listed = services.projects(self.repo)
        sidebar.clear()
        held = sum(count for _name, count in listed)
        sidebar.append(ListItem(Label(f"All ({held})"), name=EVERYTHING))
        for name, count in listed:
            sidebar.append(ListItem(Label(f"{name} ({count})"), name=name))
        put_away = services.archived_count(self.repo)
        sidebar.append(ListItem(Label(f"Archived ({put_away})"), name=ARCHIVED))
        names = [EVERYTHING, *(name for name, _count in listed), ARCHIVED]
        sidebar.index = names.index(was_on) if was_on in names else 0

    def _list_memos(self, rows: list[services.MemoRow]) -> None:
        """Draws the memo table from one listing, keying every row by the id of
        the memo it describes: the row is what gets picked, and its position
        says nothing about which memo that is. The cells go in the order the
        headings were added in, both of them services'."""
        memos = self.query_one("#memos", DataTable)
        memos.clear()
        for row in rows:
            memos.add_row(
                row.name,
                row.duration,
                row.speakers,
                row.todos,
                row.status,
                row.created,
                row.updated,
                row.project,
                key=str(row.id),
            )
        # the rows have just come back from the database, which knows nothing of
        # work still running or of a recording that is not stored yet, so both
        # have to put themselves back
        for key in self.importing:
            self._place_importing(key)
        self._draw_importing()
        self._draw_running()

    def _place_importing(self, key: str) -> None:
        """Stands a row in for a recording still coming in. Everything but its
        name and its stage bar is blank, because nothing else about it is known
        until the pipeline has read it."""
        self.query_one("#memos", DataTable).add_row(
            Path(key).name, "", "", "", "", "", "", "", key=key
        )

    def _draw_importing(self) -> None:
        """Puts the stage bar back on the row of every recording on its way in."""
        memos = self.query_one("#memos", DataTable)
        for key, (stage, doing) in self.importing.items():
            if key in memos.rows:
                memos.update_cell(
                    key, "status", _importing(stage, doing, self.frame), update_width=True
                )

    def _draw_running(self) -> None:
        """Puts the indicator back on the row of everything being worked on that
        the list is showing. A recording still being brought in is passed over:
        its row counts the stages of the pipeline off instead, which says more
        than a block travelling with no end in sight."""
        memos = self.query_one("#memos", DataTable)
        for key, doing in self.running.items():
            if str(key) not in self.importing and str(key) in memos.rows:
                memos.update_cell(
                    str(key), "status", _running(doing, self.frame), update_width=True
                )

    def _relist(self) -> None:
        """Redraws whichever listing is on screen, so that the status column
        stops describing the memos as they were when the list was opened. The
        listing has to be redrawn as the same listing: a redraw that reached for
        a project while the whole library was on screen would narrow the list
        nobody asked to narrow, and there is no project to reach for anyway.

        The archive is asked about first because it is exclusive with all three
        of the others: the drawer is on screen with no project, no tag and no
        whole library behind it, and every one of those would redraw it as a
        listing of memos that are not in it."""
        if self.archived:
            self.show_archived()
        elif self.tag is not None:
            self.show_tagged(self.tag)
        elif self.everything:
            self.show_everything()
        elif self.project is not None:
            self.show_project(self.project)

    def _relist_in_place(self) -> None:
        """Redraws the listing without moving what the reader is pointing at, as
        a redraw nobody asked for has to: the cursor goes back on the row it was
        resting on, found again by its key, since a memo arriving at the top of
        the list slides every row below it down one and a cursor left at its own
        position would come to rest on some other memo. A row that is no longer
        listed leaves the cursor wherever the table puts it."""
        memos = self.query_one("#memos", DataTable)
        was_on = self._cursor_key()
        self._relist()
        if was_on is not None and was_on in memos.rows:
            memos.move_cursor(row=memos.get_row_index(was_on))

    def action_toggle_sort(self) -> None:
        """Flips the memo list between newest-created and newest-updated.
        Redrawn in place rather than fresh: reordering a list somebody is
        already reading must not also move what their next keypress acts on."""
        self.sort = "updated" if self.sort == "created" else "created"
        self._relist_in_place()

    def _take_in_outside_writes(self) -> None:
        """Draws what some other process has written since the last look. The
        recorder runs the pipeline in a process of its own, so a memo can be
        stored while this screen sits idle, and a screen that only redraws on
        its own keystrokes goes on denying that memo exists until somebody
        restarts it.

        Nothing at all when nothing has been written, which is nearly every
        look: reading the listing back each time to compare it would mean a
        query every couple of seconds for the length of a session.

        Nothing either while a dialog is up, while a job is running, or while a
        recording is coming in. A list rebuilt under a dialog moves the thing
        the dialog is asking about, and the rows standing in for recordings
        being brought in exist on this screen alone, so a rebuild from the
        database would take them away mid-import. What was written is not lost
        by waiting: the version is left unread, and each of those states redraws
        as it ends anyway."""
        if len(self.screen_stack) > 1 or self.jobs or self.importing:
            return
        version = services.data_version(self.repo)
        if version == self.seen_version:
            return
        self.seen_version = version
        self.load_projects()
        self._relist_in_place()

    def _listed(self, memo_id: int) -> bool:
        """Whether a memo is among the rows the table is showing."""
        return str(memo_id) in self.query_one("#memos", DataTable).rows

    def _subtitle(self) -> str:
        """What the header says about the listing on screen, beyond the memos
        themselves: the archive drawer, every project at once, a tag search,
        and an order other than the default, whichever of those apply, joined
        in that order. A plain project's own listing needs none of them — its
        name is already the highlighted row in the sidebar — so the ordinary
        case is the empty string, not a bug in it."""
        pieces = []
        if self.archived:
            pieces.append("archived")
        if self.everything:
            pieces.append("all projects")
        if self.tag is not None:
            pieces.append(f"tag: {self.tag}")
        if self.sort != "created":
            pieces.append("by updated")
        return " · ".join(pieces)

    def show_everything(self) -> None:
        """Lists every recording there is, whatever it is filed under, ordered
        as the sort state says. The subtitle says so, since a list that no
        project accounts for would otherwise read as one project's, and the
        project column beside each name says where each of them lives."""
        self.project = None
        self.tag = None
        self.everything = True
        self.archived = False
        self.sub_title = self._subtitle()
        self._list_memos(services.memo_rows(self.repo, sort=self.sort))
        self.refresh_bindings()

    def show_archived(self) -> None:
        """Lists the memos somebody has put away, and only those: the drawer is
        opened in place of a live listing rather than merged into one, since a
        list holding both would leave archiving with no visible effect at all.
        The subtitle says so, because a listing no sidebar project accounts for
        would otherwise read as a project that has emptied itself.

        Nothing under an archived memo was rewritten while it sat there, so
        every memo key still means here what it means in a live listing."""
        self.project = None
        self.tag = None
        self.everything = False
        self.archived = True
        self.sub_title = self._subtitle()
        self._list_memos(services.memo_rows(self.repo, sort=self.sort, archived=True))
        self.refresh_bindings()

    def show_project(self, project: str) -> None:
        """Lists one project's recordings, ordered as the sort state says.
        This is also the way back from a tag search, so it takes the search
        down with it rather than leaving the subtitle claiming a tag."""
        self.project = project
        self.tag = None
        self.everything = False
        self.archived = False
        self.sub_title = self._subtitle()
        self._list_memos(services.memo_rows(self.repo, project=project, sort=self.sort))
        self.refresh_bindings()

    def show_tagged(self, tag: str) -> None:
        """Fills the memo list with everything carrying one tag, from whatever
        project it is filed under, ordered as the sort state says. The
        subtitle says so: the list no longer matches the highlighted project,
        and leaving that unsaid makes the sidebar read as a lie.

        What was being read before the search is left standing, whole library or
        one project, because escape comes back to it. Not the archive drawer,
        though: a search answers out of the live memos, so a subtitle still
        claiming the drawer would be describing a listing nothing in it is
        in."""
        self._list_memos(services.memo_rows(self.repo, tag=tag, sort=self.sort))
        self.tag = tag
        self.archived = False
        self.sub_title = self._subtitle()
        self.refresh_bindings()

    def show_memo(self, memo_id: int) -> None:
        """Shows one recording's notes and what was actually said in it."""
        self.memo_id = memo_id
        self.query_one("#notes", NotesPane).document.update(
            services.notes_markdown(self.repo, memo_id)
        )
        self.query_one("#transcript", Static).update(
            services.transcript_lines(self.repo, memo_id, raw=self.raw)
        )
        self.query_one(TabbedContent).get_tab("transcript-tab").label = (
            "Transcript (raw)" if self.raw else "Transcript"
        )
        self.refresh_bindings()

    def _reload(self, project: str | None) -> None:
        """Redraws the sidebar and whatever listing is under it, dropping the
        detail panes when the memo they describe is no longer among its rows.
        Every bulk refiling ends here.

        A refiling moves memos between projects, so the whole library shows
        exactly the same rows afterwards and one project rarely does — which is
        why the listing that is up decides what is redrawn, rather than the
        project the caller was working from."""
        self.load_projects()
        if self.everything:
            self.show_everything()
        elif project is not None:
            self.show_project(project)
        else:
            return
        memo_id = self.memo_id
        if memo_id is not None and not self._listed(memo_id):
            self.clear_memo()

    def clear_memo(self) -> None:
        """Empties the detail panes and forgets what they were showing, for when
        the memo they describe is no longer one of the rows above them. The tab
        drops its raw marking with the transcript it was describing, while the
        way of reading transcripts is kept for the next memo opened."""
        self.memo_id = None
        self.query_one("#notes", NotesPane).document.update("*no memo shown*")
        self.query_one("#transcript", Static).update("")
        self.query_one(TabbedContent).get_tab("transcript-tab").label = "Transcript"
        self.refresh_bindings()

    def action_toggle_raw(self) -> None:
        """Swaps the transcript between the repaired reading and the words as
        they were transcribed, which is how a repair pass gets checked. What it
        swaps is the transcript in the pane, so a memo only pointed at is opened
        to be read: the answer to raw against repaired is the text itself."""
        memo_id = self._target_memo()
        if memo_id is None:
            return
        self.raw = not self.raw
        self.show_memo(memo_id)

    def _start_job(
        self, key: Hashable, subject: str, doing: str, run: Callable[[Repository], str]
    ) -> bool:
        """Sends one piece of slow work off to a thread, saying whether it went.
        The key is whatever is being worked on: a memo's id for work on one
        already stored, and the source path for a recording being brought in,
        which has no memo yet. Either way one job at a time on the same thing —
        a second pass would be writing over what the first is still deciding —
        while two different things are free to run at once. A caller that draws
        something of its own beside the job needs to know it was refused, so
        that it does not draw it for work nobody is doing."""
        if key in self.jobs:
            self.notify(f"{subject} is already busy", severity="warning")
            return False
        self.jobs.add(key)
        self.notify(f"{doing} {subject} …")
        self._started(key, doing)
        self._run_job(key, run)
        return True

    def _started(self, key: Hashable, doing: str) -> None:
        """Marks the row of what is being worked on as busy and keeps the bar
        moving. A still bar reads as a screen that has hung, which over a pass
        that takes minutes is the whole thing being said."""
        self.running[key] = doing
        self._draw_running()
        if self.ticker is None:
            self.ticker = self.set_interval(JOB_FRAME, self._advance)

    def _advance(self) -> None:
        """Moves every bar on to its next frame."""
        self.frame += 1
        self._draw_importing()
        self._draw_running()

    def _finished(self, key: Hashable) -> None:
        """Takes the indicator off the row now that the work has stopped,
        however it stopped, and puts the whole listing back in step with the
        database: the memo that was worked on is not the only row a pass can
        have changed. With nothing left running the bars stop moving, since a
        timer redrawing an idle screen keeps a laptop awake for nothing."""
        self.running.pop(key, None)
        self._import_ended(key)
        if not self.running and self.ticker is not None:
            self.ticker.stop()
            self.ticker = None
        self._relist()

    def _start_memo_job(
        self, memo_id: int, doing: str, run: Callable[[Repository], str]
    ) -> None:
        """Slow work on a memo that already exists, keyed and named by its id."""
        self._start_job(memo_id, f"memo {memo_id}", doing, run)

    @work(thread=True)
    def _run_job(self, key: Hashable, run: Callable[[Repository], str]) -> None:
        """The slow part, off the main thread because it waits on a subprocess or
        a model. It opens its own connection, since sqlite refuses one belonging
        to another thread. Only the failures a person can act on are caught: a
        worker that raises takes the whole app down with it, so a missing model
        or a stopped server has to come back as a message, while a bug keeps its
        traceback rather than being swallowed here."""
        try:
            with services.open_repo(self.repo) as worker:
                done = run(worker)
        except (GatewayError, services.ExtractionError, services.InvalidInput) as failed:
            self.call_from_thread(self._job_ended, key, str(failed), True)
            return
        self.call_from_thread(self._job_ended, key, done, False)

    def _job_ended(self, key: Hashable, message: str, failed: bool) -> None:
        """Back on the main thread: frees the thing that was worked on, says how
        it went, and redraws what the work changed — the listing always, since a
        row that still describes the memo as it was before the pass is simply
        wrong, but the detail below it only while that memo is still the one on
        screen, since pulling somebody back to a memo they have left is worse
        than letting them find it changed when they return. A key that is not a
        memo id simply never matches the memo on screen."""
        self.jobs.discard(key)
        self._finished(key)
        self.notify(message, severity="error" if failed else "information")
        memo_id = self.memo_id
        if not failed and memo_id is not None and memo_id == key:
            self.show_memo(memo_id)

    def action_extract(self) -> None:
        """Turns the memo being pointed at into notes, asking first when that
        would go over a note somebody wrote by hand."""
        memo_id = self._target_memo()
        if memo_id is None:
            return
        if services.memo_info(self.repo, memo_id).edited:
            self.push_screen(
                ConfirmForce(), lambda agreed: self._extract_over_edit(memo_id, agreed)
            )
        else:
            self._extract(memo_id, force=False)

    def _extract_over_edit(self, memo_id: int, agreed: bool | None) -> None:
        """Extracts only once they have said the edit can go."""
        if agreed:
            self._extract(memo_id, force=True)

    def _extract(self, memo_id: int, *, force: bool) -> None:
        """Sends the extraction off, naming the backend that answered."""
        self._start_memo_job(
            memo_id,
            "extracting",
            lambda repo: (
                f"memo {memo_id} extracted"
                f" via {services.run_extraction(repo, memo_id, force=force)}"
            ),
        )

    def action_repair(self) -> None:
        """Repairs transcription errors in the memo being pointed at."""
        memo_id = self._target_memo()
        if memo_id is None:
            return
        self._start_memo_job(memo_id, "repairing", lambda repo: self._repair(repo, memo_id))

    def _repair(self, repo: Repository, memo_id: int) -> str:
        """The repair pass itself, and what to say once it has been stored."""
        services.refine_transcript(repo, memo_id)
        return f"memo {memo_id} repaired"

    def action_diarize(self) -> None:
        """Offers to run speaker detection over the memo being pointed at again,
        pinning how many voices to look for if that count is known."""
        memo_id = self._target_memo()
        if memo_id is None:
            return
        self.push_screen(DiarizeSpeakers(lambda typed: self._diarize_count(memo_id, typed)))

    def _diarize_count(self, memo_id: int, typed: str) -> None:
        """Refuses a speaker count that cannot be used here and now, so the
        modal keeps what was typed, and sends anything else off to a thread."""
        num_speakers = services.speaker_count(typed)
        self._start_memo_job(
            memo_id, "diarizing", lambda repo: self._diarize(repo, memo_id, num_speakers)
        )

    def _diarize(self, repo: Repository, memo_id: int, num_speakers: int | None = None) -> str:
        """The speaker pass itself, and what to say once it has been stored."""
        services.rediarize(repo, memo_id, num_speakers=num_speakers)
        return f"memo {memo_id} diarized"

    def action_process(self) -> None:
        """Offers to bring a new recording in, filed where you are looking."""
        self.push_screen(
            ProcessMemo(
                self.browse_root,
                self.project or "other",
                self._process,
                self._already_stored,
            )
        )

    def _already_stored(self, path: str) -> Memo | None:
        """The memo this recording is already in the database as, when it is
        one. Reading the instant it was taped is a single ffprobe over the
        container, quick enough to answer while somebody's hand is still on the
        Enter key.

        Anything that cannot be read that far counts as no duplicate rather than
        as a failure: a path with a typo in it, or a file ffprobe cannot make
        sense of, is no evidence that the recording is already here, and both
        are about to be refused where they are properly refused — one by the
        import entry below, the other by the pipeline, saying what ffmpeg
        said."""
        src = Path(path.strip())
        if not src.is_file():
            return None
        try:
            return services.find_duplicate(self.repo, src)
        except GatewayError:
            return None

    def _process(self, path: str, project: str, options: ImportOptions) -> None:
        """Refuses what is plainly not a recording, and a project with no name,
        here and now so the modal keeps what was typed. Past that the pipeline
        decides: a file ffmpeg cannot make sense of fails the way any gateway
        does, and says what ffmpeg said."""
        src = Path(path.strip())
        if not src.is_file():
            raise services.InvalidInput("no recording at that path")
        name = services.project_name(project)
        started = self._start_job(
            src,
            src.name,
            "processing",
            lambda repo: self._processed(repo, src, name, options),
        )
        if started:
            self._import_started(src)

    def _import_started(self, src: Path) -> None:
        """Puts the recording on the list under its own name the moment it is
        sent off. The pipeline runs for minutes before there is a memo to list,
        and a list that says nothing of the recording in all that time reads as
        one that took nothing in."""
        self.importing[str(src)] = (1, "converting")
        self._place_importing(str(src))
        self._draw_importing()

    def _import_at(self, src: Path, stage: int, doing: str) -> None:
        """Moves the recording's row on to the stage the pipeline has reached,
        from the worker thread the pipeline reports on."""
        self.call_from_thread(self._import_stage, str(src), stage, doing)

    def _import_stage(self, key: str, stage: int, doing: str) -> None:
        """Back on the main thread: remembers how far the recording has got, so
        that a redrawn list stands its row back up on the same stage."""
        self.importing[key] = (stage, doing)
        self._draw_importing()

    def _import_ended(self, key: Hashable) -> None:
        """Takes the row of a recording that is no longer coming in off the
        list, whether it became a memo or failed on the way, and puts the
        sidebar counts back in step with what was stored. The row is removed
        here rather than left to the redraw that follows, since with no project
        being browsed there is no redraw to take it away."""
        if self.importing.pop(str(key), None) is None:
            return
        memos = self.query_one("#memos", DataTable)
        if str(key) in memos.rows:
            memos.remove_row(str(key))
        self.load_projects()

    def _processed(
        self, repo: Repository, src: Path, project: str, options: ImportOptions
    ) -> str:
        """Runs the whole pipeline, counting its stages off on the row standing
        in for the recording, and leaves the reader on whatever they were
        reading: the memo is stored, so it will be there when they go looking,
        and its row appears in place of the placeholder once the job ends.
        Repair and extraction run on in the same job as stages of their own,
        repair first so a following extraction reads the repaired transcript
        rather than the raw one, and each only when it was actually asked
        for — a memo brought in with just its transcript is left to be
        finished a step at a time later. Either stage failing must not undo a
        memo already safely stored, so a failure in either is only reported —
        the same tolerance `vtn process` gives both at the command line."""
        result = services.process_memo(
            repo,
            src,
            project,
            log=self._stage,
            progress=lambda stage, doing: self._import_at(src, stage, doing),
            num_speakers=options.num_speakers,
            diarize="speakers" in options.steps,
        )
        if "refine" in options.steps:
            self._stage("refining transcript …")
            try:
                refined = services.refine_transcript(repo, result.memo_id)
            except (GatewayError, services.ExtractionError) as failed:
                self.call_from_thread(self.notify, str(failed), severity="warning")
            else:
                self._stage(f"memo {result.memo_id} repaired {len(refined.changes)} lines")
        if "notes" in options.steps:
            self._stage("extracting notes …")
            self._import_at(src, TOTAL_STAGES, "extracting notes")
            try:
                backend = services.run_extraction(repo, result.memo_id, template=options.template)
            except (GatewayError, services.ExtractionError) as failed:
                self.call_from_thread(self.notify, str(failed), severity="warning")
            else:
                self._stage(f"memo {result.memo_id} extracted via {backend}")
        else:
            self._stage("transcript stored — extract notes any time")
        return f"{src.name} is memo {result.memo_id}"

    def _stage(self, message: str) -> None:
        """One line of progress from the pipeline, said on the main thread.
        Converting and transcribing take minutes between them, and a screen
        that says nothing for that long reads as one that has hung."""
        self.call_from_thread(self.notify, message)

    def action_ask(self) -> None:
        """Offers to put a question to the memo being pointed at."""
        memo_id = self._target_memo()
        if memo_id is None:
            return
        self.push_screen(AskQuestion(lambda asked: self._ask(memo_id, asked)))

    def _ask(self, memo_id: int, asked: str) -> None:
        """Refuses a blank question here and now, so the modal keeps what was
        typed, and sends anything else off to a thread."""
        text = services.question(asked)
        self._start_memo_job(
            memo_id, "answering", lambda repo: self._answer(repo, memo_id, text)
        )

    def _answer(self, repo: Repository, memo_id: int, asked: str) -> str:
        """Puts the question, then opens the answer — even when the reader has
        moved on to another memo. Work like extracting or repairing writes its
        result to the database, so a screen that does not redraw loses nothing
        and the change is there on the way back. An answer is stored nowhere, so
        this screen is the only copy of it: dropping it for arriving late would
        destroy the one thing that was asked for and waited on."""
        backend, answer = services.ask(repo, memo_id, asked)
        self.call_from_thread(self.push_screen, Answer(memo_id, asked, answer))
        return f"memo {memo_id} answered via {backend}"

    def action_find_tag(self) -> None:
        """Offers to look for a tag across every project at once."""
        self.push_screen(TagSearch(self.show_tagged))

    def action_settings(self) -> None:
        """Opens every setting `vtn config` would list, to be read and changed
        without leaving the app."""
        self.push_screen(SettingsScreen())

    def action_todo_board(self) -> None:
        """Opens everything every memo has committed to, as one list to work
        down."""
        self.push_screen(TodoBoard(self.repo), self._board_closed)

    def action_check_tasks(self) -> None:
        """Opens just what the memo being pointed at committed to, to be checked
        off against the note that states it. A note is read to find out what was
        agreed; the ticking that follows belongs on the same screen rather than
        on the far side of the whole board."""
        memo_id = self._target_memo()
        if memo_id is None:
            return
        self.push_screen(TodoBoard(self.repo, memo_id=memo_id), self._board_closed)

    def _board_closed(self, memo_id: int | None) -> None:
        """Takes the app back to what the board has been changing behind it: a
        task checked off changes what a memo row says is still outstanding, and
        the note that states that task now shows its box ticked, and both were
        drawn before the board went up. The sidebar is left alone, cursor and all
        — it counts recordings, and no number on it can have moved while the only
        thing on offer was checking a task off.

        The memo a to-do was committed in is opened last, over a list that
        already describes it correctly; with none asked for, whichever memo was
        already open is redrawn where it stands."""
        self._relist()
        shown = memo_id if memo_id is not None else self.memo_id
        if shown is not None:
            self.show_memo(shown)

    def action_step_back(self) -> None:
        """Closes whatever is open before it ever moves focus off it, since
        escape means close what is in front of you rather than merely look
        away from it: an open memo goes first, wherever the cursor sitting
        inside its detail pane has wandered to, and a tag search goes next.
        Only once neither is open does escape fall back to moving focus
        toward the projects list, the near edge of the screen: out of a
        detail pane onto the memo table, and out of the table onto the
        sidebar."""
        memos = self.query_one("#memos", DataTable)
        if self.memo_id is not None:
            self.clear_memo()
            memos.focus()
            return
        if self.tag is not None:
            self._clear_tag()
            return
        projects = self.query_one("#projects", ListView)
        focused = self.focused
        if focused is memos:
            projects.focus()
        elif focused is not None and focused is not projects:
            memos.focus()

    def _clear_tag(self) -> None:
        """Puts whatever the search was run from back in the list once it has
        been read — the project, or the whole library when the search reached
        out of that. A search run before anything was ever browsed has nothing
        behind it, and leaves the list empty rather than inventing one. With no
        search showing there is nothing to come back from, so the escape ladder
        never reaches this rung in that case."""
        if self.tag is None:
            return
        if self.project is not None:
            self.show_project(self.project)
        elif self.everything:
            self.show_everything()
        else:
            self.query_one("#memos", DataTable).clear()
            self.tag = None
            self.sub_title = self._subtitle()
            self.refresh_bindings()

    def action_action_menu(self) -> None:
        """Lays out everything that can be done to the memo being pointed at, if
        any memo is: the memo keys are worth learning and unguessable, and a
        footer only names as many of them as the terminal is wide enough for."""
        memo_id = self._target_memo()
        if memo_id is None:
            return
        self.push_screen(
            ActionMenu(services.require_memo(self.repo, memo_id).filename),
            self._run_chosen,
        )

    async def _run_chosen(self, action: str | None) -> None:
        """Runs whatever the menu was closed on, as though its key had been
        pressed out here: the menu names the actions and this screen owns them,
        so picking one off the list and pressing its key cannot come apart.

        Awaited rather than called, because dispatching an action is
        asynchronous — a callback that dropped the coroutine would close the
        menu on nothing happening at all."""
        if action is not None:
            await self.run_action(action)

    def action_memo_info(self) -> None:
        """Says what state the memo being pointed at is in, if any memo is."""
        memo_id = self._target_memo()
        if memo_id is None:
            return
        self.push_screen(MemoDetails(services.memo_info_text(self.repo, memo_id)))

    def action_edit_notes(self) -> None:
        """Opens the pointed-at memo's notes for editing, if any memo is. The
        memo is put on screen first: the editor closes back onto the note it
        just changed, and closing onto some other memo's note would read as the
        edit having landed on the wrong one."""
        memo_id = self._target_memo()
        if memo_id is None:
            return
        self.show_memo(memo_id)
        self.push_screen(
            NoteEditor(
                services.notes_markdown(self.repo, memo_id),
                lambda markdown: self._save_notes(memo_id, markdown),
            )
        )

    def _save_notes(self, memo_id: int, markdown: str) -> None:
        """Stores what they wrote, then shows the memo as it now reads."""
        services.save_notes(self.repo, memo_id, markdown)
        self.show_memo(memo_id)

    def action_move_memo(self) -> None:
        """Offers to refile the memo being pointed at, if any memo is."""
        memo_id = self._target_memo()
        if memo_id is None:
            return
        self.push_screen(
            MoveMemo(
                services.projects(self.repo),
                lambda project: self._move_memo(memo_id, project),
            )
        )

    def _move_memo(self, memo_id: int, project: str) -> None:
        """Refiles the memo, then redraws what that changed. Filed away from the
        project on screen, the memo drops out of the list, and panes left
        describing it would be describing a row that is no longer there."""
        services.move_memo(self.repo, memo_id, project)
        self._reload(self.project)

    def action_title_memo(self) -> None:
        """Offers to rename the memo being pointed at, if any memo is."""
        memo_id = self._target_memo()
        if memo_id is None:
            return
        self.push_screen(
            RenameMemo(
                services.require_memo(self.repo, memo_id).filename,
                lambda name: self._title_memo(memo_id, name),
            )
        )

    def _title_memo(self, memo_id: int, name: str) -> None:
        """Renames the memo, then redraws the list it is named in — the list
        being the one place the name is read. A name of nothing is refused by
        services, and the refusal travels back to the modal, which keeps what
        was typed rather than losing it."""
        services.rename_memo(self.repo, memo_id, name)
        self._relist()

    def action_delete_memo(self) -> None:
        """Offers to throw the pointed-at memo away, asking first and naming it
        while it asks: this is the one memo key with nothing behind it."""
        memo_id = self._target_memo()
        if memo_id is None:
            return
        filename = services.require_memo(self.repo, memo_id).filename
        self.push_screen(
            ConfirmDelete(filename),
            lambda confirmed: self._delete_memo(memo_id, confirmed),
        )

    def _delete_memo(self, memo_id: int, confirmed: bool | None) -> None:
        """Throws the memo away once they have said to, and takes everything it
        left on screen with it: the panes below the list when that memo was the
        one being read, the count beside the project it was filed under, and its
        row. Says what went by name, since the row that said so is gone."""
        if not confirmed:
            return
        filename = services.delete_memo(self.repo, memo_id)
        if self.memo_id == memo_id:
            self.clear_memo()
            self.query_one("#memos", DataTable).focus()
        self.load_projects()
        self._relist()
        self.notify(f"deleted {filename}")

    def action_toggle_archive(self) -> None:
        """Puts the pointed-at memo away, or takes it back out when it is
        already away: one key, because a memo is in exactly one of the two
        states and the key always means the other one. Nothing is asked first,
        unlike the delete beside it — every byte of the memo stays where it is,
        and the way back is the same keypress.

        Which way it goes is read off the memo rather than off the listing that
        is on screen, so this never holds a second opinion about where the memo
        already is — a stale flag would archive a memo that is already archived
        and leave the drawer looking broken.

        Either way the memo leaves the listing it was in, so everything it left
        on the screen goes with it, exactly as a delete's does: the panes below
        the list when it was the memo being read, the count beside its project
        and the count beside the archive, and its row. What moved is said by
        name, since the row that named it is gone."""
        memo_id = self._target_memo()
        if memo_id is None:
            return
        if services.memo_info(self.repo, memo_id).archived:
            moved = f"took {services.unarchive_memo(self.repo, memo_id)} out of the archive"
        else:
            moved = f"archived {services.archive_memo(self.repo, memo_id)}"
        if self.memo_id == memo_id:
            self.clear_memo()
            self.query_one("#memos", DataTable).focus()
        self.load_projects()
        self._relist()
        self.notify(moved)

    def action_rename_speaker(self) -> None:
        """Offers to name a voice in the memo being pointed at, if any memo is."""
        memo_id = self._target_memo()
        if memo_id is None:
            return
        self.push_screen(
            RenameSpeaker(
                services.speakers(self.repo, memo_id),
                lambda label, name: self._rename_speaker(memo_id, label, name),
            )
        )

    def _rename_speaker(self, memo_id: int, label: str, name: str) -> None:
        """Names the voice, then shows the transcript calling it that — opening
        the memo if it was only pointed at, since the renamed voice in its own
        lines is the only place the new name can be checked."""
        services.rename_speaker(self.repo, memo_id, label, name)
        self.show_memo(memo_id)

    def _pointed_project(self) -> str | None:
        """The project the sidebar cursor is resting on, and nothing at all once
        the cursor is no longer what the person is pointing at: the project keys
        read the sidebar, so they stay out of the way while the memos have focus.

        Nothing either on either of the two rows that are not projects. The
        whole library is every project at once, so renaming or emptying it names
        nothing; the archive is a drawer memos are put in, and the name behind
        that row is a sentinel no project can carry — emptying it would take
        that sentinel for a real project and refile every memo filed under it.
        That answer is what keeps those keys hidden on both rows, and what makes
        them harmless if one is reached some other way."""
        sidebar = self.query_one("#projects", ListView)
        if self.focused is not sidebar:
            return None
        chosen = sidebar.highlighted_child
        if chosen is None or chosen.name in (EVERYTHING, ARCHIVED):
            return None
        return chosen.name

    def action_rename_project(self) -> None:
        """Offers to rename the project the sidebar cursor is on."""
        project = self._pointed_project()
        if project is None:
            return
        self.push_screen(RenameProject(project, self._rename_project))

    def _rename_project(self, old: str, new: str) -> None:
        """Renames the project, then redraws the sidebar. Browsing the project
        that was renamed, the memos stay in front of you under the new name:
        nothing moved but the label they are filed under."""
        services.rename_project(self.repo, old, new)
        renamed = services.project_name(new)
        self._reload(renamed if self.project == old else self.project)

    def action_remove_project(self) -> None:
        """Offers to empty the project the sidebar cursor is on, asking first."""
        project = self._pointed_project()
        if project is None:
            return
        self.push_screen(
            ConfirmRemove(project),
            lambda confirmed: self._remove_project(project, confirmed),
        )

    def _remove_project(self, project: str, confirmed: bool | None) -> None:
        """Files the project's memos under other, once they have said to. The
        one project that cannot go anywhere is other itself, and being told so
        is better than the screen going down over it."""
        if not confirmed:
            return
        try:
            services.remove_project(self.repo, project)
        except services.InvalidInput as refused:
            self.notify(str(refused), severity="warning")
            return
        self._reload(self.project)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """A project fills the memo list, and hands the cursor to it. The row
        above them all fills it with every memo there is, narrowed by nothing;
        the row below them fills it with the memos that have been put away, and
        with nothing else. Each of those two carries a name a project cannot
        have, so no project can be mistaken for either."""
        if event.list_view.id == "projects":
            chosen = event.item.name or EVERYTHING
            if chosen == EVERYTHING:
                self.show_everything()
            elif chosen == ARCHIVED:
                self.show_archived()
            else:
                self.show_project(chosen)
            self.query_one("#memos", DataTable).focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """The memo keys follow the cursor down the list, since what they act on
        is whatever it has come to rest on. Only this list moves them: the
        settings screen has a table of its own, and the row highlighted in it
        says nothing about which memo is being pointed at behind it."""
        if event.data_table.id == "memos":
            self.refresh_bindings()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """A memo fills the detail below the list. The row is keyed by the id of
        the memo it describes — the one thing about a row that survives the list
        being redrawn, reordered or narrowed to a tag — and every row is drawn
        with one, so there is usually an id under the cursor to read.

        A row standing in for a recording still on its way in is keyed by the
        path it is arriving from rather than by a memo id it does not have yet,
        so there is nothing behind it to open."""
        try:
            memo_id = int(str(event.row_key.value))
        except ValueError:
            return
        self.show_memo(memo_id)
