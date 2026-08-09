from collections.abc import Callable

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
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


class MemoApp(App[None]):
    """One screen over the memo database: the projects down the side, the chosen
    project's recordings beside them, and what was said and made of the one
    recording you are looking at."""

    CSS = """
    #projects { width: 26; border-right: solid $panel; }
    #memos { height: 40%; border-bottom: solid $panel; }
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
        # the memos with work running on them, one job each: two passes over one
        # recording would race each other's writes
        self.jobs: set[int] = set()
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
                    with TabPane("Notes"), VerticalScroll():
                        yield Markdown(id="notes")
                    with TabPane("Transcript", id="transcript-tab"), VerticalScroll():
                        yield Static(id="transcript")
        yield Footer()

    def on_mount(self) -> None:
        """Opens on the projects that exist, with the first one already picked
        out: a list highlighting nothing makes the session's first Enter do
        nothing, which reads as the app being broken."""
        self.load_projects()
        self.query_one("#projects", ListView).focus()

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

    def show_memo(self, memo_id: int) -> None:
        """Shows one recording's notes and what was actually said in it."""
        self.memo_id = memo_id
        self.query_one("#notes", Markdown).update(services.notes_markdown(self.repo, memo_id))
        self.query_one("#transcript", Static).update(
            services.transcript_lines(self.repo, memo_id, raw=self.raw)
        )
        self.query_one(TabbedContent).get_tab("transcript-tab").label = (
            "Transcript (raw)" if self.raw else "Transcript"
        )

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
        self.query_one("#notes", Markdown).update("*no memo shown*")
        self.query_one("#transcript", Static).update("")
        self.query_one(TabbedContent).get_tab("transcript-tab").label = "Transcript"

    def action_toggle_raw(self) -> None:
        """Swaps the transcript between the repaired reading and the words as
        they were transcribed, which is how a repair pass gets checked. With no
        memo on screen there is nothing under the tab to call raw."""
        if self.memo_id is None:
            return
        self.raw = not self.raw
        self.show_memo(self.memo_id)

    def _start_job(
        self, memo_id: int, doing: str, run: Callable[[Repository, int], str]
    ) -> None:
        """Sends one piece of slow work off to a thread. A memo already being
        worked on is refused rather than queued: the second pass would be writing
        over what the first is still deciding. Two different memos are free to
        run at once."""
        if memo_id in self.jobs:
            self.notify(f"memo {memo_id} is already busy", severity="warning")
            return
        self.jobs.add(memo_id)
        self.notify(f"{doing} memo {memo_id} …")
        self._run_job(memo_id, run)

    @work(thread=True)
    def _run_job(self, memo_id: int, run: Callable[[Repository, int], str]) -> None:
        """The slow part, off the main thread because it waits on a subprocess or
        a model. It opens its own connection, since sqlite refuses one belonging
        to another thread. Only the failures a person can act on are caught: a
        worker that raises takes the whole app down with it, so a missing model
        or a stopped server has to come back as a message, while a bug keeps its
        traceback rather than being swallowed here."""
        try:
            with services.open_repo(self.repo) as worker:
                done = run(worker, memo_id)
        except (GatewayError, services.ExtractionError, services.InvalidInput) as failed:
            self.call_from_thread(self._job_ended, memo_id, str(failed), True)
            return
        self.call_from_thread(self._job_ended, memo_id, done, False)

    def _job_ended(self, memo_id: int, message: str, failed: bool) -> None:
        """Back on the main thread: frees the memo, says how it went, and redraws
        what the work changed — but only while that memo is still the one on
        screen, since pulling somebody back to a memo they have left is worse
        than letting them find it changed when they return."""
        self.jobs.discard(memo_id)
        self.notify(message, severity="error" if failed else "information")
        if not failed and self.memo_id == memo_id:
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
        self._start_job(
            memo_id,
            "extracting",
            lambda repo, mid: (
                f"memo {mid} extracted via {services.run_extraction(repo, mid, force=force)}"
            ),
        )

    def action_repair(self) -> None:
        """Repairs transcription errors in the memo on screen."""
        memo_id = self.memo_id
        if memo_id is None:
            return
        self._start_job(memo_id, "repairing", self._repair)

    def _repair(self, repo: Repository, memo_id: int) -> str:
        """The repair pass itself, and what to say once it has been stored."""
        services.refine_transcript(repo, memo_id)
        return f"memo {memo_id} repaired"

    def action_diarize(self) -> None:
        """Runs speaker detection over the memo on screen again."""
        memo_id = self.memo_id
        if memo_id is None:
            return
        self._start_job(memo_id, "diarizing", self._diarize)

    def _diarize(self, repo: Repository, memo_id: int) -> str:
        """The speaker pass itself, and what to say once it has been stored."""
        services.rediarize(repo, memo_id)
        return f"memo {memo_id} diarized"

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
