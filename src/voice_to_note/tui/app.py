from collections.abc import Callable

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Footer,
    Header,
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


class MemoApp(App[None]):
    """One screen over the memo database: the projects down the side, the chosen
    project's recordings beside them, and what was said and made of the one
    recording you are looking at."""

    CSS = """
    #projects { width: 26; border-right: solid $panel; }
    #memos { height: 40%; border-bottom: solid $panel; }
    """
    BINDINGS = [("e", "edit_notes", "Edit notes"), ("q", "quit", "Quit")]

    def __init__(self, repo: Repository) -> None:
        """Reads through the database the command line has already opened."""
        super().__init__()
        self.repo = repo
        self.memo_id: int | None = None

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
                    with TabPane("Transcript"), VerticalScroll():
                        yield Static(id="transcript")
        yield Footer()

    def on_mount(self) -> None:
        """Opens on the projects that exist, with the first one already picked
        out: a list highlighting nothing makes the session's first Enter do
        nothing, which reads as the app being broken."""
        sidebar = self.query_one("#projects", ListView)
        for name, count in services.projects(self.repo):
            sidebar.append(ListItem(Label(f"{name} ({count})"), name=name))
        sidebar.index = 0
        sidebar.focus()

    def show_project(self, project: str) -> None:
        """Lists one project's recordings, newest first as everywhere else."""
        memos = self.query_one("#memos", ListView)
        memos.clear()
        for memo in services.memos(self.repo, project=project):
            memos.append(ListItem(Label(memo.filename), name=str(memo.id)))

    def show_memo(self, memo_id: int) -> None:
        """Shows one recording's notes and what was actually said in it."""
        self.memo_id = memo_id
        self.query_one("#notes", Markdown).update(services.notes_markdown(self.repo, memo_id))
        self.query_one("#transcript", Static).update(
            services.transcript_lines(self.repo, memo_id)
        )

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

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """A project fills the memo list; a memo fills the detail below it."""
        chosen = event.item.name or ""
        if event.list_view.id == "projects":
            self.show_project(chosen)
            self.query_one("#memos", ListView).focus()
        else:
            self.show_memo(int(chosen))
