import re
from collections.abc import Container, Iterable, Sequence
from dataclasses import dataclass

from ..domain import ActionItem

# a model restating a commitment it has already stated rarely spells it exactly
# the way it did last time, so the wording is not what one to-do is recognised
# as another by
_SPACING = re.compile(r"\s+")
# closing punctuation a model puts on some restatements and not others
_TRAILING = " .,;:!"


def normalize(task: str) -> str:
    """The key one to-do is matched to another by: the task with its spacing
    tidied, its closing punctuation dropped and its case forgotten. Two tasks
    that come to the same key are the same task, whatever wording each
    extraction happened to give it."""
    return _SPACING.sub(" ", task).strip().rstrip(_TRAILING).casefold()


@dataclass(frozen=True)
class TodoItem:
    """One to-do as a fresh extraction states it, carrying the key it is
    matched to an already stored list by."""

    text: str
    norm: str
    owner: str
    deadline: str


@dataclass(frozen=True)
class StoredTodo:
    """The little that planning needs to know about a to-do already stored:
    what it matches on, and whether anybody has had their say about it."""

    id: int
    norm: str
    touched: bool


@dataclass(frozen=True)
class TodoPlan:
    """What a memo's to-do list has to become for a fresh extraction: the rows
    to add, the rows to restate in the newer wording, and the rows to drop."""

    insert: list[TodoItem]
    refresh: list[tuple[int, TodoItem]]
    delete: list[int]


def todo_items(action_items: Iterable[ActionItem]) -> list[TodoItem]:
    """The to-dos an extraction commits to, in the order it stated them. A task
    of nothing is dropped, and one stated twice is kept once: a model repeating
    itself must not leave two rows to check off separately."""
    items: dict[str, TodoItem] = {}
    for a in action_items:
        text = (a.get("task") or "").strip()
        key = normalize(text)
        if not key or key in items:
            continue
        items[key] = TodoItem(
            text, key, (a.get("owner") or "").strip(), (a.get("deadline") or "").strip()
        )
    return list(items.values())


def reconcile(
    items: Sequence[TodoItem],
    stored: Sequence[StoredTodo],
    open_in_project: Container[str],
) -> TodoPlan:
    """How a memo's to-do list changes when its notes are extracted again.

    An extraction is regenerated whole, so the list cannot simply be written
    from it: checking a task off is the user's work, and a rerun that dropped it
    would throw that work away. A task the new notes state again therefore keeps
    its row and its status, and is merely reworded where nobody has touched it —
    a touched row is left exactly as its owner left it. A row the new notes no
    longer mention goes only while it is untouched, an untouched row being the
    model's own noise for a better run to correct.

    A task this memo has never carried is checked against the rest of its
    project before it is added: the same commitment restated in next week's
    standup is the one already open, not a second thing to do. One that was
    already done is not — that task has come round again."""
    by_norm = {row.norm: row for row in stored}
    insert: list[TodoItem] = []
    refresh: list[tuple[int, TodoItem]] = []
    matched: set[int] = set()
    for item in items:
        row = by_norm.get(item.norm)
        if row is not None:
            matched.add(row.id)
            if not row.touched:
                refresh.append((row.id, item))
        elif item.norm not in open_in_project:
            insert.append(item)
    delete = [row.id for row in stored if row.id not in matched and not row.touched]
    return TodoPlan(insert, refresh, delete)
