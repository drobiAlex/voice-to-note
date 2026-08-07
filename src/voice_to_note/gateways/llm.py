import json
import shutil
import subprocess
import urllib.error
import urllib.request

from .. import config

NOTES_PROMPT = """Extract structured notes from this voice-memo transcript.

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

ASK_PROMPT = """Answer the question using only this voice-memo transcript. Reference speakers and timestamps where helpful. If the answer is not in the transcript, say so plainly. Be concise.

Question: {question}

Transcript:
{transcript}"""


class BackendError(RuntimeError):
    """A backend was installed but the call to it failed."""


def notes_prompt(transcript: str) -> str:
    return NOTES_PROMPT + transcript


def ask_prompt(transcript: str, question: str) -> str:
    return ASK_PROMPT.format(question=question, transcript=transcript)


def claude_available() -> bool:
    return shutil.which("claude") is not None


def ollama_available() -> bool:
    try:
        with urllib.request.urlopen(f"{config.OLLAMA_URL}/api/tags", timeout=3) as r:
            models = [m["name"] for m in json.loads(r.read()).get("models", [])]
        return any(m.startswith(config.OLLAMA_MODEL) for m in models)
    except (urllib.error.URLError, OSError):
        return False


def claude_complete(prompt: str) -> str:
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", config.CLAUDE_MODEL],
            input=prompt, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired as e:
        raise BackendError(str(e)) from e
    if proc.returncode != 0:
        raise BackendError(f"claude -p failed: {proc.stderr[-500:]}")
    return proc.stdout


def ollama_complete(prompt: str, schema: dict | None = None) -> str:
    payload = {
        "model": config.OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_ctx": 32768},
    }
    if schema is not None:
        payload["format"] = schema
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{config.OLLAMA_URL}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            return json.loads(r.read())["message"]["content"]
    except urllib.error.URLError as e:
        raise BackendError(str(e)) from e
