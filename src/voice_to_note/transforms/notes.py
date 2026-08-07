import json

from ..domain import Extraction

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


def parse_notes(text: str) -> dict:
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
    return data


def render_notes(extraction: Extraction) -> str:
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
            lines.append(f"  [ ] {a['task']}" + (f"  ({extra})" if extra else ""))
        lines.append("")
    for key, header in (
        ("decisions", "DECISIONS"),
        ("key_insights", "KEY INSIGHTS"),
        ("open_questions", "OPEN QUESTIONS"),
    ):
        if d[key]:
            lines.append(header)
            lines.extend(f"  - {item}" for item in d[key])
            lines.append("")
    if d["dates"]:
        lines.append("DATES")
        lines.extend(f"  - {x['date']}: {x['context']}" for x in d["dates"])
        lines.append("")
    if d["tags"]:
        lines.append("tags: " + ", ".join(d["tags"]))
    return "\n".join(lines)
