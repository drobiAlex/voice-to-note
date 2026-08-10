from collections.abc import Callable, Hashable, Iterable
from pathlib import Path, PurePath

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    MarkdownViewer,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from .. import services
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
        # the app behind this modal reads list selections as memo ids
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
        # the app behind this modal reads list selections as memo ids
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


class ProcessMemo(ModalScreen[None]):
    """A recording to bring in, and the project to file it under. There are two
    ways to name the file because there are two ways people have it: pasted from
    a file manager, or somewhere they would have to go and find. Both end up in
    the same line, which is the only thing the modal reads when it submits."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, root: Path, project: str, store: Callable[[str, str], None]) -> None:
        """Opens on the folder to browse from, the project being browsed, and
        the way to bring a file in."""
        super().__init__()
        self.root = root
        self.project = project
        self.store = store

    def compose(self) -> ComposeResult:
        """Where the recording is, what to file it under, and somewhere to go
        looking if the path is not already to hand."""
        yield Input(placeholder="path to a recording", id="source-path")
        yield Input(self.project, id="source-project")
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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter from either line brings the recording in."""
        self._process()

    def _process(self) -> None:
        """Closes only once the recording has actually been sent off, so a path
        with a typo in it can be corrected where it was typed."""
        try:
            self.store(
                self.query_one("#source-path", Input).value,
                self.query_one("#source-project", Input).value,
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
        "edit_notes",
        "memo_info",
        "move_memo",
        "rename_speaker",
        "toggle_raw",
        "extract",
        "repair",
        "diarize",
        "ask",
    }
)
PROJECT_ACTIONS = frozenset({"rename_project", "remove_project"})


class MemoApp(App[None]):
    """One screen over the memo database: the projects down the side, the chosen
    project's recordings beside them, and what was said and made of the one
    recording you are looking at."""

    CSS = """
    #projects { width: 26; border-right: solid $panel; }
    #memos { height: 40%; border-bottom: solid $panel; }
    #source-tree { height: 1fr; }
    """
    BINDINGS = [
        ("e", "edit_notes", "Edit notes"),
        ("i", "memo_info", "Info"),
        ("m", "move_memo", "Move"),
        # "r" rather than "n": n already means "no" in the discard dialog
        ("r", "rename_speaker", "Rename speaker"),
        ("t", "toggle_raw", "Raw transcript"),
        ("x", "extract", "Extract"),
        # the domain calls refinement a repair pass, and r already renames
        ("p", "repair", "Repair"),
        ("d", "diarize", "Diarize"),
        ("a", "ask", "Ask"),
        # neither half of the case rule: this acts on no memo and on no project,
        # it brings something new into the app
        ("o", "process", "Add recording"),
        # lower case acts on the one memo, upper case on the whole project the
        # sidebar cursor is resting on
        ("R", "rename_project", "Rename project"),
        ("X", "remove_project", "Empty project"),
        ("slash", "find_tag", "Find tag"),
        ("escape", "clear_tag", "Back to project"),
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
        # what has work running on it, one job each: two passes over the same
        # thing would race each other's writes. A memo already stored is keyed by
        # its id, a recording still being brought in by the path it came from,
        # since its memo does not exist yet
        self.jobs: set[Hashable] = set()
        # where the add-recording tree starts looking. Home is where recordings
        # tend to be; a VTN_BROWSE_ROOT setting would land here
        self.browse_root = Path.home()
        # a way of reading transcripts rather than a property of any one memo:
        # somebody checking a repair pass is checking all of it, memo after memo
        self.raw = False

    def compose(self) -> ComposeResult:
        """Projects beside the memo list, the memo list above its detail."""
        yield Header()
        with Horizontal():
            yield ListView(id="projects")
            with Vertical():
                yield ListView(id="memos")
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
        nothing, which reads as the app being broken."""
        self.load_projects()
        self.query_one("#projects", ListView).focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Which keys the footer offers, so that it never advertises one that
        would do nothing. Returning False hides a binding and also stops the key
        reaching its action; the no-op guards inside the actions stay all the
        same, since a key that is merely hidden has to remain harmless for
        anything that reaches it another way.

        The two halves are gated differently on purpose. A memo action is offered
        whenever a memo is open, whatever holds focus, because it acts on the
        memo being read. A project action is offered only while the sidebar holds
        the cursor, because the thing it acts on is the row the cursor rests on."""
        if action in MEMO_ACTIONS:
            return self.memo_id is not None
        if action in PROJECT_ACTIONS:
            # checked while the screen is still being built, before there is one
            sidebar = self.query("#projects")
            return bool(sidebar) and self.focused is sidebar.first()
        if action == "clear_tag":
            return self.tag is not None
        return True

    def load_projects(self) -> None:
        """Draws the sidebar from what is in the database now, counts and all."""
        sidebar = self.query_one("#projects", ListView)
        sidebar.clear()
        for name, count in services.projects(self.repo):
            sidebar.append(ListItem(Label(f"{name} ({count})"), name=name))
        sidebar.index = 0

    def show_project(self, project: str) -> None:
        """Lists one project's recordings, newest first as everywhere else. This
        is also the way back from a tag search, so it takes the search down with
        it rather than leaving the subtitle claiming a tag."""
        self.project = project
        self.tag = None
        self.sub_title = ""
        memos = self.query_one("#memos", ListView)
        memos.clear()
        for memo in services.memos(self.repo, project=project):
            memos.append(ListItem(Label(memo.filename), name=str(memo.id)))
        self.refresh_bindings()

    def show_tagged(self, tag: str) -> None:
        """Fills the memo list with everything carrying one tag, from whatever
        project it is filed under. The subtitle says so: the list no longer
        matches the highlighted project, and leaving that unsaid makes the
        sidebar read as a lie."""
        memos = self.query_one("#memos", ListView)
        memos.clear()
        for memo in services.memos(self.repo, tag=tag):
            memos.append(ListItem(Label(memo.filename), name=str(memo.id)))
        self.tag = tag
        self.sub_title = f"tag: {tag}"
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
        """Redraws the sidebar and, where one is being browsed, that project's
        memo list, dropping the detail panes when the memo they describe is no
        longer among its rows. Every bulk refiling ends here."""
        self.load_projects()
        if project is None:
            return
        self.show_project(project)
        memo_id = self.memo_id
        if memo_id is not None and all(
            memo.id != memo_id for memo in services.memos(self.repo, project=project)
        ):
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
        they were transcribed, which is how a repair pass gets checked. With no
        memo on screen there is nothing under the tab to call raw."""
        if self.memo_id is None:
            return
        self.raw = not self.raw
        self.show_memo(self.memo_id)

    def _start_job(
        self, key: Hashable, subject: str, doing: str, run: Callable[[Repository], str]
    ) -> None:
        """Sends one piece of slow work off to a thread. The key is whatever is
        being worked on: a memo's id for work on one already stored, and the
        source path for a recording being brought in, which has no memo yet.
        Either way one job at a time on the same thing — a second pass would be
        writing over what the first is still deciding — while two different
        things are free to run at once."""
        if key in self.jobs:
            self.notify(f"{subject} is already busy", severity="warning")
            return
        self.jobs.add(key)
        self.notify(f"{doing} {subject} …")
        self._run_job(key, run)

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
        it went, and redraws what the work changed — but only while that memo is
        still the one on screen, since pulling somebody back to a memo they have
        left is worse than letting them find it changed when they return. A key
        that is not a memo id simply never matches the memo on screen."""
        self.jobs.discard(key)
        self.notify(message, severity="error" if failed else "information")
        memo_id = self.memo_id
        if not failed and memo_id is not None and memo_id == key:
            self.show_memo(memo_id)

    def action_extract(self) -> None:
        """Turns the memo on screen into notes, asking first when that would go
        over a note somebody wrote by hand."""
        memo_id = self.memo_id
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
        """Repairs transcription errors in the memo on screen."""
        memo_id = self.memo_id
        if memo_id is None:
            return
        self._start_memo_job(memo_id, "repairing", lambda repo: self._repair(repo, memo_id))

    def _repair(self, repo: Repository, memo_id: int) -> str:
        """The repair pass itself, and what to say once it has been stored."""
        services.refine_transcript(repo, memo_id)
        return f"memo {memo_id} repaired"

    def action_diarize(self) -> None:
        """Runs speaker detection over the memo on screen again."""
        memo_id = self.memo_id
        if memo_id is None:
            return
        self._start_memo_job(memo_id, "diarizing", lambda repo: self._diarize(repo, memo_id))

    def _diarize(self, repo: Repository, memo_id: int) -> str:
        """The speaker pass itself, and what to say once it has been stored."""
        services.rediarize(repo, memo_id)
        return f"memo {memo_id} diarized"

    def action_process(self) -> None:
        """Offers to bring a new recording in, filed where you are looking."""
        self.push_screen(
            ProcessMemo(self.browse_root, self.project or "other", self._process)
        )

    def _process(self, path: str, project: str) -> None:
        """Refuses what is plainly not a recording, and a project with no name,
        here and now so the modal keeps what was typed. Past that the pipeline
        decides: a file ffmpeg cannot make sense of fails the way any gateway
        does, and says what ffmpeg said."""
        src = Path(path.strip())
        if not src.is_file():
            raise services.InvalidInput("no recording at that path")
        name = services.project_name(project)
        self._start_job(
            src, src.name, "processing", lambda repo: self._processed(repo, src, name)
        )

    def _processed(self, repo: Repository, src: Path, project: str) -> str:
        """Runs the whole pipeline, then draws the new memo into the lists it
        belongs in without pulling the reader off whatever they were reading:
        it is stored, so it will be there when they go looking."""
        result = services.process_memo(repo, src, project, log=self._stage)
        self.call_from_thread(self._reload, self.project)
        return f"{src.name} is memo {result.memo_id}"

    def _stage(self, message: str) -> None:
        """One line of progress from the pipeline, said on the main thread.
        Converting and transcribing take minutes between them, and a screen
        that says nothing for that long reads as one that has hung."""
        self.call_from_thread(self.notify, message)

    def action_ask(self) -> None:
        """Offers to put a question to the memo on screen."""
        memo_id = self.memo_id
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

    def action_clear_tag(self) -> None:
        """Puts the project back in the list once a tag search has been read.
        With no search showing there is nothing to come back from, so escape
        stays out of the way of everything else."""
        if self.tag is None:
            return
        if self.project is None:
            self.query_one("#memos", ListView).clear()
            self.tag = None
            self.sub_title = ""
            self.refresh_bindings()
        else:
            self.show_project(self.project)

    def action_memo_info(self) -> None:
        """Says what state the memo on screen is in, if one is on screen at all."""
        if self.memo_id is None:
            return
        self.push_screen(MemoDetails(services.memo_info_text(self.repo, self.memo_id)))

    def action_edit_notes(self) -> None:
        """Opens the shown memo's notes for editing, if one is shown at all."""
        if self.memo_id is None:
            return
        self.push_screen(
            NoteEditor(services.notes_markdown(self.repo, self.memo_id), self._save_notes)
        )

    def _save_notes(self, markdown: str) -> None:
        """Stores what they wrote, then shows the memo as it now reads."""
        memo_id = self.memo_id
        if memo_id is None:
            return
        services.save_notes(self.repo, memo_id, markdown)
        self.show_memo(memo_id)

    def action_move_memo(self) -> None:
        """Offers to refile the memo on screen, if one is on screen at all."""
        if self.memo_id is None:
            return
        self.push_screen(MoveMemo(services.projects(self.repo), self._move_memo))

    def _move_memo(self, project: str) -> None:
        """Refiles the memo, then redraws what that changed. Filed away from the
        project on screen, the memo drops out of the list, and panes left
        describing it would be describing a row that is no longer there."""
        memo_id = self.memo_id
        if memo_id is None:
            return
        services.move_memo(self.repo, memo_id, project)
        self._reload(self.project)

    def action_rename_speaker(self) -> None:
        """Offers to name a voice in the memo on screen."""
        if self.memo_id is None:
            return
        self.push_screen(
            RenameSpeaker(services.speakers(self.repo, self.memo_id), self._rename_speaker)
        )

    def _rename_speaker(self, label: str, name: str) -> None:
        """Names the voice, then shows the transcript calling it that."""
        memo_id = self.memo_id
        if memo_id is None:
            return
        services.rename_speaker(self.repo, memo_id, label, name)
        self.show_memo(memo_id)

    def _pointed_project(self) -> str | None:
        """The project the sidebar cursor is resting on, and nothing at all once
        the cursor is no longer what the person is pointing at: the project keys
        read the sidebar, so they stay out of the way while the memos have focus."""
        sidebar = self.query_one("#projects", ListView)
        if self.focused is not sidebar:
            return None
        chosen = sidebar.highlighted_child
        return chosen.name if chosen else None

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
        """A project fills the memo list; a memo fills the detail below it."""
        chosen = event.item.name or ""
        if event.list_view.id == "projects":
            self.show_project(chosen)
            self.query_one("#memos", ListView).focus()
        else:
            self.show_memo(int(chosen))
