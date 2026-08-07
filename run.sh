#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

REPO=vendor/whisper.cpp
MODELS_DIR=models
MODEL="${VTN_WHISPER_MODEL:-large-v3-turbo}"

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 — install: $2" >&2; exit 1; }
}
need uv "curl -LsSf https://astral.sh/uv/install.sh | sh"
need git "xcode-select --install"
need cmake "brew install cmake"
need ffmpeg "brew install ffmpeg"

uv sync --quiet

if [ ! -d "$REPO" ]; then
  echo "cloning whisper.cpp …"
  git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$REPO"
fi

BIN="$REPO/build/bin/whisper-cli"
if [ ! -x "$BIN" ]; then
  echo "building whisper.cpp (Metal) …"
  cmake -S "$REPO" -B "$REPO/build" -DCMAKE_BUILD_TYPE=Release > /dev/null
  cmake --build "$REPO/build" -j --config Release > /dev/null
fi

mkdir -p "$MODELS_DIR" data

GGML="$MODELS_DIR/ggml-$MODEL.bin"
if [ ! -f "$GGML" ]; then
  echo "downloading whisper model $MODEL …"
  bash "$REPO/models/download-ggml-model.sh" "$MODEL" "$MODELS_DIR"
fi

VAD="$MODELS_DIR/ggml-silero-v5.1.2.bin"
if [ ! -f "$VAD" ]; then
  if [ -f "$REPO/models/download-vad-model.sh" ]; then
    echo "downloading VAD model …"
    bash "$REPO/models/download-vad-model.sh" silero-v5.1.2 "$MODELS_DIR" \
      || echo "VAD download failed — continuing without VAD"
  fi
fi

SHERPA_RELEASES="https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEG_DIR="$MODELS_DIR/sherpa-onnx-pyannote-segmentation-3-0"
if [ ! -f "$SEG_DIR/model.onnx" ]; then
  echo "downloading speaker segmentation model …"
  curl -fSL "$SHERPA_RELEASES/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2" \
    | tar -xj -C "$MODELS_DIR"
fi
EMB="$MODELS_DIR/nemo_en_titanet_large.onnx"
if [ ! -f "$EMB" ]; then
  echo "downloading speaker embedding model …"
  curl -fSL -o "$EMB" \
    "$SHERPA_RELEASES/speaker-recongition-models/nemo_en_titanet_large.onnx"
fi

if [ $# -gt 0 ]; then
  exec uv run vtn "$@"
fi

echo
echo "setup complete."
echo "  process a memo:   ./run.sh process path/to/memo.m4a"
echo "  list memos:       ./run.sh list"
echo "  show transcript:  ./run.sh show <id>"
