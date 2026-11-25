import difflib
import re
from collections.abc import Collection

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

# what people call their own language, for the ones likely to be typed that
# way; matched case-insensitively like the English names
_NATIVE = {
    "українська": "uk", "polski": "pl", "deutsch": "de", "español": "es",
    "francais": "fr", "français": "fr", "italiano": "it", "português": "pt",
    "portugues": "pt", "nederlands": "nl", "русский": "ru", "čeština": "cs",
    "svenska": "sv", "norsk": "no", "suomi": "fi", "magyar": "hu",
    "română": "ro", "türkçe": "tr", "日本語": "ja", "한국어": "ko", "中文": "zh",
}

# every two-letter code ISO 639-1 defines. A code outside this list is a typo
# or a country code, and passing it on would only ever fail to match
ISO_639_1 = frozenset(
    "aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch"
    " co cr cs cu cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga"
    " gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja"
    " jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv"
    " mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or"
    " os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr"
    " ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi"
    " vo wa wo xh yi yo za zh zu".split()
)

# the three-letter ISO 639-2 codes people reach for, for the languages in the
# table (both the bibliographic and the terminologic spelling where they differ)
_THREE = {
    "ara": "ar", "bul": "bg", "ben": "bn", "cat": "ca", "ces": "cs", "cze": "cs",
    "dan": "da", "deu": "de", "ger": "de", "ell": "el", "gre": "el", "eng": "en",
    "spa": "es", "est": "et", "fas": "fa", "per": "fa", "fin": "fi", "fra": "fr",
    "fre": "fr", "heb": "he", "hin": "hi", "hrv": "hr", "hun": "hu", "ind": "id",
    "ita": "it", "jpn": "ja", "kor": "ko", "lit": "lt", "lav": "lv", "nld": "nl",
    "dut": "nl", "nor": "no", "pol": "pl", "por": "pt", "ron": "ro", "rum": "ro",
    "rus": "ru", "slk": "sk", "slo": "sk", "slv": "sl", "srp": "sr", "swe": "sv",
    "tha": "th", "tur": "tr", "ukr": "uk", "vie": "vi", "zho": "zh", "chi": "zh",
}

# country codes mistaken for language codes. "kr" is a real ISO code (Kanuri)
# but no video is captioned in it, and Korea's "kr" is by far the likelier
# intent, so it is treated as the mistake it almost always is
_COUNTRY = {
    "ua": "uk", "jp": "ja", "cn": "zh", "gr": "el", "cz": "cs", "dk": "da",
    "kr": "ko", "se": "sv", "us": "en", "gb": "en",
}

# a language code: two or three letters, optional regional or script subtags
_CODE = re.compile(r"^([A-Za-z]{2,3})(-[A-Za-z0-9]+)*$")


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


def resolve_language(text: str, offered: Collection[str] = ()) -> str:
    """One language as a person typed it — a name ("Ukrainian", "polski") or a
    code ("uk", "ukr", "pt-BR") — as the code to look for. Names resolve
    through the table; two-letter codes must be real ISO 639-1 codes and
    three-letter ones are mapped to them where the language is in the table.
    The usual country-code slips ("ua", "jp") are refused with what was
    probably meant, because passing one on would silently never match and the
    preference would quietly lose a language. Strictness is for what a person
    types: a code the video itself offers (`offered`) is always accepted, so a
    track the table has never heard of can still be picked from the video's own
    list."""
    word = text.strip()
    key = word.lower()
    for code in offered:
        if code.lower() == key:
            return code
    if key in _BY_NAME:
        return _BY_NAME[key]
    if key in _NATIVE:
        return _NATIVE[key]
    if key in _COUNTRY:
        meant = _COUNTRY[key]
        raise ValueError(
            f"unknown caption language {word!r} — did you mean {meant}"
            f" ({LANGUAGES[meant]})?"
        )
    match = _CODE.match(word)
    if match:
        base = match.group(1).lower()
        rest = word[len(base):]
        if len(base) == 2 and base in ISO_639_1:
            return base + rest
        if len(base) == 3 and base in _THREE:
            return _THREE[base] + rest
        if len(base) == 3 and base not in _BY_NAME:
            # a three-letter code outside the table ("fil", "yue") is what
            # yt-dlp itself uses for languages that have no two-letter one
            return base + rest
    close = difflib.get_close_matches(key, list(_BY_NAME), n=3, cutoff=0.6)
    hint = f" — did you mean {', '.join(name.title() for name in close)}?" if close else ""
    raise ValueError(f"unknown caption language {word!r}{hint}")


def parse_languages(text: str, offered: Collection[str] = ()) -> list[str]:
    """A comma-separated preference as codes, in the order given, duplicates
    dropped. Blank entries are skipped so a trailing comma in a hand-edited
    config is harmless."""
    codes: list[str] = []
    for part in text.split(","):
        if part.strip():
            code = resolve_language(part, offered)
            if code not in codes:
                codes.append(code)
    return codes
