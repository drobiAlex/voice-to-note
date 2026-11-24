import re
import tomllib
from dataclasses import dataclass, field
from typing import Any

# the line that opens and closes a template's front matter. Three plus signs
# rather than the three dashes of YAML front matter: a prompt is free to start
# with a markdown rule, and a rule must never be mistaken for a config block
FENCE = "+++"


# how many extra sections a note keeps. A template declares a handful; the cap
# only exists so a runaway reply cannot turn one note into a wall of headings
MAX_SECTIONS = 20


def clean_heading(heading: str) -> str:
    """A heading as one plain line: whitespace runs, newlines included, become
    a single space and leading `#` marks go, since the renderers add their own
    and a heading carrying a newline or a `#` would break out of its line into
    the note around it. Empty means there is no heading to show."""
    return re.sub(r"\s+", " ", heading).strip().lstrip("#").strip()


class TemplateError(ValueError):
    """A template's front matter could not be read. A ValueError so the pure
    layer needs nothing from the layers above it; the service that reads the
    file turns it into the refusal a person sees."""


@dataclass(frozen=True)
class ParsedTemplate:
    """A prompt template split into what the model reads and what the
    template declares about itself.

    `sections` are the extra headings the notes should carry, in file order,
    each with what the model is to put under it. `tables` keeps every other
    front-matter table exactly as written, so a table this version does not
    know yet survives parsing and the layer that learns it can read it
    without the parser being reworked."""

    body: str
    sections: tuple[tuple[str, str], ...] = ()
    tables: dict[str, Any] = field(default_factory=dict)


def _split(text: str) -> tuple[str, str] | None:
    """The front matter and the prompt after it, or None for a template that
    does not start with a fence. Only the very first line can open the block,
    so a `+++` further down a prompt is just text."""
    # an editor's byte-order mark or a blank line above the fence must not turn
    # the block into prompt text the model is then asked to answer
    text = re.sub(r"\A\ufeff?(?:[ \t]*\r?\n)*", "", text)
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != FENCE:
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == FENCE:
            return "".join(lines[1:i]), "".join(lines[i + 1 :])
    raise TemplateError(f"front matter opens with {FENCE} but is never closed with {FENCE}")


def parse_template(text: str) -> ParsedTemplate:
    """Reads an optional TOML block between two `+++` lines off the top of a
    template. A file without one is returned whole, so every template written
    before front matter existed behaves exactly as it did. A block that is
    there but wrong is refused rather than ignored: a template author who
    wrote a `[sections]` table and got no sections would otherwise never learn
    why the notes came back without them."""
    split = _split(text)
    if split is None:
        return ParsedTemplate(body=text)
    raw, body = split
    try:
        tables = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        raise TemplateError(f"front matter is not valid TOML: {e}") from e
    declared = tables.pop("sections", {})
    if not isinstance(declared, dict):
        raise TemplateError("[sections] must be a table of heading = \"what to put there\"")
    sections: list[tuple[str, str]] = []
    for heading, ask in declared.items():
        heading = clean_heading(heading)
        if not isinstance(ask, str) or not ask.strip() or not heading:
            raise TemplateError(
                f"section {heading!r} needs a heading and a text saying what belongs under it"
            )
        sections.append((heading, ask.strip()))
    return ParsedTemplate(body=body.lstrip("\n"), sections=tuple(sections), tables=tables)


def sections_instruction(sections: tuple[tuple[str, str], ...]) -> str:
    """The sentence block that asks for the extra `sections` key. Written here
    and not left to the template author, so the JSON shape the parser accepts
    stays in this app's hands: a template says which headings it wants and
    what goes under each, and cannot get the key's name or form wrong."""
    wanted = "\n".join(f'- "{heading}": {ask}' for heading, ask in sections)
    return (
        'Also add one more key to the JSON object, "sections": a list with exactly'
        ' these entries, in this order, each shaped {"heading": "...", "body": "..."}.'
        " Use each heading verbatim; body is Markdown text, and when the transcript"
        " has nothing for a heading say so there rather than leaving it out.\n"
        f"{wanted}\n"
    )


def compose_prompt(template: ParsedTemplate, transcript: str) -> str:
    """The prompt the model is sent: the template, the instruction for its
    sections when it declares any, and the transcript last. A template
    conventionally ends on a `Transcript:` line the text follows, so the
    instruction goes in front of that line and the transcript still reads as
    the thing being answered; a template without one gets the instruction
    appended."""
    body = template.body
    if template.sections:
        instruction = sections_instruction(template.sections)
        head, marker, tail = body.rpartition("Transcript:")
        if marker and not tail.strip():
            body = f"{head}{instruction}\n{marker}{tail}"
        else:
            body = f"{body.rstrip()}\n\n{instruction}\n"
    return body + transcript
