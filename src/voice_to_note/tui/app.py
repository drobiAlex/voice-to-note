from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
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
)

from .. import services
from ..storage.repository import Repository


class MemoApp(App[None]):
    """One screen over the memo database: the projects down the side, the chosen
    project's recordings beside them, and what was said and made of the one
    recording you are looking at."""

    CSS = """
    #projects { width: 26; border-right: solid $panel; }
    #memos { height: 40%; border-bottom: solid $panel; }
    """
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, repo: Repository) -> None:
        """Reads through the database the command line has already opened."""
        super().__init__()
        self.repo = repo

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
        self.query_one("#notes", Markdown).update(services.notes_markdown(self.repo, memo_id))
        self.query_one("#transcript", Static).update(
            services.transcript_lines(self.repo, memo_id)
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """A project fills the memo list; a memo fills the detail below it."""
        chosen = event.item.name or ""
        if event.list_view.id == "projects":
            self.show_project(chosen)
            self.query_one("#memos", ListView).focus()
        else:
            self.show_memo(int(chosen))
