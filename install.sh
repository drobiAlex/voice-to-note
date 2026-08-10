#!/usr/bin/env bash
set -euo pipefail

REPO="${VTN_INSTALL_REPO:-https://github.com/drobiAlex/voice-to-note}"

# whisper.cpp builds against Metal and models live under Application Support —
# both are macOS-only.
if [ "$(uname -s)" != "Darwin" ]; then
  echo "voice-to-note needs macOS (Metal build, Application Support paths)." >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "missing: git — install: xcode-select --install" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "installing vtn …"
# --reinstall makes re-running this installer the upgrade path: it re-fetches HEAD.
uv tool install "git+$REPO" --reinstall

# uv resolves sherpa-onnx from the project's own index at install time (see
# pyproject.toml); confirm that held and PyPI's incomplete wheel didn't win instead.
if ! "$(uv tool dir)/voice-to-note/bin/python" -c "import sherpa_onnx" 2>/dev/null; then
  echo "sherpa-onnx resolved from the wrong index (missing bundled onnxruntime)." >&2
  echo "please report an issue: $REPO/issues" >&2
  exit 1
fi

BIN="$(uv tool dir --bin)"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "hint: add $BIN to your PATH in your shell profile" ;;
esac

echo "fetching models and building whisper.cpp …"
"$BIN/vtn" setup

echo
echo "vtn lives in ~/Library/Application Support/vtn"
echo "  vtn tui                     browse and process memos"
echo "  vtn process path/to/memo.m4a"
echo
echo "re-run this installer any time to upgrade"
