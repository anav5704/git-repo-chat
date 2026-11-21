import json
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    Static,
)

from tui.client import create_repo, list_repos, stream_chat


class RepoList(Screen[None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("r", "reload", "Refresh"),
        Binding("c", "open", "Chat"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="repos")
        yield Button("Chat", id="open")
        yield Input(placeholder="Path or URL to index", id="add")
        yield Label("", id="error")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Source", "Status", "Commit")
        table.cursor_type = "row"
        table.zebra_stripes = True
        self.repos: list[dict[str, object]] = []
        self._loading = False
        self.action_reload()
        self.set_interval(2, self.action_reload)

    def action_reload(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.run_worker(self._load, thread=True, exclusive=False, exit_on_error=False)

    def _load(self) -> None:
        try:
            repos = list_repos()
            error = ""
        except Exception as exc:  # noqa: BLE001
            repos = None
            error = str(exc)
        self.app.call_from_thread(self._show, repos, error)
        self.app.call_from_thread(self._finish_load)

    def _finish_load(self) -> None:
        self._loading = False

    def _show(self, repos: list[dict[str, object]] | None, error: str) -> None:
        if error:
            self.query_one("#error", Label).update(error)
        if repos is None:
            return
        self.repos = repos
        table = self.query_one(DataTable)
        table.clear()
        for repo in repos:
            commit = repo.get("last_indexed_commit") or ""
            table.add_row(
                str(repo.get("source", "")), str(repo.get("status", "")), str(commit)
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        source = event.value.strip()
        if not source:
            return
        event.input.clear()
        self.run_worker(
            lambda: self._add(source), thread=True, exclusive=False, exit_on_error=False
        )

    def _add(self, source: str) -> None:
        try:
            create_repo(source)
            error = ""
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        self.app.call_from_thread(self._added, error)

    def _added(self, error: str) -> None:
        self.query_one("#error", Label).update(error)
        self.action_reload()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open":
            self.action_open()

    def action_open(self) -> None:
        table = self.query_one(DataTable)
        if (
            not self.repos
            or table.cursor_row < 0
            or table.cursor_row >= len(self.repos)
        ):
            return
        repo = self.repos[table.cursor_row]
        self.app.push_screen(ChatScreen(str(repo["id"]), str(repo["source"])))


class ChatScreen(Screen[None]):
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "back", "Back")]

    def __init__(self, repo_id: str, source: str) -> None:
        super().__init__()
        self.repo_id = repo_id
        self.source = source
        self.conversation_id: str | None = None
        self.answer = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self.source, id="source")
        yield Markdown("", id="answer")
        yield Static("", id="trace")
        yield Static("", id="citations")
        yield Input(placeholder="Ask about this repo", id="question")
        yield Footer()

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        message = event.value.strip()
        if not message:
            return
        event.input.clear()
        event.input.disabled = True
        self.answer = ""
        self.query_one("#answer", Markdown).update("")
        self.query_one("#trace", Static).update("")
        self.query_one("#citations", Static).update("")
        self.run_worker(
            lambda: self._ask(message),
            thread=True,
            exclusive=False,
            exit_on_error=False,
        )

    def _ask(self, message: str) -> None:
        try:
            stream_chat(self.repo_id, message, self.conversation_id, self._on_event)
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._show_error, str(exc))
        self.app.call_from_thread(self._enable)

    def _on_event(self, name: str, data: str) -> None:
        self.app.call_from_thread(self._apply, name, data)

    def _apply(self, name: str, data: str) -> None:
        if name == "meta":
            body = json.loads(data)
            conversation = body.get("conversation_id")
            if isinstance(conversation, str):
                self.conversation_id = conversation
            return
        if name == "token":
            self.answer += data
            self.query_one("#answer", Markdown).update(self.answer)
            return
        if name == "trace":
            self.query_one("#trace", Static).update(_trace(json.loads(data)))
            return
        if name == "citations":
            self.query_one("#citations", Static).update(_citations(json.loads(data)))
            return
        if name == "error":
            self._show_error(data)

    def _show_error(self, message: str) -> None:
        if "429" in message or "quota" in message.lower():
            message = "Gemini is rate limiting requests. Wait a minute, then ask again."
        self.query_one("#trace", Static).update(message)
        if not self.answer.strip():
            self.query_one("#answer", Markdown).update(message)

    def _enable(self) -> None:
        self.query_one("#question", Input).disabled = False
        self.query_one("#question", Input).focus()


def _trace(body: dict[str, object]) -> str:
    route = body.get("route")
    stages = body.get("stages")
    if not isinstance(route, dict):
        route = {}
    if not isinstance(stages, list):
        stages = []
    flags = "  ".join(
        f"{name} {route.get(name)}" for name in ("symbol", "semantic", "history", "web")
    )
    names = ", ".join(str(stage) for stage in stages)
    return f"{flags}\nstages: {names}"


def _citations(body: object) -> str:
    if not isinstance(body, list):
        return ""
    lines: list[str] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        start = item.get("start_line")
        end = item.get("end_line")
        if isinstance(path, str) and start and end:
            lines.append(f"{path}:{start}-{end}")
        elif isinstance(item.get("url"), str):
            lines.append(str(item["url"]))
        elif isinstance(item.get("sha"), str):
            lines.append(str(item["sha"]))
    return "\n".join(lines)


class RepoChat(App[None]):
    TITLE = "Repo chat"
    CSS = """
    #repos, #answer { height: 1fr; }
    #trace, #citations { height: 5; }
    #error { height: auto; max-height: 4; color: $error; }
    #source { height: auto; max-height: 2; }
    #add, #question { dock: bottom; }
    """

    def on_mount(self) -> None:
        self.push_screen(RepoList())
