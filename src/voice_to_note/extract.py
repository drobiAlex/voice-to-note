import json
import shutil
import subprocess
import urllib.error
import urllib.request

from . import config

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

PROMPT = """Extract structured notes from this voice-memo transcript.

Return ONLY a JSON object, no markdown fences, exactly this shape:
{
  "title": "short descriptive title",
  "summary": "3-6 sentence summary",
  "action_items": [{"task": "...", "owner": "name or null", "deadline": "date or verbatim phrase or null"}],
  "decisions": ["..."],
  "key_insights": ["..."],
  "open_questions": ["..."],
  "dates": [{"date": "...", "context": "..."}],
  "tags": ["lowercase-keyword"]
}

Rules:
- action_items: concrete tasks someone committed to; owner = who does it (use speaker names from the transcript when clear, else null)
- deadline: keep verbatim when not a clean date ("by Friday")
- decisions: things agreed or settled in the conversation
- key_insights: important facts, realizations, information worth remembering
- open_questions: questions raised but not resolved
- dates: every date/time mentioned, with its context
- Empty arrays are fine. Never invent content not in the transcript.

Transcript:
"""


def claude_available() -> bool:
    return shutil.which("claude") is not None


def ollama_available() -> bool:
    try:
        with urllib.request.urlopen(f"{config.OLLAMA_URL}/api/tags", timeout=3) as r:
            models = [m["name"] for m in json.loads(r.read()).get("models", [])]
        return any(m.startswith(config.OLLAMA_MODEL) for m in models)
    except (urllib.error.URLError, OSError):
        return False


def _claude(prompt: str) -> str:
    proc = subprocess.run(
        ["claude", "-p", "--model", config.CLAUDE_MODEL],
        input=prompt, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed: {proc.stderr[-500:]}")
    return proc.stdout


def _ollama(prompt: str) -> str:
    body = json.dumps(
        {
            "model": config.OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "format": SCHEMA,
            "options": {"temperature": 0, "num_ctx": 32768},
        }
    ).encode()
    req = urllib.request.Request(
        f"{config.OLLAMA_URL}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.loads(r.read())["message"]["content"]


def _parse(text: str) -> dict:
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


def extract(transcript: str) -> tuple[str, dict]:
    prompt = PROMPT + transcript
    errors = []
    if claude_available():
        try:
            return "claude", _parse(_claude(prompt))
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            errors.append(f"claude: {e}")
    if ollama_available():
        try:
            return f"ollama/{config.OLLAMA_MODEL}", _parse(_ollama(prompt))
        except (RuntimeError, ValueError, urllib.error.URLError) as e:
            errors.append(f"ollama: {e}")
    if errors:
        raise RuntimeError("extraction failed: " + "; ".join(errors))
    raise RuntimeError(
        "no extraction backend: install claude CLI, or run Ollama and"
        f" `ollama pull {config.OLLAMA_MODEL}`"
    )
