import json
from collections.abc import Container
from typing import cast

from ..domain import Extraction, NotesPayload
from .todos import normalize

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "owner": {"type": ["string", "null"]},
                    "deadline": {"type": ["string", "null"]},
                },
                "required": ["task", "owner", "deadline"],
            },
        },
        "decisions": {"type": "array", "items": {"type": "string"}},
        "key_insights": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "dates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "context": {"type": "string"},
                },
                "required": ["date", "context"],
            },
        },
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "title", "summary", "action_items", "decisions",
        "key_insights", "open_questions", "dates", "tags",
    ],
}


def parse_notes(text: str) -> NotesPayload:
    """Rescues the notes from whatever wrapping an LLM put around them, and
    refuses anything missing a section the reader expects."""
    text = text.strip()
    if "<think>" in text and "</think>" in text:
        text = text.split("</think>", 1)[1]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in output: {text[:200]}")
    data = json.loads(text[start : end + 1])
    missing = [k for k in SCHEMA["required"] if k not in data]
    if missing:
        raise ValueError(f"missing keys: {missing}")
    # the check above is what makes this true — json.loads promises nothing
    return cast(NotesPayload, data)


def _box(task: str, done: Container[str] | None) -> str:
    """Whether an action item's box is ticked. A task is recognised as checked
    off by the key to-dos are matched on rather than by its wording, since the
    notes are regenerated whole and restate a commitment however the model
    happened to word it that time. No list of finished tasks to check against
    reads as nothing finished — a render with nothing behind it must not claim
    work is done."""
    return "[x]" if done is not None and normalize(task) in done else "[ ]"


def render_notes(extraction: Extraction, done: Container[str] | None = None) -> str:
    """Lays the notes out the way a person reads them, each action item's box
    ticked when its task is one of the memo's finished ones."""
    d = extraction.data
    lines = [
        f"# {d['title']}",
        f"  ({extraction.backend}, {extraction.created_at})",
        "",
        d["summary"],
        "",
    ]
    if d["action_items"]:
        lines.append("ACTION ITEMS")
        for a in d["action_items"]:
            extra = ", ".join(x for x in (a.get("owner"), a.get("deadline")) if x)
            lines.append(
                f"  {_box(a['task'], done)} {a['task']}" + (f"  ({extra})" if extra else "")
            )
        lines.append("")
    for items, header in (
        (d["decisions"], "DECISIONS"),
        (d["key_insights"], "KEY INSIGHTS"),
        (d["open_questions"], "OPEN QUESTIONS"),
    ):
        if items:
            lines.append(header)
            lines.extend(f"  - {item}" for item in items)
            lines.append("")
    if d["dates"]:
        lines.append("DATES")
        lines.extend(f"  - {x['date']}: {x['context']}" for x in d["dates"])
        lines.append("")
    if d["tags"]:
        lines.append("tags: " + ", ".join(d["tags"]))
    return "\n".join(lines)


def render_notes_markdown(
    extraction: Extraction, done: Container[str] | None = None
) -> str:
    """The same notes as Markdown, for a screen that renders it rather than a
    terminal that prints it. The plain form indents its sections, which Markdown
    reads as one run-on paragraph — here every section is a real heading and
    every action item a real list item."""
    d = extraction.data
    lines = [
        f"# {d['title']}",
        "",
        f"*{extraction.backend} · {extraction.created_at}*",
        "",
        d["summary"],
        "",
    ]
    if d["action_items"]:
        # whatever the model wrote stays as it wrote it: escaping its asterisks
        # would show them to a reader who meant emphasis, and a task is one line
        # here regardless of what the markup renders as
        lines += ["## Action items", ""]
        for a in d["action_items"]:
            extra = ", ".join(x for x in (a.get("owner"), a.get("deadline")) if x)
            lines.append(
                f"- {_box(a['task'], done)} {a['task']}" + (f" — {extra}" if extra else "")
            )
        lines.append("")
    for items, header in (
        (d["decisions"], "## Decisions"),
        (d["key_insights"], "## Key insights"),
        (d["open_questions"], "## Open questions"),
    ):
        if items:
            lines += [header, "", *(f"- {item}" for item in items), ""]
    if d["dates"]:
        lines += ["## Dates", "", *(f"- **{x['date']}** — {x['context']}" for x in d["dates"]), ""]
    if d["tags"]:
        lines.append("Tags: " + ", ".join(f"`{t}`" for t in d["tags"]))
    return "\n".join(lines).rstrip() + "\n"
