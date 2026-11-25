import difflib
import re

# the languages somebody is likely to want captions in, by the code YouTube
# files them under. Not every language yt-dlp can report: a track in a
# language missing here is still listed, under the name yt-dlp gave it, and
# still chosen by its code — this table only lets a person say "polish"
# instead of remembering "pl"
LANGUAGES: dict[str, str] = {
    "ar": "Arabic", "bg": "Bulgarian", "bn": "Bengali", "ca": "Catalan",
    "cs": "Czech", "da": "Danish", "de": "German", "el": "Greek",
    "en": "English", "es": "Spanish", "et": "Estonian", "fa": "Persian",
    "fi": "Finnish", "fr": "French", "he": "Hebrew", "hi": "Hindi",
    "hr": "Croatian", "hu": "Hungarian", "id": "Indonesian", "it": "Italian",
    "ja": "Japanese", "ko": "Korean", "lt": "Lithuanian", "lv": "Latvian",
    "nl": "Dutch", "no": "Norwegian", "pl": "Polish", "pt": "Portuguese",
    "ro": "Romanian", "ru": "Russian", "sk": "Slovak", "sl": "Slovenian",
    "sr": "Serbian", "sv": "Swedish", "th": "Thai", "tr": "Turkish",
    "uk": "Ukrainian", "vi": "Vietnamese", "zh": "Chinese",
}

_BY_NAME = {name.lower(): code for code, name in LANGUAGES.items()}
# what a language code looks like: two or three letters, then optional
# regional or script subtags ("pt-BR", "zh-Hans")
_CODE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]+)*$")


def primary(code: str) -> str:
    """The language part of a code, lowercase, without region, script or the
    "-orig" yt-dlp appends to a video's own spoken language — what two tracks
    must share to be the same language however they are spelled."""
    return code.split("-", 1)[0].lower()


def language_name(code: str, reported: str = "") -> str:
    """What to call a language in words. A plain code gets the table's name; a
    regional one ("pt-BR") keeps the name yt-dlp reported for it, since the
    table cannot say which variant it is. A language the table has never heard
    of falls back to yt-dlp's name with its "(Original)" tag dropped, then to
    the bare code, so every track is still something a person can read."""
    base = code.removesuffix("-orig")
    name = reported.replace("(Original)", "").strip()
    if "-" in base and name:
        return name
    return LANGUAGES.get(base.lower()) or name or code


def resolve_language(text: str) -> str:
    """One language as a person typed it — a name ("Ukrainian") or a code
    ("uk", "pt-BR") — as the code to look for. Names resolve through the
    table only. Anything that is neither a known name nor shaped like a code
    is refused with the nearest names, because a typo passed on as a code would
    surface as a misleading "no such captions" after a network round trip.
    Codes outside the table are let through for the same reason the table is
    not exhaustive."""
    word = text.strip()
    key = word.lower()
    if key in _BY_NAME:
        return _BY_NAME[key]
    if key in LANGUAGES:
        return key
    if _CODE.match(word) and (len(word) <= 3 or "-" in word):
        return primary(word) + word[len(primary(word)):]
    close = difflib.get_close_matches(key, list(_BY_NAME), n=3, cutoff=0.6)
    hint = f" — did you mean {', '.join(name.title() for name in close)}?" if close else ""
    raise ValueError(f"unknown caption language {word!r}{hint}")


def parse_languages(text: str) -> list[str]:
    """A comma-separated preference as codes, in the order given, duplicates
    dropped. Blank entries are skipped so a trailing comma in a hand-edited
    config is harmless."""
    codes: list[str] = []
    for part in text.split(","):
        if part.strip():
            code = resolve_language(part)
            if code not in codes:
                codes.append(code)
    return codes
