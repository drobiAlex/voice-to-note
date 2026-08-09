from collections.abc import Callable

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
        ("m", "move_memo", "Move"),
        # "r" rather than "n": n already means "no" in the discard dialog
        ("r", "rename_speaker", "Rename speaker"),
        ("t", "toggle_raw", "Raw transcript"),
        # lower case acts on the one memo, upper case on the whole project the
        # sidebar cursor is resting on
        ("R", "rename_project", "Rename project"),
        ("X", "remove_project", "Empty project"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, repo: Repository) -> None:
        """Reads through the database the command line has already opened."""
        super().__init__()
        self.repo = repo
        self.memo_id: int | None = None
        self.project: str | None = None
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
        """Lists one project's recordings, newest first as everywhere else."""
        self.project = project
        memos = self.query_one("#memos", ListView)
        memos.clear()
        for memo in services.memos(self.repo, project=project):
            memos.append(ListItem(Label(memo.filename), name=str(memo.id)))

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
