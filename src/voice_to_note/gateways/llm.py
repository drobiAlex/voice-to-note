import json
import shutil
import subprocess
import urllib.error
import urllib.request

from .. import config
from . import GatewayError

# an availability check runs ahead of every extraction, so it gives up quickly
# rather than making the user wait on a backend that is not there
AVAILABILITY_TIMEOUT_S = 3
# stuck-call guards, not latency targets: a long transcript legitimately takes
# minutes, and the local model is the slower of the two
CLAUDE_TIMEOUT_S = 600
OLLAMA_TIMEOUT_S = 1800

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


class BackendError(GatewayError):
    """A backend was installed but the call to it failed."""


def notes_prompt(transcript: str) -> str:
    """Asks for the notes in the exact shape the app can store."""
    return NOTES_PROMPT + transcript


def ask_prompt(transcript: str, question: str) -> str:
    """Asks a question in a way that keeps the answer inside the transcript."""
    return ASK_PROMPT.format(question=question, transcript=transcript)


def claude_available() -> bool:
    """Whether the preferred backend is installed on this machine."""
    return shutil.which("claude") is not None


def ollama_available() -> bool:
    """Whether the local fallback is running and has the model pulled."""
    try:
        with urllib.request.urlopen(
            f"{config.OLLAMA_URL}/api/tags", timeout=AVAILABILITY_TIMEOUT_S
        ) as r:
            models = [m["name"] for m in json.loads(r.read()).get("models", [])]
        return any(m.startswith(config.OLLAMA_MODEL) for m in models)
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, AttributeError):
        # this answers a yes/no question, so anything unreadable means "no";
        # it must never be the thing that ends a run. The comparison stays inside
        # the guard too: a name that is not a string fails there, not at the fetch
        return False


def claude_complete(prompt: str) -> str:
    """Asks Claude, through the CLI the user already signed in to."""
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", config.CLAUDE_MODEL],
            input=prompt, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT_S,
        )
    except FileNotFoundError as e:
        # claude_available() passed moments ago; it can still be gone by now
        raise BackendError(f"claude could not be run: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise BackendError(str(e)) from e
    if proc.returncode != 0:
        raise BackendError(f"claude -p failed: {proc.stderr[-500:]}")
    return proc.stdout


def ollama_complete(prompt: str, schema: dict | None = None) -> str:
    """Asks the local model, which can be held to a required answer shape."""
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
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_S) as r:
            reply = r.read()
    except urllib.error.URLError as e:
        raise BackendError(str(e)) from e
    try:
        return json.loads(reply)["message"]["content"]
    except (ValueError, KeyError, TypeError) as e:
        # ollama answers 200 with an error body when the model was never pulled
        raise BackendError(f"unexpected reply from ollama: {reply[:500]!r}") from e
